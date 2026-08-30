"""Adaptive reader for Photos.sqlite (the Photos app library database).

Table and column names move between iOS releases (ZGENERICASSET became
ZASSET, the album join table is numbered differently in every build), so
everything here is discovered from ``sqlite_master``/``PRAGMA table_info``
at run time rather than hardcoded.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Optional

from .util import apple_timestamp, coordinate

ASSET_TABLES = ("ZASSET", "ZGENERICASSET")
ALBUM_TABLES = ("ZGENERICALBUM", "ZALBUM")
ATTRIBUTE_TABLES = ("ZADDITIONALASSETATTRIBUTES",)

JOIN_TABLE_RE = re.compile(r"^Z_\d+ASSETS$", re.IGNORECASE)

# candidate column names, best first
ASSET_FIELDS = {
    "uuid": ("ZUUID",),
    "filename": ("ZFILENAME",),
    "directory": ("ZDIRECTORY",),
    "date_created": ("ZDATECREATED",),
    "date_added": ("ZADDEDDATE",),
    "date_modified": ("ZMODIFICATIONDATE",),
    "latitude": ("ZLATITUDE",),
    "longitude": ("ZLONGITUDE",),
    "favorite": ("ZFAVORITE",),
    "hidden": ("ZHIDDEN",),
    "trashed": ("ZTRASHEDSTATE",),
    "trashed_date": ("ZTRASHEDDATE",),
    "kind": ("ZKIND",),
    "duration": ("ZDURATION",),
    "width": ("ZWIDTH",),
    "height": ("ZHEIGHT",),
    "tz_offset": ("ZTIMEZONEOFFSET",),
    "saved_asset_type": ("ZSAVEDASSETTYPE",),
}

ATTRIBUTE_FIELDS = {
    "original_filename": ("ZORIGINALFILENAME",),
    "timezone_name": ("ZTIMEZONENAME",),
    "exif_timestamp": ("ZEXIFTIMESTAMPSTRING",),
    "creator_bundle": ("ZCREATORBUNDLEID",),
    "import_session": ("ZIMPORTEDBYBUNDLEIDENTIFIER", "ZIMPORTEDBYDISPLAYNAME"),
}

KIND_NAMES = {0: "photo", 1: "video"}


def _tables(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {row[0].upper(): row[0] for row in rows}


def _columns(conn: sqlite3.Connection, table: str) -> dict:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {row[1].upper(): row[1] for row in rows}


class PhotosLibrary:
    """Reads a Photos.sqlite copy and indexes assets by path and filename."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(f"file:{self.db_path}", uri=True, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.text_factory = lambda b: b.decode("utf-8", "replace")
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

        self.tables = _tables(self.conn)
        self.warnings: list = []

        self.asset_table = self._pick_table(ASSET_TABLES)
        if not self.asset_table:
            raise ValueError(
                "no asset table (ZASSET/ZGENERICASSET) in "
                f"{self.db_path.name}; tables found: "
                + ", ".join(sorted(self.tables.values())[:25])
            )
        self.asset_columns = _columns(self.conn, self.asset_table)
        self.fields = self._resolve(self.asset_columns, ASSET_FIELDS)

        self.attribute_table = self._pick_table(ATTRIBUTE_TABLES)
        self.attribute_fields: dict = {}
        self.attribute_fk: Optional[str] = None
        if self.attribute_table:
            cols = _columns(self.conn, self.attribute_table)
            self.attribute_fields = self._resolve(cols, ATTRIBUTE_FIELDS)
            self.attribute_fk = cols.get("ZASSET")
            if not self.attribute_fk:
                self.attribute_table = None
                self.warnings.append(
                    "ZADDITIONALASSETATTRIBUTES has no ZASSET column; "
                    "original filenames unavailable"
                )

        self.album_table = self._pick_table(ALBUM_TABLES)
        self.album_join = self._find_album_join()

        self._by_relpath: dict = {}
        self._by_filename: dict = {}
        self._assets: list = []

    # ---------------------------------------------------------- discovery

    def _pick_table(self, candidates) -> Optional[str]:
        for name in candidates:
            if name.upper() in self.tables:
                return self.tables[name.upper()]
        return None

    @staticmethod
    def _resolve(columns: dict, spec: dict) -> dict:
        out = {}
        for key, candidates in spec.items():
            for candidate in candidates:
                if candidate.upper() in columns:
                    out[key] = columns[candidate.upper()]
                    break
        return out

    def _find_album_join(self) -> Optional[dict]:
        """Locate the numbered album<->asset join table for this schema."""
        if not self.album_table:
            return None
        for upper, real in self.tables.items():
            if not JOIN_TABLE_RE.match(upper):
                continue
            cols = [
                c for c in _columns(self.conn, real).values()
                if not c.upper().startswith("Z_FOK")
            ]
            album_col = next((c for c in cols if "ALBUM" in c.upper()), None)
            asset_col = next(
                (c for c in cols if "ASSET" in c.upper() and c != album_col), None
            )
            if not (album_col and asset_col):
                continue
            try:  # confirm it really joins the album table before trusting it
                self.conn.execute(
                    f'SELECT j."{asset_col}" FROM "{real}" j '
                    f'JOIN "{self.album_table}" a ON a.Z_PK = j."{album_col}" LIMIT 1'
                ).fetchone()
            except sqlite3.Error:
                continue
            return {"table": real, "album_col": album_col, "asset_col": asset_col}
        self.warnings.append("no album join table found; album names unavailable")
        return None

    def schema_report(self) -> dict:
        return {
            "database": str(self.db_path),
            "asset_table": self.asset_table,
            "asset_columns_found": self.fields,
            "asset_columns_missing": sorted(
                set(ASSET_FIELDS) - set(self.fields)
            ),
            "attribute_table": self.attribute_table,
            "attribute_columns_found": self.attribute_fields,
            "album_table": self.album_table,
            "album_join": self.album_join,
            "asset_count": self.count(),
            "warnings": list(self.warnings),
        }

    def count(self) -> int:
        try:
            return self.conn.execute(
                f'SELECT count(*) FROM "{self.asset_table}"'
            ).fetchone()[0]
        except sqlite3.Error:
            return -1

    # ------------------------------------------------------------- assets

    def _album_map(self) -> dict:
        """asset Z_PK -> sorted list of album titles."""
        if not self.album_join or not self.album_table:
            return {}
        album_cols = _columns(self.conn, self.album_table)
        title_col = album_cols.get("ZTITLE") or album_cols.get("ZNAME")
        if not title_col:
            return {}
        join = self.album_join
        sql = (
            f'SELECT j."{join["asset_col"]}" AS pk, a."{title_col}" AS title '
            f'FROM "{join["table"]}" j '
            f'JOIN "{self.album_table}" a ON a.Z_PK = j."{join["album_col"]}" '
            f'WHERE a."{title_col}" IS NOT NULL AND a."{title_col}" != ""'
        )
        mapping: dict = {}
        try:
            for row in self.conn.execute(sql):
                mapping.setdefault(row["pk"], set()).add(row["title"])
        except sqlite3.Error as exc:
            self.warnings.append(f"album lookup failed: {exc}")
            return {}
        return {pk: sorted(titles) for pk, titles in mapping.items()}

    def load(self) -> list:
        """Read every asset row into dicts and build the lookup indexes."""
        if self._assets:
            return self._assets

        fields = self.fields
        select = ['a.Z_PK AS pk']
        for key, column in fields.items():
            select.append(f'a."{column}" AS "{key}"')
        joins = ""
        if self.attribute_table and self.attribute_fk:
            for key, column in self.attribute_fields.items():
                select.append(f'x."{column}" AS "attr_{key}"')
            joins = (
                f' LEFT JOIN "{self.attribute_table}" x '
                f'ON x."{self.attribute_fk}" = a.Z_PK'
            )
        sql = f'SELECT {", ".join(select)} FROM "{self.asset_table}" a{joins}'

        albums = self._album_map()
        assets = []
        for row in self.conn.execute(sql):
            data = dict(row)
            kind = data.get("kind")
            directory = (data.get("directory") or "").strip("/")
            filename = data.get("filename") or ""
            relpath = (
                f"Media/{directory}/{filename}" if directory and filename else ""
            )
            asset = {
                "photos_pk": data.get("pk"),
                "photos_uuid": data.get("uuid") or "",
                "photos_filename": filename,
                "photos_directory": directory,
                "photos_relative_path": relpath,
                "photos_original_filename": data.get("attr_original_filename") or "",
                "photos_date_created": apple_timestamp(data.get("date_created")),
                "photos_date_added": apple_timestamp(data.get("date_added")),
                "photos_date_modified": apple_timestamp(data.get("date_modified")),
                "photos_trashed_date": apple_timestamp(data.get("trashed_date")),
                "photos_latitude": coordinate(data.get("latitude")),
                "photos_longitude": coordinate(data.get("longitude")),
                "photos_favorite": _as_bool(data.get("favorite")),
                "photos_hidden": _as_bool(data.get("hidden")),
                "photos_trashed": _as_bool(data.get("trashed")),
                "photos_kind": KIND_NAMES.get(kind, "" if kind is None else str(kind)),
                "photos_duration": data.get("duration"),
                "photos_width": data.get("width"),
                "photos_height": data.get("height"),
                "photos_timezone": data.get("attr_timezone_name") or "",
                "photos_albums": "; ".join(albums.get(data.get("pk"), [])),
                "photos_matched": False,
            }
            assets.append(asset)
            if relpath:
                self._by_relpath.setdefault(relpath.lower(), asset)
            for name in (filename, asset["photos_original_filename"]):
                if name:
                    self._by_filename.setdefault(name.lower(), asset)
        self._assets = assets
        return assets

    def lookup(self, relative_path: str, filename: str = "") -> Optional[dict]:
        """Match a Manifest.db camera-roll path to its Photos.sqlite row.

        Prefers the exact ``Media/DCIM/100APPLE/IMG_0001.HEIC`` path and falls
        back to the bare filename (which is unique in practice on a device).
        """
        self.load()
        asset = self._by_relpath.get((relative_path or "").lower())
        if asset is None:
            asset = self._by_filename.get(
                (filename or Path(relative_path or "").name).lower()
            )
        if asset is not None:
            asset["photos_matched"] = True
        return asset

    def unmatched(self) -> list:
        return [a for a in self.load() if not a["photos_matched"]]

    def close(self) -> None:
        self.conn.close()


def _as_bool(value):
    if value is None:
        return ""
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return bool(value)


PHOTOS_METADATA_COLUMNS = [
    "photos_uuid",
    "photos_filename",
    "photos_original_filename",
    "photos_date_created",
    "photos_date_added",
    "photos_date_modified",
    "photos_latitude",
    "photos_longitude",
    "photos_favorite",
    "photos_hidden",
    "photos_trashed",
    "photos_trashed_date",
    "photos_kind",
    "photos_duration",
    "photos_width",
    "photos_height",
    "photos_timezone",
    "photos_albums",
]
