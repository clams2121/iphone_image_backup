"""Tests for the backup reader, run against a synthetic backup.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import csv
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from iphonebackup.backup import discover_backups, parse_mbfile  # noqa: E402
from iphonebackup.extract import Extractor, Options  # noqa: E402
from iphonebackup.photos import PhotosLibrary  # noqa: E402
from iphonebackup.sms import normalize_attachment_path  # noqa: E402
from iphonebackup.util import apple_timestamp, assert_within, coordinate  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from make_test_backup import UDID, build, mbfile_blob  # noqa: E402


class FixtureTest(unittest.TestCase):
    """End-to-end run over the synthetic backup."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="iphonebackup_test_"))
        cls.root = build(cls.tmp / "backups")
        cls.out = cls.tmp / "out"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.backups = discover_backups(self.root)

    def test_discovers_both_backups(self):
        self.assertEqual(len(self.backups), 2)
        names = {b.device_name for b in self.backups}
        self.assertEqual(names, {"Test iPhone", "Locked iPhone"})

    def test_reads_device_facts_from_plists(self):
        backup = next(b for b in self.backups if b.udid == UDID)
        self.assertEqual(backup.ios_version, "17.4.1")
        self.assertEqual(backup.product_name, "iPhone 14 Pro")
        self.assertFalse(backup.is_encrypted)

    def test_flags_encrypted_backup(self):
        locked = next(b for b in self.backups if b.device_name == "Locked iPhone")
        self.assertTrue(locked.is_encrypted)

    def test_fanout_layout(self):
        backup = next(b for b in self.backups if b.udid == UDID)
        with backup:
            item = backup.find_one(
                "CameraRollDomain", "Media/DCIM/100APPLE/IMG_0001.JPG"
            )
            self.assertIsNotNone(item)
            stored = item.stored_path
            self.assertIsNotNone(stored)
            self.assertEqual(stored.parent.name, item.file_id[:2])
            self.assertEqual(item.size, stored.stat().st_size)

    def _extract(self, output: Path, **kwargs) -> object:
        backup = next(b for b in self.backups if b.udid == UDID)
        with backup:
            return Extractor(backup, Options(output=output, quiet=True, **kwargs)).run()

    def test_extraction_counts_and_layout(self):
        out = self.out / "run1"
        summary = self._extract(out)
        self.assertEqual(summary.counts["camera_roll"], 6)
        self.assertEqual(summary.counts["messages"], 2)  # the PDF is skipped
        self.assertEqual(summary.counts["whatsapp"], 1)  # thumb/profile skipped
        self.assertEqual(summary.counts["voice_memo"], 2)

        device_dir = out / "Test_iPhone_00008110"
        self.assertTrue((device_dir / "camera_roll" / "IMG_0001.JPG").exists())
        self.assertTrue(
            (device_dir / "message_attachments" / "IMG_2001.JPG").exists()
        )
        self.assertTrue(
            (device_dir / "voice_memos" / "20240115 093000.m4a").exists()
        )
        self.assertFalse((device_dir / "message_attachments" / "contract.pdf").exists())

    def test_missing_content_is_reported_not_fatal(self):
        summary = self._extract(self.out / "run_missing")
        self.assertTrue(
            any("IMG_0777.JPG" in error for error in summary.errors),
            summary.errors,
        )

    def test_photos_metadata_joins_onto_camera_roll(self):
        out = self.out / "run_meta"
        self._extract(out)
        rows = self._rows(out)
        first = rows[("camera_roll", "IMG_0001.JPG")]
        self.assertEqual(first["photos_albums"], "Holidays 2023")
        self.assertEqual(first["photos_favorite"], "True")
        self.assertEqual(first["photos_date_created"], "2023-07-04T12:34:56Z")
        self.assertEqual(first["photos_latitude"], "37.7749")

        hidden = rows[("camera_roll", "IMG_0003.JPG")]
        self.assertEqual(hidden["photos_hidden"], "True")
        trashed = rows[("camera_roll", "IMG_0004.JPG")]
        self.assertEqual(trashed["photos_trashed"], "True")
        video = rows[("camera_roll", "IMG_0005.MOV")]
        self.assertEqual(video["photos_kind"], "video")
        self.assertEqual(video["photos_duration"], "12.5")

    def test_exif_read_from_the_copied_file(self):
        out = self.out / "run_exif"
        self._extract(out)
        row = self._rows(out)[("camera_roll", "IMG_0001.JPG")]
        self.assertEqual(row["exif_make"], "Apple")
        self.assertEqual(row["exif_model"], "iPhone 14 Pro")
        self.assertEqual(row["exif_datetime"], "2023:07:04 12:34:56")
        self.assertAlmostEqual(float(row["exif_latitude"]), 37.7749, places=3)
        self.assertAlmostEqual(float(row["exif_longitude"]), -122.4194, places=3)

    def test_message_metadata_joins_onto_attachments(self):
        out = self.out / "run_sms"
        self._extract(out)
        rows = self._rows(out)
        row = rows[("messages", "IMG_2001.JPG")]
        self.assertEqual(row["msg_chat"], "Alex")
        self.assertEqual(row["msg_direction"], "received")
        self.assertEqual(row["msg_date"], "2024-02-01T15:00:00Z")
        sent = rows[("messages", "trail.mov")]
        self.assertEqual(sent["msg_direction"], "sent")
        self.assertEqual(sent["msg_handle"], "friend@example.com")

    def test_voice_memo_titles(self):
        out = self.out / "run_voice"
        self._extract(out)
        rows = self._rows(out)
        row = rows[("voice_memo", "20240115 093000.m4a")]
        self.assertEqual(row["voice_title"], "Band practice idea")
        self.assertEqual(row["voice_date"], "2024-01-15T09:30:00Z")

    def test_message_text_excluded_unless_requested(self):
        out = self.out / "run_notext"
        self._extract(out)
        with (out / "Test_iPhone_00008110" / "metadata.csv").open() as handle:
            self.assertNotIn("msg_text", next(csv.reader(handle)))
        out2 = self.out / "run_text"
        self._extract(out2, include_message_text=True)
        row = self._rows(out2)[("messages", "IMG_2001.JPG")]
        self.assertEqual(row["msg_text"], "here you go")

    def test_dedup_by_content_hash_within_a_run(self):
        out = self.out / "run_dedup"
        summary = self._extract(out)
        self.assertEqual(summary.skipped_duplicates, 1)
        rows = self._rows(out)
        copy = rows[("camera_roll", "IMG_0101.JPG")]
        self.assertEqual(copy["status"], "duplicate")
        # the duplicate keeps its own row but points at the single stored file
        self.assertTrue(copy["output_path"].endswith("camera_roll/IMG_0001.JPG"))
        self.assertEqual(
            rows[("camera_roll", "IMG_0001.JPG")]["sha256"], copy["sha256"]
        )

    def test_rerun_extracts_nothing_new(self):
        out = self.out / "run_twice"
        self._extract(out)
        before = sorted(p.name for p in (out / "Test_iPhone_00008110" / "camera_roll").iterdir())
        summary = self._extract(out)
        after = sorted(p.name for p in (out / "Test_iPhone_00008110" / "camera_roll").iterdir())
        self.assertEqual(before, after)
        self.assertEqual(summary.bytes_written, 0)
        self.assertEqual(summary.skipped_duplicates, sum(summary.counts.values()))
        self.assertEqual(len(self._rows(out)), 11)

    def test_dry_run_writes_nothing(self):
        out = self.out / "run_dry"
        summary = self._extract(out, dry_run=True)
        self.assertGreater(sum(summary.counts.values()), 0)
        self.assertFalse(out.exists())

    @staticmethod
    def _rows(output: Path) -> dict:
        path = output / "Test_iPhone_00008110" / "metadata.csv"
        with path.open(newline="", encoding="utf-8") as handle:
            return {
                (row["source"], row["original_filename"]): row
                for row in csv.DictReader(handle)
            }


