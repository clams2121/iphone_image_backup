"""Adaptive reader for sms.db (Messages / iMessage) attachment metadata."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from .util import apple_timestamp

ATTACHMENT_PREFIXES = ("Library/SMS/Attachments/",)


def _tables(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0].upper(): row[0] for row in rows}


def _columns(conn: sqlite3.Connection, table: str) -> dict:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {row[1].upper(): row[1] for row in rows}


def normalize_attachment_path(raw: str) -> str:
    """Turn an ``attachment.filename`` value into a Manifest relativePath.

    Values look like ``~/Library/SMS/Attachments/0a/10/GUID/IMG_0001.HEIC``
    or ``/var/mobile/Library/SMS/Attachments/...``; Manifest.db stores
    ``Library/SMS/Attachments/0a/10/GUID/IMG_0001.HEIC``.
    """
    if not raw:
        return ""
    path = raw.replace("\\", "/")
    for prefix in ATTACHMENT_PREFIXES:
        index = path.find(prefix)
        if index != -1:
            return path[index:]
    path = path.lstrip("~").lstrip("/")
    if path.startswith("var/mobile/"):
        path = path[len("var/mobile/"):]
    return path


class MessagesDB:
    """Indexes sms.db attachments by their on-device relative path."""

    def __init__(self, db_path: Path, include_text: bool = False):
        self.db_path = Path(db_path)
        self.include_text = include_text
        self.conn = sqlite3.connect(f"file:{self.db_path}", uri=True, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.text_factory = lambda b: b.decode("utf-8", "replace")
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass

        self.tables = _tables(self.conn)
        self.warnings: list = []
        self.attachment_table = self.tables.get("ATTACHMENT")
        if not self.attachment_table:
            raise ValueError(f"no attachment table in {self.db_path.name}")
        self.attachment_columns = _columns(self.conn, self.attachment_table)
        self._by_path: dict = {}
        self._by_name: dict = {}
        self._rows: list = []

    # ------------------------------------------------------------ schema

    def schema_report(self) -> dict:
        report = {
            "database": str(self.db_path),
            "tables": sorted(self.tables.values()),
            "attachment_columns": sorted(self.attachment_columns.values()),
            "attachment_count": self._count(self.attachment_table),
            "has_message_attachment_join": "MESSAGE_ATTACHMENT_JOIN" in self.tables,
            "has_chat_tables": "CHAT" in self.tables
            and "CHAT_MESSAGE_JOIN" in self.tables,
            "warnings": list(self.warnings),
        }
        for table in ("MESSAGE", "HANDLE", "CHAT"):
            real = self.tables.get(table)
            if real:
                report[f"{table.lower()}_columns"] = sorted(
                    _columns(self.conn, real).values()
                )
        return report

    def _count(self, table: Optional[str]) -> int:
        if not table:
            return 0
        try:
            return self.conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        except sqlite3.Error:
            return -1

    # ------------------------------------------------------- attachments

    def load(self) -> list:
        if self._rows:
            return self._rows

        acols = self.attachment_columns
        filename_col = acols.get("FILENAME")
        if not filename_col:
            raise ValueError("attachment table has no filename column")

        select = ["a.ROWID AS attachment_id", f'a."{filename_col}" AS filename']
        for key, candidates in (
            ("mime_type", ("MIME_TYPE",)),
            ("transfer_name", ("TRANSFER_NAME",)),
            ("total_bytes", ("TOTAL_BYTES",)),
            ("created_date", ("CREATED_DATE",)),
            ("is_sticker", ("IS_STICKER",)),
        ):
            for candidate in candidates:
                if candidate in acols:
                    select.append(f'a."{acols[candidate]}" AS {key}')
                    break

        joins = ""
        message_table = self.tables.get("MESSAGE")
        join_table = self.tables.get("MESSAGE_ATTACHMENT_JOIN")
        if message_table and join_table:
            mcols = _columns(self.conn, message_table)
            jcols = _columns(self.conn, join_table)
            j_att = jcols.get("ATTACHMENT_ID")
            j_msg = jcols.get("MESSAGE_ID")
            if j_att and j_msg:
                joins += (
                    f' LEFT JOIN "{join_table}" j ON j."{j_att}" = a.ROWID'
                    f' LEFT JOIN "{message_table}" m ON m.ROWID = j."{j_msg}"'
                )
                select.append("m.ROWID AS message_rowid")
                for key, candidate in (
                    ("msg_guid", "GUID"),
                    ("msg_date_raw", "DATE"),
                    ("msg_is_from_me", "IS_FROM_ME"),
                    ("msg_service", "SERVICE"),
                ):
                    if candidate in mcols:
                        select.append(f'm."{mcols[candidate]}" AS {key}')
                if self.include_text and "TEXT" in mcols:
                    select.append(f'm."{mcols["TEXT"]}" AS msg_text')
                handle_table = self.tables.get("HANDLE")
                if handle_table and "HANDLE_ID" in mcols:
                    hcols = _columns(self.conn, handle_table)
                    if "ID" in hcols:
                        joins += (
                            f' LEFT JOIN "{handle_table}" h '
                            f'ON h.ROWID = m."{mcols["HANDLE_ID"]}"'
                        )
                        select.append(f'h."{hcols["ID"]}" AS msg_handle')
            else:
                self.warnings.append(
                    "message_attachment_join lacks expected columns; "
                    "attachments will have no message context"
                )
        else:
            self.warnings.append(
                "no message/message_attachment_join tables; "
                "attachments will have no message context"
            )

        chat_map = self._chat_map()
        sql = f'SELECT {", ".join(select)} FROM "{self.attachment_table}" a{joins}'
        rows = []
        for row in self.conn.execute(sql):
            data = dict(row)
            relpath = normalize_attachment_path(data.get("filename") or "")
            record = {
                "source_db": "messages",
                "msg_relative_path": relpath,
                "msg_attachment_name": data.get("transfer_name")
                or Path(relpath).name,
                "msg_mime_type": data.get("mime_type") or "",
                "msg_total_bytes": data.get("total_bytes"),
                "msg_guid": data.get("msg_guid") or "",
                "msg_date": apple_timestamp(data.get("msg_date_raw"), "auto")
                or apple_timestamp(data.get("created_date"), "auto"),
                "msg_direction": _direction(data.get("msg_is_from_me")),
                "msg_service": data.get("msg_service") or "",
                "msg_handle": data.get("msg_handle") or "",
                "msg_chat": chat_map.get(data.get("message_rowid"), ""),
                "msg_matched": False,
            }
            if self.include_text:
                record["msg_text"] = (data.get("msg_text") or "").replace("\n", " ")
            rows.append(record)
            if relpath:
                self._by_path.setdefault(relpath.lower(), record)
                self._by_name.setdefault(Path(relpath).name.lower(), record)
        self._rows = rows
        return rows

    def _chat_map(self) -> dict:
        """message ROWID -> chat display name or identifier."""
        chat = self.tables.get("CHAT")
        cmj = self.tables.get("CHAT_MESSAGE_JOIN")
        if not (chat and cmj):
            return {}
        ccols = _columns(self.conn, chat)
        jcols = _columns(self.conn, cmj)
        name_col = ccols.get("DISPLAY_NAME")
        ident_col = ccols.get("CHAT_IDENTIFIER") or ccols.get("GUID")
        j_chat = jcols.get("CHAT_ID")
        j_msg = jcols.get("MESSAGE_ID")
        if not (ident_col and j_chat and j_msg):
            return {}
        label = (
            f'COALESCE(NULLIF(c."{name_col}", ""), c."{ident_col}")'
            if name_col
            else f'c."{ident_col}"'
        )
        try:
            rows = self.conn.execute(
                f'SELECT j."{j_msg}" AS mid, {label} AS label '
                f'FROM "{cmj}" j JOIN "{chat}" c ON c.ROWID = j."{j_chat}"'
            ).fetchall()
        except sqlite3.Error as exc:
            self.warnings.append(f"chat lookup failed: {exc}")
            return {}
        return {row["mid"]: row["label"] or "" for row in rows}

    def lookup(self, relative_path: str) -> Optional[dict]:
        self.load()
        record = self._by_path.get((relative_path or "").lower())
        if record is None:
            record = self._by_name.get(Path(relative_path or "").name.lower())
        if record is not None:
            record["msg_matched"] = True
        return record

    def unmatched(self) -> list:
        return [r for r in self.load() if not r["msg_matched"]]

    def close(self) -> None:
        self.conn.close()


def _direction(is_from_me) -> str:
    if is_from_me is None:
        return ""
    try:
        return "sent" if int(is_from_me) else "received"
    except (TypeError, ValueError):
        return ""


MESSAGE_METADATA_COLUMNS = [
    "msg_attachment_name",
    "msg_mime_type",
    "msg_guid",
    "msg_date",
    "msg_direction",
    "msg_service",
    "msg_handle",
    "msg_chat",
]
