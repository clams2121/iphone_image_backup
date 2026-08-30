#!/usr/bin/env python3
"""Extract photos, videos, message attachments and voice memos from
unencrypted iTunes/Finder iPhone backups, with a full metadata index.

Output layout:

    output/
      <device_name>_<udid8>/
        camera_roll/
        message_attachments/
        voice_memos/
        metadata.csv
        .extract_index.json     (content hashes, for cross-run dedup)

Everything is local: no network calls, and nothing is written outside the
--output directory.

Examples:
    python3 extract_backup.py --list
    python3 extract_backup.py --backup 00008030-001 --output ~/Pictures/iphone
    python3 extract_backup.py --all --json
    python3 extract_backup.py --backup MyPhone --only camera_roll --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from iphonebackup.backup import (
    DEFAULT_BACKUP_ROOT,
    BackupError,
    discover_backups,
    load_backup,
)
from iphonebackup.exifdata import available as exif_available
from iphonebackup.extract import (
    DEFAULT_CAMERA_PATHS,
    Extractor,
    Options,
    print_summary,
)

CATEGORIES = ("camera_roll", "messages", "whatsapp", "voice_memo")


def list_backups(backups: list) -> None:
    if not backups:
        print("No backups found.")
        return
    print(f"Found {len(backups)} backup(s):\n")
    for index, backup in enumerate(backups, 1):
        print(f"[{index}] {backup.describe()}\n")


def choose_backups(backups: list) -> list:
    list_backups(backups)
    if len(backups) == 1:
        return backups
    if not sys.stdin.isatty():
        raise BackupError("multiple backups found; pass --backup <udid> or --all")
    while True:
        answer = input(
            f"Extract which backup? [1-{len(backups)}, or 'all']: "
        ).strip().lower()
        if answer == "all":
            return backups
        if answer.isdigit() and 1 <= int(answer) <= len(backups):
            return [backups[int(answer) - 1]]
        print("Please enter a number from the list, or 'all'.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=DEFAULT_BACKUP_ROOT,
        help=f"where backups live (default: {DEFAULT_BACKUP_ROOT})",
    )
    parser.add_argument(
        "--backup",
        action="append",
        default=[],
        help="UDID, UDID prefix, device name, or path (repeatable)",
    )
    parser.add_argument("--all", action="store_true", help="process every backup found")
    parser.add_argument("--list", action="store_true", help="list backups and exit")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output"),
        help="output directory (default: ./output)",
    )
    parser.add_argument(
        "--only",
        action="append",
        choices=CATEGORIES,
        default=[],
        help="restrict to these categories (repeatable)",
    )
    parser.add_argument(
        "--skip",
        action="append",
        choices=CATEGORIES,
        default=[],
        help="skip these categories (repeatable)",
    )
    parser.add_argument(
        "--camera-path",
        action="append",
        default=[],
        help=(
            "extra CameraRollDomain path pattern to include, SQL LIKE syntax "
            "(default: "
            + ", ".join(DEFAULT_CAMERA_PATHS).replace("%", "%%")
            + ")"
        ),
    )
    parser.add_argument(
        "--json", action="store_true", help="also write metadata.json"
    )
    parser.add_argument(
        "--global-dedup",
        action="store_true",
        help="dedupe across all devices instead of per device",
    )
    parser.add_argument(
        "--include-message-text",
        action="store_true",
        help="include the message body text in the metadata (off by default)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be extracted without copying or writing anything",
    )
    parser.add_argument("--quiet", action="store_true", help="less progress output")
    args = parser.parse_args(argv)

    try:
        if args.backup:
            targets = [load_backup(name, args.backup_root) for name in args.backup]
        else:
            found = discover_backups(args.backup_root)
            if args.list or not found:
                list_backups(found)
                if not found:
                    print(f"(looked in {args.backup_root})")
                return 0 if found else 1
            targets = found if args.all else choose_backups(found)
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    categories = tuple(args.only) if args.only else CATEGORIES
    categories = tuple(c for c in categories if c not in args.skip)
    if not categories:
        print("error: every category was skipped; nothing to do", file=sys.stderr)
        return 1

    options = Options(
        output=args.output,
        categories=categories,
        camera_paths=list(DEFAULT_CAMERA_PATHS) + args.camera_path,
        write_json=args.json,
        dry_run=args.dry_run,
        global_dedup=args.global_dedup,
        include_message_text=args.include_message_text,
        quiet=args.quiet,
    )

    if not args.quiet:
        print(f"Output directory: {Path(args.output).expanduser().resolve()}")
        print(f"Categories:       {', '.join(categories)}")
        print(f"EXIF support:     {exif_available()}")
        if args.dry_run:
            print("DRY RUN: nothing will be copied or written.")

    summaries = []
    skipped = []
    for backup in targets:
        with backup:
            if backup.legacy_mbdb:
                skipped.append(
                    f"{backup.device_name} ({backup.udid}): pre-iOS 10 "
                    "Manifest.mbdb backup, not supported"
                )
                continue
            if backup.is_encrypted:
                skipped.append(
                    f"{backup.device_name} ({backup.udid}): backup is encrypted "
                    "(Manifest.plist IsEncrypted = true) -- skipped"
                )
                continue
            try:
                summaries.append(Extractor(backup, options).run())
            except BackupError as exc:
                skipped.append(f"{backup.device_name} ({backup.udid}): {exc}")
            except KeyboardInterrupt:
                print("\nInterrupted.", file=sys.stderr)
                return 130

    if skipped:
        print("\nSkipped:")
        for message in skipped:
            print(f"  - {message}")

    if summaries:
        print_summary(summaries)
        return 0
    print("Nothing was extracted.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
