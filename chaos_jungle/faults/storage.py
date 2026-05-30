"""Storage fault implementation wrapping cj_storage."""

from __future__ import annotations
from typing import TYPE_CHECKING

from chaos_jungle.faults.base import Fault

if TYPE_CHECKING:
    from chaos_jungle.targets.base import Target

# Path to cj_storage.py on the target machine
_CJ_STORAGE = "~/chaos-jungle/storage/cj_storage.py"


class StorageCorrupt(Fault):
    """Corrupt files at block-device level using dd and cj_storage.

    Schedules periodic bit-flip corruption via crontab. The original
    byte values are recorded in the existing ``cj.db`` SQLite database
    so corruptions can be reverted exactly.

    Parameters
    ----------
    pattern : str
        Glob pattern for files to corrupt, e.g. ``"*.pdb"``.
    directory : str
        Directory to search for matching files.
    interval : str
        Crontab frequency, e.g. ``"10m"`` for every 10 minutes.
    recursive : bool, optional
        Search directory recursively. Default ``True``.
    cj_storage_path : str, optional
        Path to ``cj_storage.py`` on the target machine.

    Notes
    -----
    This fault requires ``sudo`` on the target machine because it reads
    and writes raw block devices via ``dd``.

    Examples
    --------
    >>> fault = StorageCorrupt("*.pdb", "/data/input", interval="10m")
    """

    # System packages (apt)
    dependencies = ["python3", "e2fsprogs", "inotify-tools", "coreutils"]
    # Also requires on target: pip3 install python-crontab chaos-jungle

    def __init__(
        self,
        pattern: str,
        directory: str,
        interval: str = "10m",
        recursive: bool = True,
        cj_storage_path: str = _CJ_STORAGE,
    ) -> None:
        self.pattern = pattern
        self.directory = directory
        self.interval = interval
        self.recursive = recursive
        self.cj_storage_path = cj_storage_path

    def start(self, target: "Target") -> None:
        recursive_flag = "-r" if self.recursive else ""
        target.sudo(
            f"python3 {self.cj_storage_path} "
            f"-f '{self.pattern}' -d {self.directory} {recursive_flag} "
            f"--start -F {self.interval}"
        )

    def stop(self, target: "Target") -> None:
        target.sudo(f"python3 {self.cj_storage_path} --stop")

    def revert(self, target: "Target") -> None:
        target.sudo(f"python3 {self.cj_storage_path} --revert")

    def _parameters(self) -> dict:
        return {
            "pattern": self.pattern,
            "directory": self.directory,
            "interval": self.interval,
            "recursive": self.recursive,
        }
