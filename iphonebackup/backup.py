"""Discovery of iTunes/Finder backups and access to their Manifest.db index."""

from __future__ import annotations

import plistlib
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from .util import sanitize_dirname, unix_timestamp

DEFAULT_BACKUP_ROOT = (
    Path.home() / "Library" / "Application Support" / "MobileSync" / "Backup"
)

# Manifest.db Files.flags
FLAG_FILE = 1
FLAG_DIRECTORY = 2
FLAG_SYMLINK = 4


class BackupError(Exception):
    pass


def read_plist(path: Path) -> dict:
    """Read an XML or binary plist; returns {} when absent or unreadable."""
    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except FileNotFoundError:
        return {}
    except Exception:
        try:  # pragma: no cover - only used on exotic/legacy plists
            import biplist  # type: ignore

            data = biplist.readPlist(str(path))
        except Exception:
            return {}
    return data if isinstance(data, dict) else {}


def parse_mbfile(blob: Optional[bytes]) -> dict:
    """Decode the NSKeyedArchiver blob in Manifest.db's ``Files.file`` column.

    Returns a flat dict of the MBFile properties (Size, LastModified, Birth,
    Mode, ProtectionClass, ...). Returns {} for anything it cannot decode --
    the blob is a bonus, never something extraction depends on.
    """
    if not blob:
        return {}
    try:
        plist = plistlib.loads(bytes(blob))
    except Exception:
        return {}
    if not isinstance(plist, dict):
        return {}
    objects = plist.get("$objects")
    if not isinstance(objects, list):
        return {k: v for k, v in plist.items() if not isinstance(v, (dict, list))}

    def deref(value):
        if isinstance(value, plistlib.UID):
            index = value.data
            if 0 <= index < len(objects):
                return objects[index]
            return None
        return value

    root = plist.get("$top", {}).get("root")
    target = deref(root) if root is not None else None
    if not isinstance(target, dict):
        # Fall back to the first object that looks like an MBFile.
        target = next(
            (o for o in objects if isinstance(o, dict) and "Size" in o), None
        )
    if not isinstance(target, dict):
        return {}

    out: dict = {}
    for key, value in target.items():
        if key.startswith("$"):
            continue
        value = deref(value)
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
    return out


@dataclass
class ManifestFile:
    """One row of Manifest.db's Files table, plus its on-disk location."""

    file_id: str
    domain: str
    relative_path: str
    flags: int
    size: Optional[int] = None
    modified: Optional[int] = None
    birth: Optional[int] = None
    backup: Optional["Backup"] = None

    @property
    def name(self) -> str:
        return self.relative_path.rsplit("/", 1)[-1]

    @property
    def is_file(self) -> bool:
        return self.flags == FLAG_FILE or (self.flags & FLAG_DIRECTORY) == 0

    @property
    def stored_path(self) -> Optional[Path]:
        return self.backup.stored_path(self.file_id) if self.backup else None

    @property
    def modified_iso(self) -> str:
        return unix_timestamp(self.modified)


