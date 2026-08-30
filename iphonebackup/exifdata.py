"""EXIF extraction, read straight from the copied-out image files.

``exifread`` is tried first (it copes with JPEG, TIFF, HEIC and most raw
formats); Pillow is the fallback. Both are optional -- without them the
extractor still runs and simply leaves the EXIF columns empty.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .util import IMAGE_EXTS

try:  # optional
    import exifread  # type: ignore
except Exception:  # pragma: no cover
    exifread = None

try:  # optional
    from PIL import Image, ExifTags  # type: ignore
except Exception:  # pragma: no cover
    Image = None
    ExifTags = None

EXIF_COLUMNS = [
    "exif_datetime",
    "exif_make",
    "exif_model",
    "exif_lens",
    "exif_software",
    "exif_orientation",
    "exif_width",
    "exif_height",
    "exif_latitude",
    "exif_longitude",
    "exif_altitude",
]

EMPTY = {key: "" for key in EXIF_COLUMNS}


def available() -> str:
    parts = []
    if exifread is not None:
        parts.append("exifread")
    if Image is not None:
        parts.append("Pillow")
    return ", ".join(parts) or "none (EXIF columns will be blank)"


def _ratio(value) -> Optional[float]:
    try:
        if hasattr(value, "num") and hasattr(value, "den"):
            return value.num / value.den if value.den else None
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _dms_to_decimal(values, ref) -> Optional[float]:
    try:
        parts = [_ratio(v) for v in values]
    except TypeError:
        return None
    parts = [p for p in parts if p is not None]
    if not parts:
        return None
    while len(parts) < 3:
        parts.append(0.0)
    decimal = parts[0] + parts[1] / 60.0 + parts[2] / 3600.0
    if str(ref).upper().startswith(("S", "W")):
        decimal = -decimal
    return round(decimal, 8)


def _from_exifread(path: Path) -> Optional[dict]:
    if exifread is None:
        return None
    try:
        with path.open("rb") as handle:
            tags = exifread.process_file(handle, details=False)
    except Exception:
        return None
    if not tags:
        return None

    def text(name):
        tag = tags.get(name)
        return str(tag).strip() if tag is not None else ""

    out = dict(EMPTY)
    out["exif_datetime"] = (
        text("EXIF DateTimeOriginal")
        or text("EXIF DateTimeDigitized")
        or text("Image DateTime")
    )
    out["exif_make"] = text("Image Make")
    out["exif_model"] = text("Image Model")
    out["exif_lens"] = text("EXIF LensModel") or text("MakerNote LensModel")
    out["exif_software"] = text("Image Software")
    out["exif_orientation"] = text("Image Orientation")
    out["exif_width"] = text("EXIF ExifImageWidth") or text("Image ImageWidth")
    out["exif_height"] = text("EXIF ExifImageLength") or text("Image ImageLength")

    lat = tags.get("GPS GPSLatitude")
    lon = tags.get("GPS GPSLongitude")
    if lat is not None and lon is not None:
        out["exif_latitude"] = _dms_to_decimal(
            lat.values, text("GPS GPSLatitudeRef")
        ) or ""
        out["exif_longitude"] = _dms_to_decimal(
            lon.values, text("GPS GPSLongitudeRef")
        ) or ""
    alt = tags.get("GPS GPSAltitude")
    if alt is not None:
        value = _ratio(alt.values[0]) if getattr(alt, "values", None) else None
        if value is not None:
            if str(tags.get("GPS GPSAltitudeRef", "")).strip() in ("1", "Below sea level"):
                value = -value
            out["exif_altitude"] = round(value, 3)
    return out if any(str(v) for v in out.values()) else None


def _from_pillow(path: Path) -> Optional[dict]:
    if Image is None:
        return None
    try:
        with Image.open(path) as img:
            out = dict(EMPTY)
            out["exif_width"], out["exif_height"] = img.size
            exif = img.getexif()
            if not exif:
                return out
            named = {
                ExifTags.TAGS.get(tag, tag): value for tag, value in exif.items()
            }
            try:
                ifd = exif.get_ifd(0x8769)
                named.update(
                    {ExifTags.TAGS.get(t, t): v for t, v in ifd.items()}
                )
            except Exception:
                pass
            out["exif_datetime"] = str(
                named.get("DateTimeOriginal") or named.get("DateTime") or ""
            )
            out["exif_make"] = str(named.get("Make") or "").strip()
            out["exif_model"] = str(named.get("Model") or "").strip()
            out["exif_lens"] = str(named.get("LensModel") or "").strip()
            out["exif_software"] = str(named.get("Software") or "").strip()
            out["exif_orientation"] = str(named.get("Orientation") or "")
            try:
                gps = exif.get_ifd(0x8825)
            except Exception:
                gps = None
            if gps:
                tags = {ExifTags.GPSTAGS.get(t, t): v for t, v in gps.items()}
                if "GPSLatitude" in tags and "GPSLongitude" in tags:
                    out["exif_latitude"] = _dms_to_decimal(
                        tags["GPSLatitude"], tags.get("GPSLatitudeRef", "N")
                    ) or ""
                    out["exif_longitude"] = _dms_to_decimal(
                        tags["GPSLongitude"], tags.get("GPSLongitudeRef", "E")
                    ) or ""
                if "GPSAltitude" in tags:
                    value = _ratio(tags["GPSAltitude"])
                    if value is not None:
                        if str(tags.get("GPSAltitudeRef", 0)) in ("1", "b'\\x01'"):
                            value = -value
                        out["exif_altitude"] = round(value, 3)
            return out
    except Exception:
        return None


def read_exif(path: Path) -> dict:
    """EXIF fields for one file; blank dict for videos and unreadable files."""
    path = Path(path)
    if path.suffix.lower() not in IMAGE_EXTS:
        return dict(EMPTY)
    result = _from_exifread(path)
    if result is None or not result.get("exif_datetime"):
        fallback = _from_pillow(path)
        if fallback:
            if result is None:
                result = fallback
            else:
                for key, value in fallback.items():
                    if not str(result.get(key, "")):
                        result[key] = value
    return result or dict(EMPTY)
