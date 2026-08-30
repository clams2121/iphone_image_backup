"""Small helpers shared by the inspector and the extractor."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Core Data / CFAbsoluteTime epoch.
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".tif", ".tiff",
    ".bmp", ".webp", ".dng", ".cr2", ".nef", ".arw", ".raf", ".orf", ".avif",
}
VIDEO_EXTS = {
    ".mov", ".mp4", ".m4v", ".avi", ".mpg", ".mpeg", ".3gp", ".3g2", ".mkv", ".webm",
}
AUDIO_EXTS = {".m4a", ".caf", ".wav", ".aac", ".mp3", ".amr", ".aiff", ".aif", ".opus"}

MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ +@()\[\]-]+")


def kind_for_ext(name: str) -> str:
    """'image', 'video', 'audio' or 'other' based on a file name's extension."""
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    return "other"


def apple_timestamp(value, unit: str = "s") -> str:
    """Convert a Core Data timestamp to an ISO-8601 UTC string ('' if unusable).

    ``unit`` may be 's' (Photos.sqlite, Core Data default), 'ns' (Messages on
    iOS 11+) or 'auto' to guess from the magnitude.
    """
    if value in (None, "", 0):
        return ""
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return ""
    if unit == "auto":
        # Seconds-since-2001 for any plausible date stay below ~1e10; the
        # nanosecond form used by Messages on iOS 11+ is around 1e17.
        unit = "ns" if abs(raw) > 1e11 else "s"
    if unit == "ns":
        raw = raw / 1_000_000_000.0
    try:
        return (APPLE_EPOCH + timedelta(seconds=raw)).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return ""


def unix_timestamp(value) -> str:
    """Convert a Unix epoch value to an ISO-8601 UTC string ('' if unusable)."""
    if value in (None, "", 0):
        return ""
    try:
        return (
            datetime.fromtimestamp(float(value), tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def coordinate(value):
    """Photos stores -180.0 in ZLATITUDE/ZLONGITUDE to mean 'no location'."""
    if value is None:
        return None
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    if val <= -180.0 or val != val:  # sentinel or NaN
        return None
    return val


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def sanitize_name(name: str, fallback: str = "unnamed") -> str:
    """Make a file name safe to write, without losing the extension."""
    name = (name or "").strip().replace("/", "_").replace("\\", "_")
    name = _SAFE_NAME.sub("_", name).strip(" .")
    if not name:
        name = fallback
    if len(name) > 180:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            name = stem[: 180 - len(ext) - 1] + "." + ext
        else:
            name = name[:180]
    return name


def sanitize_dirname(name: str, fallback: str = "device") -> str:
    return sanitize_name(name, fallback).replace(" ", "_")


def unique_path(directory: Path, filename: str) -> Path:
    """A path inside ``directory`` that does not collide with an existing file."""
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    ext = Path(filename).suffix
    counter = 1
    while True:
        candidate = directory / f"{stem}_{counter}{ext}"
        if not candidate.exists():
            return candidate
        counter += 1


def assert_within(root: Path, target: Path) -> Path:
    """Guard against ever writing outside the chosen output directory."""
    root = root.resolve()
    resolved = Path(target).resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError(f"refusing to write outside output directory: {resolved}")
    return resolved


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"
