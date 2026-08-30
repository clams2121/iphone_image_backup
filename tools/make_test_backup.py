#!/usr/bin/env python3
"""Build a synthetic iPhone backup for testing the inspector and extractor.

This fabricates the same structures a real unencrypted backup has -- the
Manifest.db index with NSKeyedArchiver file blobs, the fileID fanout
directories, a Photos.sqlite with ZASSET/ZADDITIONALASSETATTRIBUTES and a
numbered album join table, an sms.db, a CloudRecordings.db, and WhatsApp
media -- so the tools can be exercised without touching real photos.

    python3 tools/make_test_backup.py /tmp/fake_backup_root
"""

from __future__ import annotations

import hashlib
import plistlib
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
UDID = "00008110-000A1B2C3D4E5F26"
ENCRYPTED_UDID = "00008030-0011223344556677"


def apple_seconds(dt: datetime) -> float:
    return (dt - APPLE_EPOCH).total_seconds()


def file_id(domain: str, relative_path: str) -> str:
    """Apple derives the fileID from SHA-1 of 'domain-relativePath'."""
    return hashlib.sha1(f"{domain}-{relative_path}".encode()).hexdigest()


def mbfile_blob(size: int, modified: int) -> bytes:
    """An NSKeyedArchiver-shaped plist like the real Files.file column."""
    plist = {
        "$version": 100000,
        "$archiver": "NSKeyedArchiver",
        "$top": {"root": plistlib.UID(1)},
        "$objects": [
            "$null",
            {
                "Size": size,
                "LastModified": modified,
                "Birth": modified - 60,
                "Mode": 33188,
                "UserID": 501,
                "GroupID": 501,
                "InodeNumber": 123456,
                "ProtectionClass": 3,
                "$class": plistlib.UID(2),
            },
            {"$classname": "MBFile", "$classes": ["MBFile", "NSObject"]},
        ],
    }
    return plistlib.dumps(plist, fmt=plistlib.FMT_BINARY)


def make_jpeg(path: Path, color, when: datetime, gps=None, model="iPhone 14 Pro"):
    from PIL import Image

    image = Image.new("RGB", (64, 48), color)
    exif = Image.Exif()
    exif[0x010F] = "Apple"
    exif[0x0110] = model
    exif[0x0131] = "17.4.1"
    exif[0x0132] = when.strftime("%Y:%m:%d %H:%M:%S")
    exif[0x8769] = {
        0x9003: when.strftime("%Y:%m:%d %H:%M:%S"),
        0xA002: 64,
        0xA003: 48,
        0xA434: f"{model} back camera 6.86mm f/1.78",
    }
    if gps:
        lat, lon = gps
        exif[0x8825] = {
            1: "N" if lat >= 0 else "S",
            2: _dms(abs(lat)),
            3: "E" if lon >= 0 else "W",
            4: _dms(abs(lon)),
            6: 42.5,
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "JPEG", exif=exif)


def _dms(value: float):
    degrees = int(value)
    minutes_full = (value - degrees) * 60
    minutes = int(minutes_full)
    seconds = round((minutes_full - minutes) * 60, 4)
    return (float(degrees), float(minutes), seconds)


