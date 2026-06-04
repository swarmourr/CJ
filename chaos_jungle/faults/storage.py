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
    """Corrupt files at block-device level using dd and cj_storage.

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

    dependencies = ["python3", "e2fsprogs", "inotify-tools", "coreutils"]
    pip_dependencies = ["python-crontab"]

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
        """Return the path to cj_storage.py on the target, deploying if needed."""
        if self.cj_storage_path != _BUNDLED:
            return self.cj_storage_path
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
