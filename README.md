# iphone_image_backup

Extract photos, videos, message attachments and voice memos — with their
metadata — out of unencrypted iTunes/Finder iPhone backups.

Everything runs locally. The scripts make no network calls, never write
outside the `--output` directory you choose, and open the backup itself
read-only (databases are worked on in a scratch copy, so `Photos.sqlite`
and `sms.db` inside the backup are never modified).

Requires macOS or Linux and Python 3.10+.

---

## Two scripts

### 1. `inspect_backup.py` — look before you extract

Prints the backups it finds and, for a chosen one, the **actual** schema of
`Manifest.db`, `Photos.sqlite`, `sms.db` and `CloudRecordings.db` on that
device, plus how many files sit in each category. Table and column names
shift between iOS releases, so run this first and check the output matches
what the extractor expects.

```bash
python3 inspect_backup.py --list                      # what backups exist
python3 inspect_backup.py                             # pick one interactively
python3 inspect_backup.py --backup 00008110 --samples 5
python3 inspect_backup.py --all --json schema.json    # machine-readable report
```

The inspector is read-only; the only thing it can write is the optional
`--json` report.

### 2. `extract_backup.py` — do the extraction

```bash
python3 extract_backup.py --list
python3 extract_backup.py --backup 00008110 --output ~/Pictures/iphone
python3 extract_backup.py --all --json
python3 extract_backup.py --backup "My iPhone" --only camera_roll --dry-run
```

Useful flags:

| flag | effect |
|---|---|
| `--backup UDID\|name\|path` | pick a backup (repeatable); otherwise you're prompted |
| `--all` | process every backup found |
| `--output DIR` | where to write (default `./output`) |
| `--only` / `--skip` | restrict categories: `camera_roll`, `messages`, `whatsapp`, `voice_memo` |
| `--camera-path 'Media/PhotoData/CPLAssets/%'` | add an extra `CameraRollDomain` path pattern (SQL `LIKE`) |
| `--json` | also write `metadata.json` next to the CSV |
| `--global-dedup` | dedupe across all devices instead of per device |
| `--include-message-text` | include message bodies in the metadata (off by default) |
| `--dry-run` | report what would be extracted, write nothing |
| `--backup-root DIR` | read backups from somewhere other than the default location |

## Output

```
output/
  <device_name>_<udid8>/
    camera_roll/            photos and videos from Media/DCIM
    message_attachments/    image/video attachments from Messages and WhatsApp
    voice_memos/            .m4a recordings
    metadata.csv            one row per backed-up file
    metadata.json           same, with --json
    .extract_index.json     content hashes, for dedup across runs
```

Files come out under their original names (`IMG_0001.HEIC`, `trail.mov`,
`20240115 093000.m4a`), not the SHA-1 hashes the backup stores them as. Name
collisions get a `_1`, `_2` suffix.

### metadata.csv

One row per file found in the backup, with:

- **provenance** — `source`, `original_filename`, `output_path`, `media_type`,
  `size_bytes`, `sha256`, `domain`, `relative_path`, `file_id`,
  `backup_mtime`, `device_name`, `device_udid`, `backup_date`, `status`
- **EXIF, read from the copied file** — `exif_datetime`, `exif_make`,
  `exif_model`, `exif_lens`, `exif_software`, `exif_orientation`,
  `exif_width`, `exif_height`, `exif_latitude`, `exif_longitude`,
  `exif_altitude`
- **Photos.sqlite, for camera roll files** — `photos_uuid`, `photos_filename`,
  `photos_original_filename`, `photos_date_created`, `photos_date_added`,
  `photos_date_modified`, `photos_latitude`, `photos_longitude`,
  `photos_favorite`, `photos_hidden`, `photos_trashed`, `photos_trashed_date`,
  `photos_kind`, `photos_duration`, `photos_width`, `photos_height`,
  `photos_timezone`, `photos_albums`
- **sms.db, for attachments** — `msg_attachment_name`, `msg_mime_type`,
  `msg_guid`, `msg_date`, `msg_direction`, `msg_service`, `msg_handle`,
  `msg_chat` (and `msg_text` only with `--include-message-text`)