class BackupBuilder:
    def __init__(self, root: Path, udid: str, encrypted: bool = False):
        self.path = root / udid
        self.udid = udid
        self.encrypted = encrypted
        self.path.mkdir(parents=True, exist_ok=True)
        self.entries: list = []

    def add(self, domain: str, relative_path: str, content: bytes = None,
            source: Path = None, present: bool = True) -> str:
        fid = file_id(domain, relative_path)
        data = content
        if source is not None:
            data = source.read_bytes()
        if data is None:
            data = b""
        if present:
            target = self.path / fid[:2] / fid
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        self.entries.append((fid, domain, relative_path, len(data)))
        return fid

    def write_manifest(self) -> None:
        db_path = self.path / "Manifest.db"
        if db_path.exists():
            db_path.unlink()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, "
            "relativePath TEXT, flags INTEGER, file BLOB)"
        )
        modified = int(datetime(2024, 5, 1, tzinfo=timezone.utc).timestamp())
        seen = set()
        for fid, domain, relative_path, size in self.entries:
            if fid in seen:
                continue
            seen.add(fid)
            conn.execute(
                "INSERT INTO Files VALUES (?,?,?,?,?)",
                (fid, domain, relative_path, 1, mbfile_blob(size, modified)),
            )
        # a couple of directory rows, as real backups have
        for domain, relative_path in (
            ("CameraRollDomain", "Media/DCIM/100APPLE"),
            ("HomeDomain", "Library/SMS"),
        ):
            fid = file_id(domain, relative_path)
            if fid not in seen:
                conn.execute(
                    "INSERT INTO Files VALUES (?,?,?,?,?)",
                    (fid, domain, relative_path, 2, None),
                )
        conn.commit()
        conn.close()

    def write_plists(self, device_name: str, ios: str) -> None:
        info = {
            "Device Name": device_name,
            "Display Name": device_name,
            "Product Name": "iPhone 14 Pro",
            "Product Type": "iPhone15,2",
            "Product Version": ios,
            "Build Version": "21E236",
            "Serial Number": "F2LX9QWERTY",
            "Unique Identifier": self.udid.replace("-", ""),
            "Last Backup Date": datetime(2024, 5, 1, 9, 30),
            "iTunes Version": "12.12.10.1",
        }
        (self.path / "Info.plist").write_bytes(
            plistlib.dumps(info, fmt=plistlib.FMT_BINARY)
        )
        manifest = {
            "IsEncrypted": self.encrypted,
            "Version": "10.0",
            "Date": datetime(2024, 5, 1, 9, 30),
            "SystemDomainsVersion": "30.0",
            "WasPasscodeSet": True,
            "Lockdown": {
                "DeviceName": device_name,
                "ProductType": "iPhone15,2",
                "ProductVersion": ios,
                "UniqueDeviceID": self.udid,
            },
        }
        (self.path / "Manifest.plist").write_bytes(
            plistlib.dumps(manifest, fmt=plistlib.FMT_BINARY)
        )
        (self.path / "Status.plist").write_bytes(
            plistlib.dumps(
                {
                    "IsFullBackup": True,
                    "SnapshotState": "finished",
                    "Date": datetime(2024, 5, 1, 9, 30),
                    "UUID": self.udid,
                },
                fmt=plistlib.FMT_BINARY,
            )
        )