class Backup:
    """A single ``Backup/<UDID>`` folder."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.udid = self.path.name
        self.info = read_plist(self.path / "Info.plist")
        self.manifest_plist = read_plist(self.path / "Manifest.plist")
        self.status = read_plist(self.path / "Status.plist")
        self._conn: Optional[sqlite3.Connection] = None
        self._files_columns: Optional[dict] = None

    # ---------------------------------------------------------------- facts

    @property
    def manifest_db(self) -> Path:
        return self.path / "Manifest.db"

    @property
    def exists(self) -> bool:
        return self.manifest_db.exists()

    @property
    def legacy_mbdb(self) -> bool:
        """Pre-iOS 10 backups use Manifest.mbdb; this tool does not read those."""
        return not self.manifest_db.exists() and (self.path / "Manifest.mbdb").exists()

    @property
    def is_encrypted(self) -> bool:
        value = self.manifest_plist.get("IsEncrypted")
        if value is None:
            value = self.info.get("IsEncrypted")
        return bool(value)

    @property
    def lockdown(self) -> dict:
        value = self.manifest_plist.get("Lockdown")
        return value if isinstance(value, dict) else {}

    @property
    def device_name(self) -> str:
        for value in (
            self.info.get("Device Name"),
            self.info.get("Display Name"),
            self.lockdown.get("DeviceName"),
        ):
            if value:
                return str(value)
        return self.udid

    @property
    def product_name(self) -> str:
        for value in (
            self.info.get("Product Name"),
            self.info.get("Product Type"),
            self.lockdown.get("ProductType"),
        ):
            if value:
                return str(value)
        return "unknown device"

    @property
    def ios_version(self) -> str:
        for value in (
            self.info.get("Product Version"),
            self.lockdown.get("ProductVersion"),
        ):
            if value:
                return str(value)
        return "?"

    @property
    def last_backup_date(self) -> str:
        for value in (
            self.info.get("Last Backup Date"),
            self.manifest_plist.get("Date"),
            self.status.get("Date"),
        ):
            if value is not None:
                return str(value)
        return "?"

    @property
    def serial_number(self) -> str:
        return str(self.info.get("Serial Number") or "")

    @property
    def output_dirname(self) -> str:
        """Folder name for this device's output; UDID-suffixed to stay unique."""
        return f"{sanitize_dirname(self.device_name)}_{self.udid[:8]}"

    def describe(self) -> str:
        state = "ENCRYPTED" if self.is_encrypted else "unencrypted"
        return (
            f"{self.device_name} ({self.product_name}, iOS {self.ios_version}) "
            f"[{state}]\n    udid: {self.udid}\n    last backup: "
            f"{self.last_backup_date}\n    path: {self.path}"
        )

    # ------------------------------------------------------------- manifest

    def connect(self) -> sqlite3.Connection:
        """Open Manifest.db read-only (falling back to a scratch copy)."""
        if self._conn is not None:
            return self._conn
        if self.legacy_mbdb:
            raise BackupError(
                f"{self.path} is a pre-iOS 10 backup (Manifest.mbdb); not supported"
            )
        if not self.manifest_db.exists():
            raise BackupError(f"no Manifest.db in {self.path}")
        try:
            conn = sqlite3.connect(
                f"file:{self.manifest_db}?mode=ro", uri=True, timeout=10
            )
            conn.execute("SELECT count(*) FROM Files LIMIT 1").fetchone()
        except sqlite3.Error:
            # A Manifest.db left with an unreplayed -wal cannot be opened
            # read-only; work on a copy instead of touching the backup.
            copy = self._scratch_copy(self.manifest_db, "Manifest.db")
            conn = sqlite3.connect(str(copy), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.text_factory = lambda b: b.decode("utf-8", "replace")
        self._conn = conn
        return conn

    def _scratch_copy(self, source: Path, name: str) -> Path:
        import tempfile

        if not hasattr(self, "_scratch"):
            self._scratch = Path(tempfile.mkdtemp(prefix="iphonebackup_"))
        target = self._scratch / name
        shutil.copy2(source, target)
        for suffix in ("-wal", "-shm"):
            side = source.with_name(source.name + suffix)
            if side.exists():
                shutil.copy2(side, target.with_name(target.name + suffix))
        return target

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        scratch = getattr(self, "_scratch", None)
        if scratch and scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)

    def __enter__(self) -> "Backup":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def files_columns(self) -> dict:
        """Actual ``Files`` columns, keyed by upper-case name -> real name."""
        if self._files_columns is None:
            rows = self.connect().execute("PRAGMA table_info(Files)").fetchall()
            if not rows:
                raise BackupError(f"{self.manifest_db} has no Files table")
            self._files_columns = {row["name"].upper(): row["name"] for row in rows}
        return self._files_columns

    def _col(self, *candidates: str) -> Optional[str]:
        cols = self.files_columns()
        for candidate in candidates:
            if candidate.upper() in cols:
                return cols[candidate.upper()]
        return None

    def stored_path(self, file_id: str) -> Optional[Path]:
        """Where a fileID's content lives: ``<backup>/ab/abcdef...``.

        Falls back to the flat pre-iOS 10 layout if the fanout path is absent.
        """
        if not file_id:
            return None
        fanned = self.path / file_id[:2] / file_id
        if fanned.exists():
            return fanned
        flat = self.path / file_id
        if flat.exists():
            return flat
        return None

    def iter_files(
        self,
        domain: Optional[str] = None,
        domain_like: Optional[str] = None,
        path_like: Optional[str] = None,
        files_only: bool = True,
        limit: Optional[int] = None,
    ) -> Iterator[ManifestFile]:
        """Query the Files table without assuming exact column names."""
        cols = self.files_columns()
        id_col = self._col("fileID", "fileId", "id")
        domain_col = self._col("domain")
        path_col = self._col("relativePath", "relative_path")
        flags_col = self._col("flags")
        blob_col = self._col("file")
        if not (id_col and domain_col and path_col):
            raise BackupError(
                "unexpected Manifest.db Files schema; columns are: "
                + ", ".join(sorted(cols.values()))
            )

        selected = [id_col, domain_col, path_col]
        if flags_col:
            selected.append(flags_col)
        if blob_col:
            selected.append(blob_col)

        where, params = [], []
        if domain:
            where.append(f'"{domain_col}" = ?')
            params.append(domain)
        if domain_like:
            where.append(f'"{domain_col}" LIKE ?')
            params.append(domain_like)
        if path_like:
            where.append(f'"{path_col}" LIKE ?')
            params.append(path_like)
        if files_only and flags_col:
            where.append(f'"{flags_col}" = {FLAG_FILE}')

        sql = f'SELECT {", ".join(chr(34) + c + chr(34) for c in selected)} FROM Files'
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f' ORDER BY "{path_col}"'
        if limit:
            sql += f" LIMIT {int(limit)}"

        for row in self.connect().execute(sql, params):
            meta = parse_mbfile(row[blob_col]) if blob_col else {}
            yield ManifestFile(
                file_id=row[id_col],
                domain=row[domain_col],
                relative_path=row[path_col] or "",
                flags=int(row[flags_col]) if flags_col else FLAG_FILE,
                size=meta.get("Size"),
                modified=meta.get("LastModified"),
                birth=meta.get("Birth"),
                backup=self,
            )

    def find_one(
        self, domain: str, relative_path: str
    ) -> Optional[ManifestFile]:
        for item in self.iter_files(domain=domain, path_like=relative_path, limit=1):
            return item
        return None

    def domains(self) -> list:
        """(domain, file count) pairs, most populous first."""
        domain_col = self._col("domain")
        sql = (
            f'SELECT "{domain_col}" AS d, count(*) AS n FROM Files '
            f'GROUP BY "{domain_col}" ORDER BY n DESC'
        )
        return [(row["d"], row["n"]) for row in self.connect().execute(sql)]

    def extract(self, item: ManifestFile, destination: Path) -> Path:
        """Copy one backed-up file out to ``destination`` (metadata preserved)."""
        source = self.stored_path(item.file_id)
        if source is None:
            raise BackupError(
                f"content for {item.domain}:{item.relative_path} "
                f"(fileID {item.file_id}) is missing from the backup"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination


def discover_backups(root: Path = DEFAULT_BACKUP_ROOT) -> list:
    """Every backup folder under ``root``, newest-looking first."""
    root = Path(root).expanduser()
    if not root.is_dir():
        return []
    found = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if (child / "Manifest.db").exists() or (child / "Manifest.mbdb").exists():
            found.append(Backup(child))
    found.sort(key=lambda b: str(b.last_backup_date), reverse=True)
    return found


def load_backup(target: str, root: Path = DEFAULT_BACKUP_ROOT) -> Backup:
    """Resolve a UDID, a partial UDID, a device name, or a path to a Backup."""
    candidate = Path(target).expanduser()
    if candidate.is_dir() and (
        (candidate / "Manifest.db").exists() or (candidate / "Manifest.mbdb").exists()
    ):
        return Backup(candidate)
    for backup in discover_backups(root):
        if backup.udid == target or backup.udid.startswith(target):
            return backup
    for backup in discover_backups(root):
        if backup.device_name.lower() == target.lower():
            return backup
    raise BackupError(f"no backup matching {target!r} under {root}")
