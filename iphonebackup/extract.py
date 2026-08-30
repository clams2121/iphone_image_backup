"""Extraction orchestration: copy media out of a backup and index its metadata.

Everything is local: files are copied from the backup folder into the chosen
output folder, and nothing is ever written outside it.
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .backup import Backup, BackupError, ManifestFile
from .exifdata import EXIF_COLUMNS, read_exif
from .photos import PHOTOS_METADATA_COLUMNS, PhotosLibrary
from .sms import MESSAGE_METADATA_COLUMNS, MessagesDB
from .util import (
    assert_within,
    human_size,
    kind_for_ext,
    sanitize_name,
    sha256_file,
    unique_path,
)
from .voicememos import VOICE_METADATA_COLUMNS, RecordingsDB

INDEX_FILENAME = ".extract_index.json"
METADATA_CSV = "metadata.csv"
METADATA_JSON = "metadata.json"

BASE_COLUMNS = [
    "source",
    "original_filename",
    "output_path",
    "media_type",
    "size_bytes",
    "sha256",
    "domain",
    "relative_path",
    "file_id",
    "backup_mtime",
    "device_name",
    "device_udid",
    "backup_date",
    "status",
]

CSV_COLUMNS = (
    BASE_COLUMNS
    + EXIF_COLUMNS
    + PHOTOS_METADATA_COLUMNS
    + MESSAGE_METADATA_COLUMNS
    + VOICE_METADATA_COLUMNS
)

# Camera roll paths queried by default.
DEFAULT_CAMERA_PATHS = ["Media/DCIM/%"]

CATEGORY_DIRS = {
    "camera_roll": "camera_roll",
    "messages": "message_attachments",
    "whatsapp": "message_attachments",
    "voice_memo": "voice_memos",
}


@dataclass
class Options:
    output: Path = Path("output")
    categories: tuple = ("camera_roll", "messages", "whatsapp", "voice_memo")
    camera_paths: list = field(default_factory=lambda: list(DEFAULT_CAMERA_PATHS))
    write_json: bool = False
    dry_run: bool = False
    global_dedup: bool = False
    include_message_text: bool = False
    quiet: bool = False


@dataclass
class Summary:
    device: str = ""
    counts: Counter = field(default_factory=Counter)
    skipped_duplicates: int = 0
    bytes_written: int = 0
    errors: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    unmatched_photos: int = 0
    unmatched_attachments: int = 0
    metadata_path: Optional[Path] = None


class Extractor:
    def __init__(self, backup: Backup, options: Options):
        self.backup = backup
        self.opt = options
        self.output_root = Path(options.output).expanduser().resolve()
        self.device_dir = self.output_root / backup.output_dirname
        self.summary = Summary(device=backup.device_name)
        self.rows: list = []
        self._work = Path(tempfile.mkdtemp(prefix="iphonebackup_dbs_"))
        self._index_path = (
            self.output_root / INDEX_FILENAME
            if options.global_dedup
            else self.device_dir / INDEX_FILENAME
        )
        self._index: dict = {}
        self.photos: Optional[PhotosLibrary] = None
        self.messages: Optional[MessagesDB] = None
        self.recordings: Optional[RecordingsDB] = None

    # ------------------------------------------------------------ plumbing

    def log(self, message: str) -> None:
        if not self.opt.quiet:
            print(message, flush=True)

    def _load_index(self) -> None:
        if self._index_path.exists():
            try:
                self._index = json.loads(self._index_path.read_text())
            except (OSError, ValueError):
                self._index = {}
                self.summary.notes.append(
                    f"could not read dedup index {self._index_path}; starting fresh"
                )
        # Drop entries whose file has since been deleted so they re-extract.
        base = self._index_path.parent
        self._index = {
            digest: rel
            for digest, rel in self._index.items()
            if (base / rel).exists()
        }

    def _save_index(self) -> None:
        if self.opt.dry_run:
            return
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        assert_within(self.output_root, self._index_path)
        self._index_path.write_text(json.dumps(self._index, indent=1, sort_keys=True))

    def _extract_db(self, name: str, candidates: Iterable) -> Optional[Path]:
        """Copy a database (plus its -wal/-shm siblings) into the scratch dir."""
        for domain, relative_path in candidates:
            item = self.backup.find_one(domain, relative_path)
            if item is None or item.stored_path is None:
                continue
            target = self._work / name
            shutil.copy2(item.stored_path, target)
            for suffix in ("-wal", "-shm"):
                side = self.backup.find_one(domain, relative_path + suffix)
                if side is not None and side.stored_path is not None:
                    shutil.copy2(
                        side.stored_path, target.with_name(target.name + suffix)
                    )
            return target
        return None

    def _open_support_databases(self) -> None:
        photos_db = self._extract_db(
            "Photos.sqlite",
            [
                ("CameraRollDomain", "Media/PhotoData/Photos.sqlite"),
                ("CameraRollDomain", "Media/PhotoData/Photos.sqlite.backup"),
            ],
        )
        if photos_db:
            try:
                self.photos = PhotosLibrary(photos_db)
                self.photos.load()
                self.log(
                    f"  Photos.sqlite: {self.photos.asset_table} with "
                    f"{self.photos.count()} assets"
                )
                for warning in self.photos.warnings:
                    self.summary.notes.append(f"Photos.sqlite: {warning}")
            except Exception as exc:
                self.summary.notes.append(f"Photos.sqlite unreadable: {exc}")
        else:
            self.summary.notes.append(
                "Photos.sqlite not present in this backup; camera roll rows will "
                "have no album/favorite/hidden metadata"
            )

        sms_db = self._extract_db(
            "sms.db",
            [("HomeDomain", "Library/SMS/sms.db")],
        )
        if sms_db:
            try:
                self.messages = MessagesDB(
                    sms_db, include_text=self.opt.include_message_text
                )
                self.messages.load()
                self.log(
                    f"  sms.db: {len(self.messages.load())} attachment rows"
                )
                for warning in self.messages.warnings:
                    self.summary.notes.append(f"sms.db: {warning}")
            except Exception as exc:
                self.summary.notes.append(f"sms.db unreadable: {exc}")
        else:
            self.summary.notes.append("sms.db not present in this backup")

        rec_db = self._extract_db(
            "CloudRecordings.db",
            [
                ("HomeDomain", "Library/Recordings/CloudRecordings.db"),
                (
                    "AppDomain-com.apple.VoiceMemos",
                    "Recordings/CloudRecordings.db",
                ),
            ],
        )
        if rec_db:
            try:
                self.recordings = RecordingsDB(rec_db)
                self.recordings.load()
            except Exception as exc:
                self.summary.notes.append(f"CloudRecordings.db unreadable: {exc}")

    # ------------------------------------------------------------ querying

    def _camera_roll_items(self) -> Iterable[ManifestFile]:
        seen = set()
        for pattern in self.opt.camera_paths:
            for item in self.backup.iter_files(
                domain="CameraRollDomain", path_like=pattern
            ):
                if item.file_id in seen:
                    continue
                if kind_for_ext(item.name) in ("image", "video"):
                    seen.add(item.file_id)
                    yield item

    def _message_items(self) -> Iterable[ManifestFile]:
        for item in self.backup.iter_files(path_like="Library/SMS/Attachments/%"):
            if kind_for_ext(item.name) in ("image", "video"):
                yield item

    def _whatsapp_items(self) -> Iterable[ManifestFile]:
        for item in self.backup.iter_files(domain_like="%whatsapp%"):
            path = item.relative_path
            if kind_for_ext(item.name) not in ("image", "video"):
                continue
            if "/Profile/" in path or path.lower().endswith(".thumb"):
                continue
            if "Media/" not in path:
                continue
            yield item

    def _voice_memo_items(self) -> Iterable[ManifestFile]:
        seen = set()
        queries = [
            {"path_like": "Library/Recordings/%"},
            {"domain_like": "AppDomain-com.apple.VoiceMemos%"},
            {"domain_like": "AppDomainGroup-group.com.apple.VoiceMemos%"},
        ]
        for query in queries:
            try:
                items = list(self.backup.iter_files(**query))
            except BackupError:
                continue
            for item in items:
                if item.file_id in seen:
                    continue
                if kind_for_ext(item.name) != "audio":
                    continue
                seen.add(item.file_id)
                yield item

    # ----------------------------------------------------------- extraction

    def run(self) -> Summary:
        if self.backup.is_encrypted:
            raise BackupError("backup is encrypted")
        self.log(f"\nDevice: {self.backup.device_name} ({self.backup.udid})")
        self.log(f"  iOS {self.backup.ios_version}, last backup {self.backup.last_backup_date}")

        self._load_index()
        self._open_support_databases()

        plan = [
            ("camera_roll", self._camera_roll_items),
            ("messages", self._message_items),
            ("whatsapp", self._whatsapp_items),
            ("voice_memo", self._voice_memo_items),
        ]
        for category, producer in plan:
            if category not in self.opt.categories:
                continue
            self.log(f"  scanning {category}...")
            count = 0
            for item in producer():
                if self._handle(category, item):
                    count += 1
            self.log(f"    {category}: {count} file(s) indexed")

        if self.photos is not None:
            self.summary.unmatched_photos = len(self.photos.unmatched())
        if self.messages is not None:
            self.summary.unmatched_attachments = len(
                [
                    r
                    for r in self.messages.unmatched()
                    if r["msg_relative_path"]
                    and kind_for_ext(r["msg_relative_path"]) in ("image", "video")
                ]
            )

        self._write_metadata()
        self._save_index()
        self.cleanup()
        return self.summary

    def _target_dir(self, category: str) -> Path:
        return self.device_dir / CATEGORY_DIRS[category]

    def _handle(self, category: str, item: ManifestFile) -> bool:
        source = item.stored_path
        if source is None:
            self.summary.errors.append(
                f"{category}: content missing for {item.relative_path} "
                f"(fileID {item.file_id})"
            )
            return False

        try:
            digest = sha256_file(source)
        except OSError as exc:
            self.summary.errors.append(f"{category}: cannot read {source}: {exc}")
            return False

        original_name = self._original_name(category, item)
        target_dir = self._target_dir(category)
        status = "extracted"
        existing = self._index.get(digest)
        if existing is not None:
            out_path = (self._index_path.parent / existing).resolve()
            status = "duplicate"
            self.summary.skipped_duplicates += 1
        else:
            out_path = unique_path(target_dir, sanitize_name(original_name))
            assert_within(self.output_root, out_path)
            if not self.opt.dry_run:
                try:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, out_path)
                except OSError as exc:
                    self.summary.errors.append(
                        f"{category}: copy failed for {item.relative_path}: {exc}"
                    )
                    return False
                self.summary.bytes_written += out_path.stat().st_size
            self._index[digest] = str(
                out_path.relative_to(self._index_path.parent)
            )

        self.summary.counts[category] += 1
        self.rows.append(
            self._build_row(category, item, out_path, digest, original_name, status)
        )
        return True

    def _original_name(self, category: str, item: ManifestFile) -> str:
        """Best available human name, falling back to the on-device filename."""
        name = item.name
        if category == "camera_roll" and self.photos is not None:
            asset = self.photos.lookup(item.relative_path, name)
            if asset:
                return asset.get("photos_filename") or name
        if category == "messages" and self.messages is not None:
            record = self.messages.lookup(item.relative_path)
            if record:
                return record.get("msg_attachment_name") or name
        return name

    def _build_row(
        self,
        category: str,
        item: ManifestFile,
        out_path: Path,
        digest: str,
        original_name: str,
        status: str,
    ) -> dict:
        row = {column: "" for column in CSV_COLUMNS}
        try:
            size = item.size if item.size is not None else out_path.stat().st_size
        except OSError:
            size = item.size or ""
        row.update(
            {
                "source": category,
                "original_filename": original_name,
                "output_path": str(
                    out_path.relative_to(self.output_root)
                    if self.output_root in out_path.parents
                    else out_path
                ),
                "media_type": kind_for_ext(item.name),
                "size_bytes": size,
                "sha256": digest,
                "domain": item.domain,
                "relative_path": item.relative_path,
                "file_id": item.file_id,
                "backup_mtime": item.modified_iso,
                "device_name": self.backup.device_name,
                "device_udid": self.backup.udid,
                "backup_date": str(self.backup.last_backup_date),
                "status": status,
            }
        )

        if out_path.exists() and not self.opt.dry_run:
            for key, value in read_exif(out_path).items():
                row[key] = value

        if category == "camera_roll" and self.photos is not None:
            asset = self.photos.lookup(item.relative_path, item.name)
            if asset:
                for key in PHOTOS_METADATA_COLUMNS:
                    value = asset.get(key)
                    row[key] = "" if value is None else value
            elif len(self.summary.notes) < 200:
                self.summary.notes.append(
                    f"no Photos.sqlite row for {item.relative_path}"
                )

        if category in ("messages", "whatsapp") and self.messages is not None:
            record = self.messages.lookup(item.relative_path)
            if record:
                for key in MESSAGE_METADATA_COLUMNS:
                    value = record.get(key)
                    row[key] = "" if value is None else value
                if self.opt.include_message_text:
                    row["msg_text"] = record.get("msg_text", "")

        if category == "voice_memo" and self.recordings is not None:
            record = self.recordings.lookup(item.name)
            if record:
                for key in VOICE_METADATA_COLUMNS:
                    value = record.get(key)
                    row[key] = "" if value is None else value

        return row

    # ------------------------------------------------------------- output

    def _write_metadata(self) -> None:
        columns = list(CSV_COLUMNS)
        if self.opt.include_message_text and "msg_text" not in columns:
            columns.append("msg_text")

        csv_path = self.device_dir / METADATA_CSV
        merged = self._merge_with_existing(csv_path, columns)
        self.summary.metadata_path = csv_path
        if self.opt.dry_run:
            return

        self.device_dir.mkdir(parents=True, exist_ok=True)
        assert_within(self.output_root, csv_path)
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in merged:
                writer.writerow(row)

        if self.opt.write_json:
            json_path = self.device_dir / METADATA_JSON
            assert_within(self.output_root, json_path)
            json_path.write_text(json.dumps(merged, indent=1, default=str))

    @staticmethod
    def _row_key(row: dict) -> str:
        """Identity of a source file: one row per backed-up file, not per
        distinct content hash -- two copies of the same photo are two files
        with their own paths and their own Photos.sqlite rows."""
        file_id = row.get("file_id") or ""
        if file_id:
            return f"{row.get('device_udid', '')}|{file_id}"
        return f"sha|{row.get('sha256', '')}|{row.get('relative_path', '')}"

    def _merge_with_existing(self, csv_path: Path, columns: list) -> list:
        """Keep rows from previous runs so the index stays cumulative."""
        previous: dict = {}
        if csv_path.exists():
            try:
                with csv_path.open(newline="", encoding="utf-8") as handle:
                    for row in csv.DictReader(handle):
                        previous[self._row_key(row)] = row
            except OSError as exc:
                self.summary.notes.append(f"could not read existing metadata: {exc}")
        for row in self.rows:
            previous[self._row_key(row)] = row
        return sorted(
            previous.values(),
            key=lambda r: (
                r.get("source", ""),
                r.get("original_filename", ""),
                r.get("relative_path", ""),
            ),
        )

    def cleanup(self) -> None:
        for db in (self.photos, self.messages, self.recordings):
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass
        shutil.rmtree(self._work, ignore_errors=True)


def print_summary(summaries: list) -> None:
    print("\n" + "=" * 68)
    print("SUMMARY")
    print("=" * 68)
    total_errors = 0
    for summary in summaries:
        print(f"\n{summary.device}")
        for category in ("camera_roll", "messages", "whatsapp", "voice_memo"):
            print(f"  {category:<16} {summary.counts.get(category, 0):>7}")
        print(f"  {'total':<16} {sum(summary.counts.values()):>7}")
        print(f"  duplicates skipped: {summary.skipped_duplicates}")
        print(f"  bytes written:      {human_size(summary.bytes_written)}")
        if summary.unmatched_photos:
            print(
                f"  Photos.sqlite rows with no extracted file: "
                f"{summary.unmatched_photos}"
            )
        if summary.unmatched_attachments:
            print(
                f"  sms.db attachments with no extracted file: "
                f"{summary.unmatched_attachments}"
            )
        if summary.metadata_path:
            print(f"  metadata: {summary.metadata_path}")
        notes = summary.notes[:10]
        if notes:
            print("  notes:")
            for note in notes:
                print(f"    - {note}")
            if len(summary.notes) > len(notes):
                print(f"    ... and {len(summary.notes) - len(notes)} more")
        if summary.errors:
            total_errors += len(summary.errors)
            print(f"  errors ({len(summary.errors)}):")
            for error in summary.errors[:10]:
                print(f"    - {error}")
            if len(summary.errors) > 10:
                print(f"    ... and {len(summary.errors) - 10} more")
    if total_errors:
        print(f"\nFinished with {total_errors} error(s).", file=sys.stderr)
    else:
        print("\nFinished with no errors.")
