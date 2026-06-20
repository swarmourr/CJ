"""Storage fault implementation wrapping cj_storage."""

from __future__ import annotations
import os
import tempfile
from importlib.resources import files, as_file
from typing import TYPE_CHECKING

from chaos_jungle.faults.base import Fault

if TYPE_CHECKING:
    from chaos_jungle.targets.base import Target

# Sentinel: use bundled scripts (default)
_BUNDLED = "__bundled__"


class StorageCorrupt(Fault):
    """Corrupt files at block-device level using dd and cj_storage.  # noqa

    Schedules periodic bit-flip corruption via crontab. The original
    byte values are recorded in ``~/.chaos-jungle/cj.db`` on the target
    so corruptions can be reverted exactly.

    The required scripts (``cj_storage.py``, ``cj_corrupt.py``,
    ``cj_database.py``) are **bundled inside the package** and deployed
    automatically to ``~/.chaos-jungle/storage/`` on the target at
    ``start()`` time — no manual clone of the original repo needed.

    Parameters
    ----------
    pattern : str
        Glob pattern for files to corrupt, e.g. ``"*.pdb"``.
    directory : str
        Directory to search for matching files.
    interval : str
        Crontab frequency, e.g. ``"10m"`` (every 10 min) or ``"2h"``.
    recursive : bool, optional
        Search directory recursively. Default ``True``.

    Notes
    -----
    Requires ``sudo`` on the target (raw block access via ``dd``).
    System packages needed: ``python3``, ``e2fsprogs``, ``coreutils``,
    ``inotify-tools``. Python package needed: ``python-crontab``.

    Examples
    --------
    >>> fault = StorageCorrupt("*.pdb", "/data/input", interval="10m")
    """

    dependencies     = ["python3", "e2fsprogs", "inotify-tools", "coreutils"]
    pip_dependencies = ["python-crontab"]
    danger_level: int          = 2
    default_metrics: list[str] = ["read_errors", "parse_errors", "write_errors",
                                   "checksum_errors", "corrupted_files"]

    _INTERVAL_RE = __import__("re").compile(r"^\d+(\.\d+)?(s|m|h)$")

    def __init__(
        self,
        pattern: str,
        directory: str,
        interval: str = "10m",
        recursive: bool = True,
    ) -> None:
        if not pattern or not pattern.strip():
            raise ValueError(
                "StorageCorrupt requires 'pattern' — a glob like '*.pdb' or '*.dat'.\n"
                "  Example: StorageCorrupt('*.pdb', '/data')"
            )
        if not directory or not directory.strip():
            raise ValueError(
                "StorageCorrupt requires 'directory' — absolute path to the directory to watch.\n"
                "  Example: StorageCorrupt('*.pdb', '/scratch/data')"
            )
        if not directory.startswith("/"):
            raise ValueError(
                f"StorageCorrupt 'directory' must be an absolute path (starting with '/'), "
                f"got {directory!r}.\n"
                f"  Example: StorageCorrupt('*.pdb', '/scratch/data')"
            )
        if not self._INTERVAL_RE.match(interval.strip()):
            raise ValueError(
                f"StorageCorrupt 'interval' must be like '10m', '2h', '30s' — got {interval!r}."
            )
        self.pattern = pattern.strip()
        self.directory = directory.rstrip("/")
        self.interval = interval.strip()
        self.recursive = recursive
        self._deployed_path: str | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _deploy_scripts(self, target: "Target") -> str:
        """Upload bundled storage scripts to the target.

        Returns the absolute path to ``cj_storage.py`` on the target.
        """
        # Resolve remote home directory
        _, home, _ = target.run("echo $HOME")
        home = home.strip() or "/root"
        cj_home = f"{home}/.chaos-jungle"
        remote_dir = f"{cj_home}/storage"

        target.run(f"mkdir -p {remote_dir}")

        # Upload the three Python scripts from the bundled package data
        storage_pkg = files("chaos_jungle.scripts.storage")
        for name in ("cj_storage.py", "cj_corrupt.py", "cj_database.py"):
            with as_file(storage_pkg / name) as src:
                target.put(str(src), f"{remote_dir}/{name}")

        # Generate cj.cfg with the correct absolute paths for this target
        cfg_content = (
            "[Paths]\n"
            f"log_dir = {cj_home}\n"
            f"database_file = {cj_home}/cj.db\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".cfg", delete=False) as tmp:
            tmp.write(cfg_content)
            tmp_path = tmp.name
        try:
            target.put(tmp_path, f"{remote_dir}/cj.cfg")
        finally:
            os.unlink(tmp_path)

        return f"{remote_dir}/cj_storage.py"

    def _get_storage_path(self, target: "Target") -> str:
        """Deploy bundled scripts to the target and return path to cj_storage.py."""
        if self._deployed_path is None:
            self._deployed_path = self._deploy_scripts(target)
        return self._deployed_path

    # ------------------------------------------------------------------
    # Fault lifecycle
    # ------------------------------------------------------------------

    def start(self, target: "Target") -> None:
        path = self._get_storage_path(target)
        recursive_flag = "-r" if self.recursive else ""
        target.sudo(
            f"python3 {path} "
            f"-f '{self.pattern}' -d {self.directory} {recursive_flag} "
            f"--start -F {self.interval}"
        )

    def stop(self, target: "Target") -> None:
        path = self._get_storage_path(target)
        target.sudo(f"python3 {path} --stop")

    def revert(self, target: "Target") -> None:
        path = self._get_storage_path(target)
        target.sudo(f"python3 {path} --revert")

    def _parameters(self) -> dict:
        return {
            "pattern": self.pattern,
            "directory": self.directory,
            "interval": self.interval,
            "recursive": self.recursive,
        }


class StorageCorruptImmediate(Fault):
    """Corrupt a file immediately by overwriting bytes at a specific offset.

    Uses ``dd`` to write random bytes from ``/dev/urandom`` directly into
    the file at ``offset``.  The original bytes are backed up before
    overwriting so ``revert()`` can restore them exactly.

    Unlike :class:`StorageCorrupt` (which runs on a crontab schedule),
    this fault fires instantly at ``start()`` time — useful for injecting
    corruption at a precise moment during a test.

    Requires ``sudo`` on the target (raw block write via ``dd``).

    Parameters
    ----------
    file_path : str
        Absolute path to the file to corrupt.
    offset : int
        Byte offset at which to start writing random bytes. Default ``0``.
    byte_count : int
        Number of bytes to overwrite with random data. Default ``16``.

    Examples
    --------
    >>> fault = StorageCorruptImmediate("/data/model.bin", offset=1024, byte_count=32)
    """

    danger_level: int          = 2
    default_metrics: list[str] = ["read_errors", "parse_errors", "checksum_errors", "corrupted_files"]

    def __init__(self, file_path: str, offset: int = 0, byte_count: int = 16) -> None:
        if not file_path or not file_path.startswith("/"):
            raise ValueError(
                "StorageCorruptImmediate 'file_path' must be an absolute path."
            )
        if offset < 0:
            raise ValueError(f"StorageCorruptImmediate 'offset' must be >= 0, got {offset}.")
        if byte_count < 1:
            raise ValueError(f"StorageCorruptImmediate 'byte_count' must be >= 1, got {byte_count}.")
        self.file_path  = file_path
        self.offset     = offset
        self.byte_count = byte_count
        self._backup_path = file_path + ".cj_backup"

    def start(self, target: "Target") -> None:
        # Back up the original file
        target.run(f"cp '{self.file_path}' '{self._backup_path}'")
        # Overwrite byte_count bytes at offset with random data
        target.sudo(
            f"dd if=/dev/urandom of='{self.file_path}' "
            f"bs=1 count={self.byte_count} seek={self.offset} conv=notrunc 2>/dev/null"
        )

    def stop(self, target: "Target") -> None:
        pass  # revert() restores the file

    def revert(self, target: "Target") -> None:
        _, exists, _ = target.run(f"test -f '{self._backup_path}' && echo yes || echo no")
        if exists.strip() == "yes":
            target.run(f"mv '{self._backup_path}' '{self.file_path}'")

    def _parameters(self) -> dict:
        return {
            "file_path":  self.file_path,
            "offset":     self.offset,
            "byte_count": self.byte_count,
        }


class SQLiteCorrupt(Fault):
    """Corrupt a page in a SQLite database file using ``dd``.

    Overwrites one full page of the SQLite database with random bytes.
    SQLite will detect the checksum mismatch and raise
    ``sqlite3.DatabaseError: database disk image is malformed`` on the
    next read.

    The original file is backed up so ``revert()`` can restore it.

    Requires ``sudo`` on the target.

    Parameters
    ----------
    db_path : str
        Absolute path to the SQLite ``.db`` file.
    page : int
        Zero-based page index to corrupt. Page 0 is the header/root page
        (most disruptive); higher pages target specific B-tree nodes.
        Default ``1`` (first data page — avoids the file header).
    page_size : int
        SQLite page size in bytes. Default ``4096`` (SQLite default).

    Examples
    --------
    >>> fault = SQLiteCorrupt("/var/agent/state.db")
    >>> fault = SQLiteCorrupt("/var/agent/state.db", page=2, page_size=4096)
    """

    danger_level: int          = 2
    default_metrics: list[str] = ["read_errors", "parse_errors", "query_errors", "corrupted_files"]

    def __init__(self, db_path: str, page: int = 1, page_size: int = 4096) -> None:
        if not db_path or not db_path.startswith("/"):
            raise ValueError("SQLiteCorrupt 'db_path' must be an absolute path.")
        if page < 0:
            raise ValueError(f"SQLiteCorrupt 'page' must be >= 0, got {page}.")
        if page_size < 512:
            raise ValueError(f"SQLiteCorrupt 'page_size' must be >= 512, got {page_size}.")
        self.db_path   = db_path
        self.page      = page
        self.page_size = page_size
        self._backup_path = db_path + ".cj_backup"

    def start(self, target: "Target") -> None:
        target.run(f"cp '{self.db_path}' '{self._backup_path}'")
        target.sudo(
            f"dd if=/dev/urandom of='{self.db_path}' "
            f"bs={self.page_size} count=1 seek={self.page} conv=notrunc 2>/dev/null"
        )

    def stop(self, target: "Target") -> None:
        pass

    def revert(self, target: "Target") -> None:
        _, exists, _ = target.run(f"test -f '{self._backup_path}' && echo yes || echo no")
        if exists.strip() == "yes":
            target.run(f"mv '{self._backup_path}' '{self.db_path}'")

    def _parameters(self) -> dict:
        return {
            "db_path":   self.db_path,
            "page":      self.page,
            "page_size": self.page_size,
        }
