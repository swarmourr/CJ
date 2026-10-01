"""Docker-backed chaos target.

The Docker target is intentionally narrow: every command is executed with
``docker exec`` inside one validated container, and failures never fall back to
the host. It is designed for evaluation campaigns where network/resource faults
must be confined to a disposable experiment container.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path

from chaos_jungle.targets.base import Target


_CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_UNSAFE_PATTERNS = (
    "/var/run/docker.sock",
    "docker ",
    "docker\t",
    "nsenter",
    "--privileged",
    "host.docker.internal",
    "rm -rf /",
)


class DockerTarget(Target):
    """Run target commands inside a specific Docker container only."""

    def __init__(
        self,
        container_id: str,
        *,
        timeout_s: float = 30.0,
        docker_bin: str = "docker",
        exec_user: str | None = None,
    ) -> None:
        if not _CONTAINER_RE.match(container_id):
            raise ValueError(f"invalid Docker container identifier: {container_id!r}")
        self.container_id = container_id
        self.timeout_s = timeout_s
        self.docker_bin = docker_bin
        self.exec_user = exec_user
        self._connected = False

    def connect(self) -> None:
        if shutil.which(self.docker_bin) is None:
            raise RuntimeError(f"Docker executable not found: {self.docker_bin!r}")
        code, out, err = self._docker(
            [
                "inspect",
                "--format",
                "{{.State.Running}}",
                self.container_id,
            ],
            timeout=self.timeout_s,
        )
        if code != 0:
            raise RuntimeError(
                f"Docker container {self.container_id!r} is unavailable: {err or out}"
            )
        if out.strip().lower() != "true":
            raise RuntimeError(f"Docker container {self.container_id!r} is not running")
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def run(self, cmd: str) -> tuple[int, str, str]:
        self._ensure_connected()
        self._validate_command(cmd)
        args = ["exec"]
        if self.exec_user:
            args += ["--user", self.exec_user]
        args += [self.container_id, "/bin/sh", "-lc", cmd]
        return self._docker(args, timeout=self.timeout_s)

    def sudo(self, cmd: str) -> tuple[int, str, str]:
        self._ensure_connected()
        self._validate_command(cmd)
        args = ["exec", "--user", "0", self.container_id, "/bin/sh", "-lc", cmd]
        return self._docker(args, timeout=self.timeout_s)

    def put(self, local_path: str, remote_path: str) -> None:
        self._ensure_connected()
        self._validate_container_path(remote_path)
        src = Path(local_path)
        if not src.exists():
            raise FileNotFoundError(local_path)
        code, out, err = self._docker(
            ["cp", str(src), f"{self.container_id}:{remote_path}"],
            timeout=self.timeout_s,
        )
        if code != 0:
            raise RuntimeError(f"docker cp to container failed: {err or out}")

    def get(self, remote_path: str, local_path: str) -> None:
        self._ensure_connected()
        self._validate_container_path(remote_path)
        code, out, err = self._docker(
            ["cp", f"{self.container_id}:{remote_path}", local_path],
            timeout=self.timeout_s,
        )
        if code != 0:
            raise RuntimeError(f"docker cp from container failed: {err or out}")

    def _ensure_connected(self) -> None:
        if not self._connected:
            self.connect()

    def _validate_command(self, cmd: str) -> None:
        lowered = cmd.lower()
        for pattern in _UNSAFE_PATTERNS:
            if pattern in lowered:
                raise ValueError(
                    f"unsafe command rejected for DockerTarget {self.container_id!r}"
                )

    def _validate_container_path(self, path: str) -> None:
        if not path.startswith("/"):
            raise ValueError("container paths must be absolute")
        if "/../" in path or path.endswith("/.."):
            raise ValueError("container path traversal is not allowed")

    def _docker(self, args: list[str], *, timeout: float) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                [self.docker_bin, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            err = exc.stderr or ""
            if isinstance(out, bytes):
                out = out.decode(errors="replace")
            if isinstance(err, bytes):
                err = err.decode(errors="replace")
            return 124, out, err or f"docker command timed out after {timeout}s"


class DockerContainerControllerTarget(Target):
    """Host-side Docker controller restricted to one experiment container.

    This target is for faults whose target is the container object itself, such
    as :class:`chaos_jungle.faults.process.ContainerKill`. It accepts only the
    small command forms emitted by that fault and rejects every other host
    operation.
    """

    _ACTIONS = {"inspect", "kill", "stop", "pause", "rm", "unpause", "start"}

    def __init__(
        self,
        container_id: str,
        *,
        timeout_s: float = 30.0,
        docker_bin: str = "docker",
    ) -> None:
        if not _CONTAINER_RE.match(container_id):
            raise ValueError(f"invalid Docker container identifier: {container_id!r}")
        self.container_id = container_id
        self.timeout_s = timeout_s
        self.docker_bin = docker_bin
        self._connected = False

    def connect(self) -> None:
        if shutil.which(self.docker_bin) is None:
            raise RuntimeError(f"Docker executable not found: {self.docker_bin!r}")
        code, _, err = self._docker(["inspect", self.container_id], timeout=self.timeout_s)
        if code != 0:
            raise RuntimeError(f"Docker container {self.container_id!r} is unavailable: {err}")
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def run(self, cmd: str) -> tuple[int, str, str]:
        self._ensure_connected()
        args, fallback = self._parse_allowed(cmd)
        code, out, err = self._docker(args, timeout=self.timeout_s)
        if code != 0 and fallback is not None:
            return 0, fallback + "\n", err
        return code, out, err

    def sudo(self, cmd: str) -> tuple[int, str, str]:
        return self.run(cmd)

    def put(self, local_path: str, remote_path: str) -> None:  # noqa: ARG002
        raise NotImplementedError("DockerContainerControllerTarget does not transfer files")

    def get(self, remote_path: str, local_path: str) -> None:  # noqa: ARG002
        raise NotImplementedError("DockerContainerControllerTarget does not transfer files")

    def _ensure_connected(self) -> None:
        if not self._connected:
            self.connect()

    def _parse_allowed(self, cmd: str) -> tuple[list[str], str | None]:
        fallback = None
        if "|| echo false" in cmd:
            fallback = "false"
            cmd = cmd.split("|| echo false", 1)[0]
        if "|| echo missing" in cmd:
            fallback = "missing"
            cmd = cmd.split("|| echo missing", 1)[0]
        cmd = cmd.replace("2>/dev/null", "").replace("|| true", "").strip()
        parts = shlex.split(cmd)
        if len(parts) < 3 or parts[0] != "docker":
            raise ValueError("only Docker commands emitted by ContainerKill are allowed")
        action = parts[1]
        if action not in self._ACTIONS:
            raise ValueError(f"Docker action {action!r} is not allowed")
        if action == "inspect":
            if self.container_id not in parts:
                raise ValueError("Docker inspect must target the experiment container")
            if parts[-1] != self.container_id:
                raise ValueError("Docker inspect target must be the final argument")
            return parts[1:], fallback
        if len(parts) != 3 or parts[2] != self.container_id:
            raise ValueError("Docker container action must target only the experiment container")
        return parts[1:], fallback

    def _docker(self, args: list[str], *, timeout: float) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                [self.docker_bin, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            err = exc.stderr or ""
            if isinstance(out, bytes):
                out = out.decode(errors="replace")
            if isinstance(err, bytes):
                err = err.decode(errors="replace")
            return 124, out, err or f"docker command timed out after {timeout}s"
