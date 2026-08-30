"""Voice Memo titles/dates from CloudRecordings.db (best effort).

The audio files themselves come straight out of Manifest.db; this database
only supplies the human-readable title the user typed and the recording
date, and its schema is probed rather than assumed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from .util import apple_timestamp

VOICE_METADATA_COLUMNS = ["voice_title", "voice_date", "voice_duration"]


class RecordingsDB:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(f"file:{self.db_path}", uri=True, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.text_factory = lambda b: b.decode("utf-8", "replace")
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self.table, self.columns = self._find_recording_table()
        self._by_name: dict = {}
        self._rows: list = []

    def _find_recording_table(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        for (name,) in rows:
            cols = {
                r[1].upper(): r[1]
                for r in self.conn.execute(f'PRAGMA table_info("{name}")')
            }
            if "ZPATH" in cols and any(
                key in cols for key in ("ZCUSTOMLABEL", "ZENCRYPTEDTITLE", "ZTITLE")
            ):
                return name, cols
        return None, {}

    def schema_report(self) -> dict:
        return {
            "database": str(self.db_path),
            "recording_table": self.table,
            "columns": sorted(self.columns.values()),
        }

    def load(self) -> list:
        if self._rows or not self.table:
            return self._rows
        cols = self.columns
        title_col = (
            cols.get("ZCUSTOMLABEL") or cols.get("ZTITLE") or cols.get("ZENCRYPTEDTITLE")
        )
        select = [f'"{cols["ZPATH"]}" AS path']
        if title_col:
            select.append(f'"{title_col}" AS title')
        if "ZDATE" in cols:
            select.append(f'"{cols["ZDATE"]}" AS date')
        if "ZDURATION" in cols:
            select.append(f'"{cols["ZDURATION"]}" AS duration')
        try:
            rows = self.conn.execute(
                f'SELECT {", ".join(select)} FROM "{self.table}"'
            ).fetchall()
        except sqlite3.Error:
            return []
        out = []
        for row in rows:
            data = dict(row)
            path = data.get("path") or ""
            record = {
                "voice_title": (data.get("title") or "").strip(),
                "voice_date": apple_timestamp(data.get("date")),
                "voice_duration": data.get("duration"),
            }
            out.append(record)
            if path:
                self._by_name.setdefault(Path(path).name.lower(), record)
        self._rows = out
        return out

    def lookup(self, filename: str) -> Optional[dict]:
        self.load()
        return self._by_name.get(Path(filename or "").name.lower())

    def close(self) -> None:
        self.conn.close()