def build_photos_sqlite(path: Path, assets: list) -> None:
    """ZASSET + ZADDITIONALASSETATTRIBUTES + ZGENERICALBUM + Z_28ASSETS."""
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE ZASSET (
            Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
            ZFILENAME VARCHAR, ZDIRECTORY VARCHAR, ZUUID VARCHAR,
            ZDATECREATED TIMESTAMP, ZADDEDDATE TIMESTAMP,
            ZMODIFICATIONDATE TIMESTAMP, ZLATITUDE FLOAT, ZLONGITUDE FLOAT,
            ZFAVORITE INTEGER, ZHIDDEN INTEGER, ZTRASHEDSTATE INTEGER,
            ZTRASHEDDATE TIMESTAMP, ZKIND INTEGER, ZDURATION FLOAT,
            ZWIDTH INTEGER, ZHEIGHT INTEGER, ZTIMEZONEOFFSET INTEGER,
            ZSAVEDASSETTYPE INTEGER);
        CREATE TABLE ZADDITIONALASSETATTRIBUTES (
            Z_PK INTEGER PRIMARY KEY, ZASSET INTEGER,
            ZORIGINALFILENAME VARCHAR, ZTIMEZONENAME VARCHAR,
            ZEXIFTIMESTAMPSTRING VARCHAR, ZCREATORBUNDLEID VARCHAR);
        CREATE TABLE ZGENERICALBUM (
            Z_PK INTEGER PRIMARY KEY, ZKIND INTEGER, ZTITLE VARCHAR,
            ZTRASHEDSTATE INTEGER);
        CREATE TABLE Z_28ASSETS (
            Z_28ALBUMS INTEGER, Z_3ASSETS INTEGER, Z_FOK_3ASSETS INTEGER);
        """
    )
    albums = {1: "Holidays 2023", 2: "Family", 3: "Recently Deleted"}
    for pk, title in albums.items():
        conn.execute(
            "INSERT INTO ZGENERICALBUM VALUES (?,?,?,?)", (pk, 2, title, 0)
        )
    for index, asset in enumerate(assets, 1):
        conn.execute(
            "INSERT INTO ZASSET (Z_PK, ZFILENAME, ZDIRECTORY, ZUUID, "
            "ZDATECREATED, ZADDEDDATE, ZMODIFICATIONDATE, ZLATITUDE, "
            "ZLONGITUDE, ZFAVORITE, ZHIDDEN, ZTRASHEDSTATE, ZTRASHEDDATE, "
            "ZKIND, ZDURATION, ZWIDTH, ZHEIGHT, ZTIMEZONEOFFSET, "
            "ZSAVEDASSETTYPE) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                index,
                asset["filename"],
                asset["directory"],
                asset["uuid"],
                apple_seconds(asset["created"]),
                apple_seconds(asset["created"] + timedelta(minutes=5)),
                apple_seconds(asset["created"] + timedelta(minutes=5)),
                asset.get("lat", -180.0),
                asset.get("lon", -180.0),
                asset.get("favorite", 0),
                asset.get("hidden", 0),
                asset.get("trashed", 0),
                apple_seconds(asset["created"]) if asset.get("trashed") else None,
                asset.get("kind", 0),
                asset.get("duration"),
                asset.get("width", 64),
                asset.get("height", 48),
                -25200,
                1,
            ),
        )
        conn.execute(
            "INSERT INTO ZADDITIONALASSETATTRIBUTES VALUES (?,?,?,?,?,?)",
            (
                index,
                index,
                asset.get("original_filename", asset["filename"]),
                "America/Los_Angeles",
                asset["created"].strftime("%Y:%m:%d %H:%M:%S"),
                "com.apple.camera",
            ),
        )
        for album_pk in asset.get("albums", []):
            conn.execute(
                "INSERT INTO Z_28ASSETS VALUES (?,?,?)", (album_pk, index, index)
            )
    conn.commit()
    conn.close()


def build_sms_db(path: Path, attachments: list) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT,
            service TEXT, uncanonicalized_id TEXT);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT,
            handle_id INTEGER, service TEXT, date INTEGER, is_from_me INTEGER,
            cache_has_attachments INTEGER);
        CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, guid TEXT,
            created_date INTEGER, filename TEXT, mime_type TEXT,
            transfer_name TEXT, total_bytes INTEGER, is_sticker INTEGER);
        CREATE TABLE message_attachment_join (message_id INTEGER,
            attachment_id INTEGER);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT,
            chat_identifier TEXT, display_name TEXT, service_name TEXT);
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        """
    )
    conn.execute(
        "INSERT INTO handle VALUES (1, '+15551234567', 'iMessage', '5551234567')"
    )
    conn.execute("INSERT INTO handle VALUES (2, 'friend@example.com', 'iMessage', NULL)")
    conn.execute(
        "INSERT INTO chat VALUES (1, 'iMessage;-;+15551234567', "
        "'+15551234567', 'Alex', 'iMessage')"
    )
    conn.execute(
        "INSERT INTO chat VALUES (2, 'iMessage;+;chat42', 'chat42', "
        "'Hiking Crew', 'iMessage')"
    )
    for index, item in enumerate(attachments, 1):
        when_ns = int(apple_seconds(item["date"]) * 1_000_000_000)
        conn.execute(
            "INSERT INTO message VALUES (?,?,?,?,?,?,?,1)",
            (
                index,
                f"MSG-GUID-{index:04d}",
                item.get("text", "here you go"),
                item.get("handle", 1),
                "iMessage",
                when_ns,
                item.get("from_me", 0),
            ),
        )
        conn.execute(
            "INSERT INTO attachment VALUES (?,?,?,?,?,?,?,0)",
            (
                index,
                f"ATT-GUID-{index:04d}",
                when_ns,
                "~/" + item["relative_path"],
                item["mime"],
                item["name"],
                item.get("size", 1024),
            ),
        )
        conn.execute(
            "INSERT INTO message_attachment_join VALUES (?,?)", (index, index)
        )
        conn.execute(
            "INSERT INTO chat_message_join VALUES (?,?)",
            (item.get("chat", 1), index),
        )
    conn.commit()
    conn.close()


