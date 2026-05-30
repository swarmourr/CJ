"""Local target — runs commands on the current machine via subprocess."""

from __future__ import annotations
import shutil
import subprocess

from chaos_jungle.targets.base import Target


class LocalTarget(Target):
    """Run fault commands on the local machine.

    No connection needed. ``sudo`` prepends ``sudo`` to the command.
    Suitable for development and single-node testing.

    Examples
    --------
    >>> target = LocalTarget()
    >>> code, out, err = target.run("hostname")
    """

    def connect(self) -> None:
        pass  # nothing to connect to

    def disconnect(self) -> None:
        pass

    def run(self, cmd: str) -> tuple[int, str, str]:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stdout, result.stderr

    def sudo(self, cmd: str) -> tuple[int, str, str]:
        return self.run(f"sudo {cmd}")

    def put(self, local_path: str, remote_path: str) -> None:
        shutil.copy2(local_path, remote_path)

    def get(self, remote_path: str, local_path: str) -> None:
        shutil.copy2(remote_path, local_path)