class LegacyPhotosSchemaTest(unittest.TestCase):
    """Older libraries use ZGENERICASSET and a different album join table."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="iphonebackup_legacy_"))
        self.db = self.tmp / "Photos.sqlite"
        conn = sqlite3.connect(self.db)
        conn.executescript(
            """
            CREATE TABLE ZGENERICASSET (
                Z_PK INTEGER PRIMARY KEY, ZFILENAME VARCHAR,
                ZDIRECTORY VARCHAR, ZUUID VARCHAR, ZDATECREATED TIMESTAMP,
                ZLATITUDE FLOAT, ZLONGITUDE FLOAT, ZFAVORITE INTEGER,
                ZHIDDEN INTEGER, ZTRASHEDSTATE INTEGER, ZKIND INTEGER);
            CREATE TABLE ZGENERICALBUM (
                Z_PK INTEGER PRIMARY KEY, ZTITLE VARCHAR);
            CREATE TABLE Z_17ASSETS (
                Z_17ALBUMS INTEGER, Z_2ASSETS INTEGER, Z_FOK_2ASSETS INTEGER);
            INSERT INTO ZGENERICASSET VALUES
                (1,'IMG_0100.JPG','DCIM/100APPLE','uuid-1',
                 550000000.0, 51.5074, -0.1278, 1, 0, 0, 0);
            INSERT INTO ZGENERICALBUM VALUES (7, 'London');
            INSERT INTO Z_17ASSETS VALUES (7, 1, 1);
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_discovers_legacy_tables(self):
        library = PhotosLibrary(self.db)
        self.assertEqual(library.asset_table, "ZGENERICASSET")
        self.assertEqual(library.album_join["table"], "Z_17ASSETS")
        self.assertEqual(library.album_join["album_col"], "Z_17ALBUMS")
        self.assertEqual(library.album_join["asset_col"], "Z_2ASSETS")
        # columns that do not exist in this schema are simply absent
        self.assertNotIn("date_added", library.fields)
        self.assertIsNone(library.attribute_table)

        asset = library.lookup("Media/DCIM/100APPLE/IMG_0100.JPG", "IMG_0100.JPG")
        self.assertIsNotNone(asset)
        self.assertEqual(asset["photos_albums"], "London")
        self.assertTrue(asset["photos_favorite"])
        self.assertEqual(asset["photos_date_created"], "2018-06-06T17:46:40Z")
        self.assertEqual(asset["photos_date_added"], "")
        library.close()