def build_recordings_db(path: Path, memos: list) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE ZRECORDING (Z_PK INTEGER PRIMARY KEY, ZPATH VARCHAR, "
        "ZCUSTOMLABEL VARCHAR, ZDATE TIMESTAMP, ZDURATION FLOAT)"
    )
    for index, memo in enumerate(memos, 1):
        conn.execute(
            "INSERT INTO ZRECORDING VALUES (?,?,?,?,?)",
            (
                index,
                memo["path"],
                memo["title"],
                apple_seconds(memo["date"]),
                memo["duration"],
            ),
        )
    conn.commit()
    conn.close()


def build(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    # Stage generated media outside the backup root so it is not mistaken
    # for part of a backup.
    work = Path(tempfile.mkdtemp(prefix="iphonebackup_staging_"))

    builder = BackupBuilder(root, UDID)

    # ---- camera roll -----------------------------------------------------
    photos = [
        dict(filename="IMG_0001.JPG", directory="DCIM/100APPLE",
             uuid="11111111-1111-4111-8111-111111111111",
             created=datetime(2023, 7, 4, 12, 34, 56, tzinfo=timezone.utc),
             lat=37.7749, lon=-122.4194, favorite=1, albums=[1]),
        dict(filename="IMG_0002.JPG", directory="DCIM/100APPLE",
             uuid="22222222-2222-4222-8222-222222222222",
             created=datetime(2023, 8, 15, 18, 5, 0, tzinfo=timezone.utc),
             albums=[1, 2]),
        dict(filename="IMG_0003.JPG", directory="DCIM/100APPLE",
             uuid="33333333-3333-4333-8333-333333333333",
             created=datetime(2023, 9, 1, 8, 0, 0, tzinfo=timezone.utc),
             hidden=1),
        dict(filename="IMG_0004.JPG", directory="DCIM/100APPLE",
             uuid="44444444-4444-4444-8444-444444444444",
             created=datetime(2023, 10, 2, 8, 0, 0, tzinfo=timezone.utc),
             trashed=1, albums=[3]),
        dict(filename="IMG_0005.MOV", directory="DCIM/100APPLE",
             uuid="55555555-5555-4555-8555-555555555555",
             created=datetime(2023, 11, 3, 8, 0, 0, tzinfo=timezone.utc),
             kind=1, duration=12.5),
        # An asset in Photos.sqlite whose file is not in the backup, to
        # exercise the unmatched-rows report.
        dict(filename="IMG_9999.JPG", directory="DCIM/101APPLE",
             uuid="99999999-9999-4999-8999-999999999999",
             created=datetime(2024, 1, 1, 8, 0, 0, tzinfo=timezone.utc)),
    ]
    gps_by_name = {"IMG_0001.JPG": (37.7749, -122.4194)}
    for position, asset in enumerate(photos[:5]):
        name = asset["filename"]
        relative = f"Media/{asset['directory']}/{name}"
        if name.endswith(".MOV"):
            # stand-in for a video: not a real container, just bytes
            builder.add("CameraRollDomain", relative, content=b"\x00FAKEMOV" * 64)
            continue
        staged = work / name
        make_jpeg(staged, (position * 40 % 255, 90, 140), asset["created"],
                  gps_by_name.get(name))
        builder.add("CameraRollDomain", relative, source=staged)

    # a duplicate of IMG_0001 under a different name, for the dedup path
    builder.add(
        "CameraRollDomain",
        "Media/DCIM/101APPLE/IMG_0101.JPG",
        source=work / "IMG_0001.JPG",
    )
    # a non-media file that must be ignored
    builder.add("CameraRollDomain", "Media/DCIM/100APPLE/thumb.ithmb", b"junk")
    # indexed but missing from disk, to exercise error reporting
    builder.add(
        "CameraRollDomain",
        "Media/DCIM/100APPLE/IMG_0777.JPG",
        content=b"never written",
        present=False,
    )

    # ---- message attachments --------------------------------------------
    attachments = [
        dict(relative_path="Library/SMS/Attachments/0a/10/"
             "AAAAAAAA-1111-2222-3333-444444444444/IMG_2001.JPG",
             name="IMG_2001.JPG", mime="image/jpeg",
             date=datetime(2024, 2, 1, 15, 0, tzinfo=timezone.utc),
             from_me=0, chat=1),
        dict(relative_path="Library/SMS/Attachments/0b/11/"
             "BBBBBBBB-1111-2222-3333-444444444444/trail.mov",
             name="trail.mov", mime="video/quicktime",
             date=datetime(2024, 2, 3, 9, 15, tzinfo=timezone.utc),
             from_me=1, chat=2, handle=2),
        dict(relative_path="Library/SMS/Attachments/0c/12/"
             "CCCCCCCC-1111-2222-3333-444444444444/contract.pdf",
             name="contract.pdf", mime="application/pdf",
             date=datetime(2024, 2, 4, 9, 15, tzinfo=timezone.utc),
             from_me=0, chat=1),
    ]
    for item in attachments:
        if item["name"].endswith(".JPG"):
            staged = work / item["name"]
            make_jpeg(staged, (200, 60, 60), item["date"], model="iPhone 12")
            builder.add("MediaDomain", item["relative_path"], source=staged)
        elif item["name"].endswith(".mov"):
            builder.add("MediaDomain", item["relative_path"], b"\x00FAKEMOV" * 32)
        else:
            builder.add("MediaDomain", item["relative_path"], b"%PDF-1.4 fake")

    # ---- WhatsApp --------------------------------------------------------
    wa_domain = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
    wa_photo = work / "IMG-20240210-WA0001.jpg"
    make_jpeg(wa_photo, (40, 160, 90),
              datetime(2024, 2, 10, 11, 0, tzinfo=timezone.utc), model="iPhone 11")
    builder.add(
        wa_domain,
        "Message/Media/15551234567@s.whatsapp.net/1/5/IMG-20240210-WA0001.jpg",
        source=wa_photo,
    )
    builder.add(
        wa_domain,
        "Message/Media/15551234567@s.whatsapp.net/1/5/IMG-20240210-WA0001.jpg.thumb",
        b"thumb",
    )
    builder.add(wa_domain, "Message/Media/Profile/12345-photo.jpg", b"profile")

    # ---- voice memos -----------------------------------------------------
    memos = [
        dict(path="Recordings/20240115 093000.m4a", title="Band practice idea",
             date=datetime(2024, 1, 15, 9, 30, tzinfo=timezone.utc),
             duration=93.4),
        dict(path="Recordings/20240220 174500.m4a", title="Grocery list",
             date=datetime(2024, 2, 20, 17, 45, tzinfo=timezone.utc),
             duration=21.0),
    ]
    for position, memo in enumerate(memos):
        name = Path(memo["path"]).name
        builder.add(
            "HomeDomain",
            f"Library/Recordings/{name}",
            bytes([position]) + b"\x00M4A\x00" * (100 + position * 7),
        )

    # ---- support databases ----------------------------------------------
    photos_db = work / "Photos.sqlite"
    build_photos_sqlite(photos_db, photos)
    builder.add(
        "CameraRollDomain", "Media/PhotoData/Photos.sqlite", source=photos_db
    )
    builder.add(
        "CameraRollDomain", "Media/PhotoData/Photos.sqlite-shm", b"\x00" * 32
    )

    sms_db = work / "sms.db"
    build_sms_db(sms_db, attachments)
    builder.add("HomeDomain", "Library/SMS/sms.db", source=sms_db)

    rec_db = work / "CloudRecordings.db"
    build_recordings_db(rec_db, memos)
    builder.add("HomeDomain", "Library/Recordings/CloudRecordings.db", source=rec_db)

    builder.write_manifest()
    builder.write_plists("Test iPhone", "17.4.1")

    # ---- a second, encrypted backup so the safety check has something ----
    locked = BackupBuilder(root, ENCRYPTED_UDID, encrypted=True)
    locked.add("CameraRollDomain", "Media/DCIM/100APPLE/IMG_5000.JPG", b"encrypted")
    locked.write_manifest()
    locked.write_plists("Locked iPhone", "16.6")

    shutil.rmtree(work, ignore_errors=True)
    return root


if __name__ == "__main__":
    destination = Path(sys.argv[1] if len(sys.argv) > 1 else "fake_backups")
    print(f"Building synthetic backups in {destination.resolve()}")
    build(destination)
    print("Done.")
