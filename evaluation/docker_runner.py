"""Docker execution backend for CJ evaluation campaigns."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_SECRET_NAME_RE = re.compile(r"(secret|password|credential)", re.I)
_SECRET_ENV_NAME_RE = re.compile(r"(api[_-]?key|token|secret|password|credential)", re.I)
_SECRET_EXACT_KEYS = {
    "api_key",
    "access_token",
    "refresh_token",
    "secret_key",
    "password",
}
_SAFE_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def redact_secrets(value: Any) -> Any:
    """Return *value* with likely secrets removed from nested structures."""
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_l = str(key).lower()
            if key_l in _SECRET_EXACT_KEYS or _SECRET_NAME_RE.search(key_l):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_secrets(item)
        return redacted
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        if value.startswith(("sk-", "Bearer ")):
            return "[REDACTED]"
    return value


@dataclass
class DockerExecutionConfig:
    """Host-side Docker settings shared by paired executions."""

    image: str
    cpus: float = 1.0
    memory: str = "2g"
    timeout_s: float = 300.0
    user: str = "1000:1000"
    network: str = "bridge"
    read_only_root: bool = True
    capabilities: list[str] = field(default_factory=list)
    workdir: str = "/cj/work"
    entrypoint_module: str = "evaluation.container_entrypoint"
    docker_bin: str = "docker"
    preserve_io: bool = False
    env_file: str | None = None
    add_host_gateway: bool = True

    def resource_limits(self) -> dict[str, Any]:
        return {
            "cpus": self.cpus,
            "memory": self.memory,
            "user": self.user,
            "network": self.network,
            "read_only_root": self.read_only_root,
            "capabilities": sorted(self.capabilities),
            "add_host_gateway": self.add_host_gateway,
        }


@dataclass
class RunContext:
    """Identity and metadata passed from the host orchestrator to Docker."""

    study_id: str
    campaign_id: str
    pair_id: str
    run_id: str
    condition: str
    output_root: str | None = None
    environment: dict[str, str] = field(default_factory=dict)


@dataclass
class PreparedContainer:
    container_id: str
    image: str
    image_digest: str
    input_dir: str
    output_dir: str
    request_path: str
    resource_limits: dict[str, Any]
    created_at: float


@dataclass
class DockerExecution:
    container: PreparedContainer
    exec_command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float


@dataclass
class DockerAgentRunResult:
    container_id: str
    exit_code: int
    timed_out: bool
    duration_s: float
    stdout: str
    stderr: str
    result: dict[str, Any]
    image: str
    image_digest: str
    resource_limits: dict[str, Any]
    artifact_paths: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return redact_secrets({
            "container_id": self.container_id,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_s": self.duration_s,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "result": self.result,
            "image": self.image,
            "image_digest": self.image_digest,
            "resource_limits": self.resource_limits,
            "artifact_paths": self.artifact_paths,
        })


class DockerAgentRunner:
    """Prepare, execute, collect, and clean up one fresh experiment container."""

    def __init__(self, config: DockerExecutionConfig) -> None:
        self.config = config

    def prepare(
        self,
        *,
        agent_system: str,
        topology: str,
        task: dict[str, Any],
        seed: int,
        model_config: dict[str, Any],
        execution_config: dict[str, Any] | None,
        run_context: RunContext,
    ) -> PreparedContainer:
        self._require_docker()
        output_root = Path(
            run_context.output_root or tempfile.mkdtemp(prefix="cj-docker-run-")
        ).expanduser().resolve()
        input_dir = output_root / "input"
        output_dir = output_root / "output"
        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        request = {
            "agent_system": agent_system,
            "topology": topology,
            "task": task,
            "seed": seed,
            "model_config": redact_secrets(model_config),
            "execution_config": execution_config or {},
            "run_context": redact_secrets(run_context.__dict__),
        }
        request_bytes = json.dumps(request, sort_keys=True, indent=2).encode()
        request_path = input_dir / "request.json"
        request_path.write_bytes(request_bytes)
        (input_dir / "request.sha256").write_text(
            hashlib.sha256(request_bytes).hexdigest() + "\n",
            encoding="utf-8",
        )

        digest = self._image_digest(self.config.image)
        name = f"cj-eval-{run_context.run_id[:24]}-{uuid.uuid4().hex[:8]}"
        create_cmd = self._create_command(name, input_dir, output_dir, run_context.environment)
        create = self._run(create_cmd, timeout=60)
        if create.returncode != 0:
            raise RuntimeError(f"docker create failed: {create.stderr or create.stdout}")
        container_id = create.stdout.strip()
        return PreparedContainer(
            container_id=container_id,
            image=self.config.image,
            image_digest=digest,
            input_dir=str(input_dir),
            output_dir=str(output_dir),
            request_path="/cj/input/request.json",
            resource_limits=self.config.resource_limits(),
            created_at=time.time(),
        )

    def exec_agent(
        self,
        container: PreparedContainer,
        *,
        request_path: str | None = None,
        output_path: str = "/cj/output/result.json",
    ) -> DockerExecution:
        start = time.time()
        if not self.is_running(container):
            start_proc = self.start_container(container)
            if start_proc.returncode != 0:
                return DockerExecution(
                    container=container,
                    exec_command=[self.config.docker_bin, "start", container.container_id],
                    exit_code=start_proc.returncode,
                    stdout=start_proc.stdout,
                    stderr=start_proc.stderr,
                    timed_out=False,
                    duration_s=time.time() - start,
                )

        cmd = [
            self.config.docker_bin,
            "exec",
            "--workdir",
            self.config.workdir,
            container.container_id,
            "python",
            "-m",
            self.config.entrypoint_module,
            "--request",
            request_path or container.request_path,
            "--output",
            output_path,
        ]
        try:
            proc = self._run(cmd, timeout=self.config.timeout_s)
            timed_out = False
            exit_code = proc.returncode
            stdout = proc.stdout
            stderr = proc.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = 124
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            stderr = stderr or f"agent execution timed out after {self.config.timeout_s}s"
        return DockerExecution(
            container=container,
            exec_command=cmd,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_s=time.time() - start,
        )

    def collect(self, execution: DockerExecution) -> DockerAgentRunResult:
        result_path = Path(execution.container.output_dir) / "result.json"
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                result = {
                    "executor_error": f"invalid AgentRunResult JSON: {exc}",
                    "success": 0.0,
                    "reported_error": 1.0,
                }
        else:
            result = {
                "executor_error": "AgentRunResult JSON was not produced",
                "success": 0.0,
                "reported_error": 1.0,
            }
        return DockerAgentRunResult(
            container_id=execution.container.container_id,
            exit_code=execution.exit_code,
            timed_out=execution.timed_out,
            duration_s=execution.duration_s,
            stdout=execution.stdout,
            stderr=execution.stderr,
            result=redact_secrets(result),
            image=execution.container.image,
            image_digest=execution.container.image_digest,
            resource_limits=execution.container.resource_limits,
            artifact_paths={
                "input_dir": execution.container.input_dir,
                "output_dir": execution.container.output_dir,
                "result_json": str(result_path),
            },
        )

    def cleanup(self, container: PreparedContainer) -> None:
        self._run(
            [self.config.docker_bin, "rm", "-f", container.container_id],
            timeout=60,
        )
        if not self.config.preserve_io:
            root = Path(container.input_dir).parent
            if root.name.startswith("cj-docker-run-"):
                shutil.rmtree(root, ignore_errors=True)

    def start_container(self, container: PreparedContainer) -> subprocess.CompletedProcess:
        return self._run([self.config.docker_bin, "start", container.container_id], timeout=60)

    def is_running(self, container: PreparedContainer) -> bool:
        proc = self._run(
            [
                self.config.docker_bin,
                "inspect",
                "--format",
                "{{.State.Running}}",
                container.container_id,
            ],
            timeout=30,
        )
        return proc.returncode == 0 and proc.stdout.strip().lower() == "true"

    def run(
        self,
        *,
        agent_system: str,
        topology: str,
        task: dict[str, Any],
        seed: int,
        model_config: dict[str, Any],
        execution_config: dict[str, Any] | None,
        run_context: RunContext,
    ) -> DockerAgentRunResult:
        container = self.prepare(
            agent_system=agent_system,
            topology=topology,
            task=task,
            seed=seed,
            model_config=model_config,
            execution_config=execution_config,
            run_context=run_context,
        )
        try:
            execution = self.exec_agent(container)
            return self.collect(execution)
        finally:
            self.cleanup(container)

    def _create_command(
        self,
        name: str,
        input_dir: Path,
        output_dir: Path,
        env: dict[str, str],
    ) -> list[str]:
        cmd = [
            self.config.docker_bin,
            "create",
            "--name",
            name,
            "--user",
            self.config.user,
            "--cpus",
            str(self.config.cpus),
            "--memory",
            self.config.memory,
            "--network",
            self.config.network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            f"type=bind,source={input_dir},target=/cj/input,readonly",
            "--mount",
            f"type=bind,source={output_dir},target=/cj/output",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
        ]
        if self.config.add_host_gateway:
            cmd += ["--add-host", "host.docker.internal:host-gateway"]
        if self.config.read_only_root:
            cmd.append("--read-only")
        for cap in sorted(set(self.config.capabilities)):
            cmd += ["--cap-add", cap]
        if self.config.env_file:
            cmd += ["--env-file", self.config.env_file]
        for key, value in sorted(env.items()):
            if not _SAFE_ENV_NAME_RE.match(key):
                raise ValueError(f"unsafe environment variable name: {key!r}")
            if _SECRET_ENV_NAME_RE.search(key):
                # Let callers provide secrets through env-file or host secret
                # mechanisms; do not bake them into command arguments.
                raise ValueError(f"secret environment value cannot be passed as an argument: {key}")
            cmd += ["--env", f"{key}={value}"]
        cmd += [self.config.image, "sleep", "infinity"]
        return cmd

    def _image_digest(self, image: str) -> str:
        proc = self._run(
            [self.config.docker_bin, "image", "inspect", image, "--format", "{{json .RepoDigests}}"],
            timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"docker image inspect failed: {proc.stderr or proc.stdout}")
        try:
            digests = json.loads(proc.stdout.strip() or "[]")
        except json.JSONDecodeError:
            digests = []
        if digests:
            return str(digests[0])
        image_id = self._run(
            [self.config.docker_bin, "image", "inspect", image, "--format", "{{.Id}}"],
            timeout=60,
        )
        if image_id.returncode == 0 and image_id.stdout.strip():
            return image_id.stdout.strip()
        raise RuntimeError(f"image {image!r} has no inspectable digest or ID")

    def _require_docker(self) -> None:
        if shutil.which(self.config.docker_bin) is None:
            raise RuntimeError(f"Docker executable not found: {self.config.docker_bin!r}")

    def _run(self, cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