class UnitTest(unittest.TestCase):
    def test_apple_timestamp_units(self):
        self.assertEqual(apple_timestamp(0), "")
        self.assertEqual(apple_timestamp(700000000), "2023-03-08T20:26:40Z")
        # Messages on iOS 11+ stores nanoseconds; older builds stored seconds
        self.assertEqual(
            apple_timestamp(700000000 * 10**9, "auto"), "2023-03-08T20:26:40Z"
        )
        self.assertEqual(apple_timestamp(700000000, "auto"), "2023-03-08T20:26:40Z")

    def test_coordinate_sentinel(self):
        self.assertIsNone(coordinate(-180.0))
        self.assertIsNone(coordinate(None))
        self.assertEqual(coordinate(51.5), 51.5)

    def test_attachment_path_normalization(self):
        self.assertEqual(
            normalize_attachment_path("~/Library/SMS/Attachments/0a/10/G/a.jpg"),
            "Library/SMS/Attachments/0a/10/G/a.jpg",
        )
        self.assertEqual(
            normalize_attachment_path(
                "/var/mobile/Library/SMS/Attachments/0a/10/G/a.jpg"
            ),
            "Library/SMS/Attachments/0a/10/G/a.jpg",
        )
        self.assertEqual(normalize_attachment_path(""), "")

    def test_mbfile_blob_roundtrip(self):
        parsed = parse_mbfile(mbfile_blob(4096, 1700000000))
        self.assertEqual(parsed["Size"], 4096)
        self.assertEqual(parsed["LastModified"], 1700000000)
        self.assertEqual(parse_mbfile(b"not a plist"), {})
        self.assertEqual(parse_mbfile(None), {})

    def test_writes_are_confined_to_the_output_directory(self):
        root = Path(tempfile.mkdtemp(prefix="iphonebackup_guard_"))
        try:
            assert_within(root, root / "camera_roll" / "IMG_0001.JPG")
            with self.assertRaises(ValueError):
                assert_within(root, root / ".." / "escaped.jpg")
            with self.assertRaises(ValueError):
                assert_within(root, Path("/etc/passwd"))
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
