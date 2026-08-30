#!/usr/bin/env python3
"""Inspect an unencrypted iTunes/Finder iPhone backup without extracting it.

Prints the backups it can find, and for a chosen backup dumps the *actual*
schema of Manifest.db, Photos.sqlite and sms.db, plus how many files sit in
each category the extractor cares about. Run this first: the extractor
discovers schema the same way, so whatever this prints is what it will use.

Read-only. No network access. Writes nothing except an optional --json report.

Examples:
    python3 inspect_backup.py --list
    python3 inspect_backup.py --backup 00008030-001
    python3 inspect_backup.py --backup ~/Desktop/SomeBackup --samples 5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from iphonebackup.backup import (
    DEFAULT_BACKUP_ROOT,
    Backup,
    BackupError,
    discover_backups,
    load_backup,
)
from iphonebackup.exifdata import available as exif_available
from iphonebackup.photos import PhotosLibrary
from iphonebackup.sms import MessagesDB
from iphonebackup.util import human_size
from iphonebackup.voicememos import RecordingsDB

INTERESTING_QUERIES = [
    ("camera roll (CameraRollDomain Media/DCIM/%)",
     {"domain": "CameraRollDomain", "path_like": "Media/DCIM/%"}),
    ("messages attachments (Library/SMS/Attachments/%)",
     {"path_like": "Library/SMS/Attachments/%"}),
    ("whatsapp (domain LIKE %whatsapp%)", {"domain_like": "%whatsapp%"}),
    ("voice memos (Library/Recordings/%)", {"path_like": "Library/Recordings/%"}),
    ("voice memos (AppDomain-com.apple.VoiceMemos)",
     {"domain_like": "AppDomain-com.apple.VoiceMemos%"}),
]

KEY_FILES = [
    ("Photos library", "CameraRollDomain", "Media/PhotoData/Photos.sqlite"),
    ("Photos WAL", "CameraRollDomain", "Media/PhotoData/Photos.sqlite-wal"),
    ("Photos SHM", "CameraRollDomain", "Media/PhotoData/Photos.sqlite-shm"),
    ("Messages", "HomeDomain", "Library/SMS/sms.db"),
    ("Messages WAL", "HomeDomain", "Library/SMS/sms.db-wal"),
    ("Voice memo index", "HomeDomain", "Library/Recordings/CloudRecordings.db"),
    ("Voice memo index (app domain)",
     "AppDomain-com.apple.VoiceMemos", "Recordings/CloudRecordings.db"),
]

RULE = "-" * 68


def header(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def list_backups(backups: list) -> None:
    if not backups:
        print("No backups found.")
        return
    print(f"Found {len(backups)} backup(s):\n")
    for index, backup in enumerate(backups, 1):
        print(f"[{index}] {backup.describe()}\n")


def choose_backup(backups: list) -> Backup:
    list_backups(backups)
    if len(backups) == 1:
        return backups[0]
    if not sys.stdin.isatty():
        raise BackupError("multiple backups found; pass --backup <udid>")
    while True:
        answer = input(f"Inspect which backup? [1-{len(backups)}]: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(backups):
            return backups[int(answer) - 1]
        print("Please enter a number from the list.")


def inspect(backup: Backup, samples: int) -> dict:
    report: dict = {
        "backup_path": str(backup.path),
        "udid": backup.udid,
        "device_name": backup.device_name,
        "product": backup.product_name,
        "ios_version": backup.ios_version,
        "last_backup_date": str(backup.last_backup_date),
        "is_encrypted": backup.is_encrypted,
    }

    header(f"BACKUP: {backup.device_name}")
    print(backup.describe())

    if backup.legacy_mbdb:
        print("\n!! This is a pre-iOS 10 backup (Manifest.mbdb). Not supported.")
        report["error"] = "legacy Manifest.mbdb backup"
        return report
    if backup.is_encrypted:
        print(
            "\n!! Manifest.plist reports IsEncrypted = true.\n"
            "   Encrypted backups cannot be read by this tool. Re-create the "
            "backup with\n   'Encrypt local backup' unchecked, or decrypt it "
            "first."
        )
        return report

    # ------------------------------------------------------------- Manifest
    header("Manifest.db -> Files schema (as actually present)")
    try:
        rows = backup.connect().execute("PRAGMA table_info(Files)").fetchall()
    except BackupError as exc:
        print(f"!! {exc}")
        report["error"] = str(exc)
        return report
    for row in rows:
        print(f"  {row['cid']:>2}  {row['name']:<20} {row['type'] or '?':<10}"
              f"{'  PK' if row['pk'] else ''}")
    report["manifest_files_columns"] = [dict(r) for r in rows]

    sql = backup.connect().execute(
        "SELECT sql FROM sqlite_master WHERE name='Files'"
    ).fetchone()
    if sql and sql[0]:
        print(f"\n  CREATE statement:\n    {sql[0]}")

    total = backup.connect().execute("SELECT count(*) FROM Files").fetchone()[0]
    print(f"\n  {total} rows in Files")
    report["file_count"] = total

    header("Top domains by file count")
    domains = backup.domains()
    for domain, count in domains[:25]:
        print(f"  {count:>8}  {domain}")
    report["domains"] = [{"domain": d, "count": c} for d, c in domains]

    header("Categories the extractor will pull from")
    report["categories"] = {}
    for label, query in INTERESTING_QUERIES:
        items = list(backup.iter_files(**query))
        size = sum(i.size or 0 for i in items)
        print(f"  {len(items):>7} file(s), {human_size(size):>10}  {label}")
        report["categories"][label] = {"count": len(items), "bytes": size}
        for item in items[:samples]:
            print(f"            e.g. {item.domain}: {item.relative_path}")
            present = "present" if item.stored_path else "MISSING FROM BACKUP"
            print(f"                 fileID {item.file_id} -> {present}")

    header("Key databases inside the backup")
    for label, domain, relative_path in KEY_FILES:
        item = backup.find_one(domain, relative_path)
        if item is None:
            print(f"  [ absent ] {label}: {domain}:{relative_path}")
            continue
        state = "on disk" if item.stored_path else "INDEXED BUT MISSING"
        print(
            f"  [ found  ] {label}: {relative_path} "
            f"({human_size(item.size or 0)}, {state})"
        )

    # ------------------------------------------------------------- Photos
    header("Photos.sqlite schema")
    photos_report = _inspect_photos(backup, samples)
    report["photos"] = photos_report

    # ---------------------------------------------------------------- sms
    header("sms.db schema")
    report["messages"] = _inspect_sms(backup, samples)

    # -------------------------------------------------------- voice memos
    header("CloudRecordings.db schema")
    report["recordings"] = _inspect_recordings(backup)

    header("EXIF support in this Python environment")
    print(f"  {exif_available()}")

    return report


def _copy_db(backup: Backup, domain: str, relative_path: str, name: str):
    """Pull a database plus its -wal/-shm out to a scratch dir."""
    import shutil
    import tempfile

    item = backup.find_one(domain, relative_path)
    if item is None or item.stored_path is None:
        return None
    work = Path(tempfile.mkdtemp(prefix="iphonebackup_inspect_"))
    target = work / name
    shutil.copy2(item.stored_path, target)
    for suffix in ("-wal", "-shm"):
        side = backup.find_one(domain, relative_path + suffix)
        if side is not None and side.stored_path is not None:
            shutil.copy2(side.stored_path, target.with_name(target.name + suffix))
            print(f"  (copied {name}{suffix} alongside; WAL will be checkpointed)")
    return target


def _inspect_photos(backup: Backup, samples: int) -> dict:
    path = _copy_db(
        backup, "CameraRollDomain", "Media/PhotoData/Photos.sqlite", "Photos.sqlite"
    )
    if path is None:
        print("  Photos.sqlite not present in this backup.")
        return {"present": False}
    try:
        library = PhotosLibrary(path)
    except Exception as exc:
        print(f"  !! could not open: {exc}")
        return {"present": True, "error": str(exc)}

    report = library.schema_report()
    report["present"] = True
    print(f"  asset table:      {report['asset_table']} ({report['asset_count']} rows)")
    print(f"  attribute table:  {report['attribute_table']}")
    print(f"  album table:      {report['album_table']}")
    print(f"  album join:       {report['album_join']}")
    print("\n  columns matched:")
    for key, column in sorted(report["asset_columns_found"].items()):
        print(f"    {key:<18} -> {column}")
    if report["asset_columns_missing"]:
        print("\n  columns NOT in this schema (left blank in metadata.csv):")
        for key in report["asset_columns_missing"]:
            print(f"    {key}")
    if report["attribute_columns_found"]:
        print("\n  ZADDITIONALASSETATTRIBUTES columns matched:")
        for key, column in sorted(report["attribute_columns_found"].items()):
            print(f"    {key:<18} -> {column}")
    for warning in report["warnings"]:
        print(f"  ! {warning}")

    assets = library.load()
    if assets and samples:
        print(f"\n  sample assets (first {min(samples, len(assets))}):")
        for asset in assets[:samples]:
            print(
                f"    {asset['photos_relative_path'] or asset['photos_filename']}"
                f"\n      created={asset['photos_date_created']} "
                f"fav={asset['photos_favorite']} hidden={asset['photos_hidden']} "
                f"trashed={asset['photos_trashed']} kind={asset['photos_kind']}"
                f"\n      gps=({asset['photos_latitude']}, "
                f"{asset['photos_longitude']}) albums={asset['photos_albums'] or '-'}"
            )
    library.close()
    return report


def _inspect_sms(backup: Backup, samples: int) -> dict:
    path = _copy_db(backup, "HomeDomain", "Library/SMS/sms.db", "sms.db")
    if path is None:
        print("  sms.db not present in this backup.")
        return {"present": False}
    try:
        db = MessagesDB(path)
    except Exception as exc:
        print(f"  !! could not open: {exc}")
        return {"present": True, "error": str(exc)}

    report = db.schema_report()
    report["present"] = True
    print(f"  tables: {', '.join(report['tables'])}")
    print(f"\n  attachment columns ({report['attachment_count']} rows):")
    print("    " + ", ".join(report["attachment_columns"]))
    for key in ("message_columns", "handle_columns", "chat_columns"):
        if key in report:
            print(f"\n  {key.replace('_', ' ')}:")
            print("    " + ", ".join(report[key]))
    rows = db.load()
    for warning in db.warnings:
        print(f"  ! {warning}")
    if rows and samples:
        print(f"\n  sample attachments (first {min(samples, len(rows))}):")
        for row in rows[:samples]:
            print(
                f"    {row['msg_relative_path']}\n"
                f"      name={row['msg_attachment_name']} "
                f"mime={row['msg_mime_type']} date={row['msg_date']} "
                f"{row['msg_direction']} via {row['msg_service']} "
                f"chat={row['msg_chat'] or '-'}"
            )
    db.close()
    return report


def _inspect_recordings(backup: Backup) -> dict:
    for domain, relative_path in (
        ("HomeDomain", "Library/Recordings/CloudRecordings.db"),
        ("AppDomain-com.apple.VoiceMemos", "Recordings/CloudRecordings.db"),
    ):
        path = _copy_db(backup, domain, relative_path, "CloudRecordings.db")
        if path is None:
            continue
        try:
            db = RecordingsDB(path)
        except Exception as exc:
            print(f"  !! could not open: {exc}")
            return {"present": True, "error": str(exc)}
        report = db.schema_report()
        report["present"] = True
        print(f"  recording table: {report['recording_table']}")
        print(f"  columns: {', '.join(report['columns'])}")
        print(f"  rows: {len(db.load())}")
        db.close()
        return report
    print("  CloudRecordings.db not present (voice memo titles unavailable).")
    return {"present": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect an unencrypted iPhone backup and dump its real schema.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=DEFAULT_BACKUP_ROOT,
        help=f"where backups live (default: {DEFAULT_BACKUP_ROOT})",
    )
    parser.add_argument(
        "--backup", help="UDID, UDID prefix, device name, or path to one backup"
    )
    parser.add_argument("--all", action="store_true", help="inspect every backup")
    parser.add_argument(
        "--list", action="store_true", help="only list the backups found, then exit"
    )
    parser.add_argument(
        "--samples", type=int, default=3, help="sample rows to print per section"
    )
    parser.add_argument(
        "--json", type=Path, help="also write the full report to this JSON file"
    )
    args = parser.parse_args(argv)

    try:
        if args.backup:
            targets = [load_backup(args.backup, args.backup_root)]
        else:
            found = discover_backups(args.backup_root)
            if args.list or not found:
                list_backups(found)
                if not found:
                    print(f"(looked in {args.backup_root})")
                return 0 if found else 1
            targets = found if args.all else [choose_backup(found)]
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    reports = []
    for backup in targets:
        with backup:
            try:
                reports.append(inspect(backup, args.samples))
            except BackupError as exc:
                print(f"error: {exc}", file=sys.stderr)
                reports.append({"udid": backup.udid, "error": str(exc)})

    if args.json:
        args.json.write_text(json.dumps(reports, indent=2, default=str))
        print(f"\nWrote report to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