- **CloudRecordings.db, for voice memos** — `voice_title`, `voice_date`,
  `voice_duration`

Any column the device's schema doesn't have is left blank rather than
failing the run. Timestamps are ISO-8601 UTC; Core Data's 2001 epoch and
the nanosecond timestamps Messages uses on iOS 11+ are both converted.

The CSV is cumulative: a later run merges into the rows already there
(keyed by device + `fileID`), so extracting several backups of the same
device over time builds one complete index.

## Dedup

Files are hashed with SHA-256 and recorded in `.extract_index.json`. A file
whose content was already extracted — in this run or a previous one — is not
copied again; it still gets its own row in `metadata.csv`, with
`status = duplicate` and `output_path` pointing at the single stored copy.
Re-running against a newer backup of the same phone therefore copies only
what's new.

Dedup is per device by default. `--global-dedup` shares one index across
every device under the output directory.

## Encrypted backups

`Manifest.plist` is checked before anything else. A backup with
`IsEncrypted = true` is skipped with a message naming the device; the run
continues with the others. To use one, turn off "Encrypt local backup" in
Finder and take a fresh backup, or decrypt it first.

## What gets picked up

| category | where it comes from |
|---|---|
| `camera_roll` | `CameraRollDomain`, `Media/DCIM/%` — images and videos |
| `messages` | any domain, `Library/SMS/Attachments/%` — images and videos only |
| `whatsapp` | any domain matching `%whatsapp%` under `Message/Media/` — thumbnails and profile pictures excluded |
| `voice_memo` | `Library/Recordings/%` and the `com.apple.VoiceMemos` app domains — audio files |

Non-media attachments (PDFs, vCards) are deliberately skipped. If iCloud
Photos is set to "Optimize iPhone Storage", full-resolution originals live
in iCloud and are not in the backup at all — the `Photos.sqlite` rows will
be there, and the summary reports how many had no file to match.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt      # optional; only affects EXIF columns
```

macOS may need Terminal to be granted **Full Disk Access**
(System Settings → Privacy & Security) to read
`~/Library/Application Support/MobileSync/Backup/`.

## Tests

The suite builds a synthetic backup — Manifest.db with NSKeyedArchiver file
blobs, the fileID fanout layout, a `Photos.sqlite`, an `sms.db`, WhatsApp
media, an encrypted second device — and runs the real code against it.

```bash
python3 -m unittest discover -s tests -v
```

To eyeball the tools without a real phone:

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
  photos.py                  Photos.sqlite reader (schema discovered at run time)
  sms.py                     sms.db reader (schema discovered at run time)
  voicememos.py              CloudRecordings.db reader
  exifdata.py                EXIF via exifread, falling back to Pillow
  extract.py                 orchestration, dedup, metadata output
  util.py                    timestamps, hashing, path safety
tools/make_test_backup.py    synthetic backup generator
tests/test_extractor.py      test suite
```

## Notes on backup internals

Kept here so the code doesn't have to re-explain itself:

- Backups live in `~/Library/Application Support/MobileSync/Backup/<UDID>/`.
- `Manifest.db` is the master index. Its `Files` table maps `fileID`
  (SHA-1 of `"<domain>-<relativePath>"`) to `domain`, `relativePath`,
  `flags` (1 = file, 2 = directory) and `file`, an NSKeyedArchiver plist
  holding `Size`, `LastModified`, `Mode` and friends.
- Content is stored at `<backup>/<fileID[:2]>/<fileID>` — the two-character
  fanout used since iOS 10. Pre-iOS 10 backups use `Manifest.mbdb` and a flat
  layout; those are detected and skipped with a message.
- `Photos.sqlite`, `sms.db` and `CloudRecordings.db` may have unreplayed
  `-wal` files. Both scripts copy the `-wal`/`-shm` siblings out alongside
  the database and let SQLite checkpoint the copy.
- Photos' asset table is `ZASSET` on recent iOS and `ZGENERICASSET` on older
  ones; the album join table is numbered per build (`Z_28ASSETS`,
  `Z_17ASSETS`, …). Both are found by probing `sqlite_master`, never
  hardcoded — see `iphonebackup/photos.py`.
