# iphone_image_backup

Extract photos, videos, message attachments and voice memos — with their
metadata — out of unencrypted iTunes/Finder iPhone backups.

Everything runs locally: no network calls, nothing written outside the output
directory you choose, and the backup itself is only ever read.

## Install

Requires macOS or Linux and Python 3.10+.

```bash
git clone https://github.com/clams2121/iphone_image_backup
cd iphone_image_backup
pip install -r requirements.txt   # optional: Pillow + exifread, for the EXIF columns
```

On macOS, Terminal needs **Full Disk Access** (System Settings → Privacy &
Security) to read `~/Library/Application Support/MobileSync/Backup/`.

## Use

**1. Inspect first.** Photos.sqlite and sms.db table names shift between iOS
releases, so start by dumping what your backup actually contains:

```bash
python3 inspect_backup.py --list              # what backups do I have?
python3 inspect_backup.py --backup <udid>     # schema + file counts for one
```

This is read-only and copies nothing.

**2. Extract.**

```bash
python3 extract_backup.py --backup <udid> --output ~/Pictures/iphone --dry-run
python3 extract_backup.py --backup <udid> --output ~/Pictures/iphone
```

Both scripts accept a UDID, a UDID prefix, a device name, or a path; with no
`--backup` they list what they find and prompt.

Useful flags for `extract_backup.py`:

| flag | effect |
|---|---|
| `--all` | process every backup found |
| `--only` / `--skip` | limit to `camera_roll`, `messages`, `whatsapp`, `voice_memo` |
| `--dry-run` | report what would be extracted, write nothing |
| `--json` | also write `metadata.json` |
| `--include-message-text` | include message bodies in the metadata (off by default) |
| `--global-dedup` | share one dedup index across devices instead of per device |
| `--camera-path 'Media/PhotoData/CPLAssets/%'` | add a `CameraRollDomain` path pattern |
| `--backup-root DIR` | read backups from somewhere other than the default |

## Output

```
output/<device>/
  camera_roll/            photos and videos from Media/DCIM
  message_attachments/    image/video attachments from Messages and WhatsApp
  voice_memos/            .m4a recordings
  metadata.csv            one row per backed-up file
  .extract_index.json     content hashes, for dedup across runs
```

Files keep their original names (`IMG_0001.HEIC`, `trail.mov`), not the SHA-1
hashes the backup stores them under.

`metadata.csv` has one row per file, combining:

- **provenance** — source category, output path, size, SHA-256, backup domain
  and on-device path, device name, backup date
- **EXIF** read from the copied file — timestamp, camera make/model/lens, GPS
- **Photos.sqlite**, for camera roll — album names, favorite, hidden, trashed,
  creation/added dates, location, kind, duration
- **sms.db**, for attachments — chat name, sender handle, direction, service, date
- **CloudRecordings.db**, for voice memos — the title you typed, date, duration

Timestamps are ISO-8601 UTC. Columns your iOS version doesn't have are left
blank rather than failing the run. The CSV is cumulative — later runs merge
into the rows already there, so several backups of one phone build one index.

## Behaviour worth knowing

- **Encrypted backups are skipped**, not failed: `Manifest.plist` is checked
  first and the run continues with the others. To use one, turn off "Encrypt
  local backup" in Finder and take a fresh backup.
- **Dedup is by content hash.** Re-running against a newer backup of the same
  phone copies only what's new. Duplicates still get their own metadata row,
  marked `duplicate` and pointing at the single stored copy.
- **Schemas are discovered, not hardcoded** — `ZASSET` vs `ZGENERICASSET`, the
  per-build album join table (`Z_28ASSETS`, `Z_17ASSETS`, …), and Manifest's
  columns are all probed at run time.
- **Non-media attachments** (PDFs, vCards) and WhatsApp thumbnails/profile
  pictures are skipped on purpose.
- **iCloud "Optimize iPhone Storage"** keeps full-resolution originals out of
  the backup entirely. The summary reports how many `Photos.sqlite` rows had
  no matching file, so you'll see it rather than silently getting fewer photos.
- Pre-iOS 10 backups (`Manifest.mbdb`) are detected and skipped.

## Tests

The suite builds a synthetic backup — Manifest.db with NSKeyedArchiver file
blobs, the fileID fanout layout, a Photos.sqlite and sms.db, an encrypted
second device — and runs the real code against it.

```bash
python3 -m unittest discover -s tests -v
```

To try the tools without a phone:

```bash
python3 tools/make_test_backup.py /tmp/fake_backups
python3 inspect_backup.py --backup-root /tmp/fake_backups --backup 00008110
python3 extract_backup.py --backup-root /tmp/fake_backups --all --output /tmp/out
```

## Layout

```
inspect_backup.py            schema inspection CLI
extract_backup.py            extraction CLI
iphonebackup/
  backup.py                  backup discovery, Manifest.db, fileID fanout
  photos.py                  Photos.sqlite reader
  sms.py                     sms.db reader
  voicememos.py              CloudRecordings.db reader
  exifdata.py                EXIF via exifread, falling back to Pillow
  extract.py                 orchestration, dedup, metadata output
  util.py                    timestamps, hashing, path safety
tools/make_test_backup.py    synthetic backup generator
tests/test_extractor.py      test suite
```
