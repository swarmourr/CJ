from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from chaos_jungle.targets.docker import DockerTarget


pytestmark = pytest.mark.docker


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    proc = subprocess.run(["docker", "version"], capture_output=True, text=True, timeout=10)
    return proc.returncode == 0


def _local_image() -> str | None:
    image = os.environ.get("CJ_EVAL_TEST_IMAGE", "python:3.12-slim")
    proc = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return image if proc.returncode == 0 else None


docker_available = pytest.mark.skipif(not _docker_available(), reason="Docker daemon is unavailable")


@docker_available
def test_docker_target_real_container_non_root_timeout_and_cleanup():
    image = _local_image()
    if not image:
        pytest.skip("CJ_EVAL_TEST_IMAGE or python:3.12-slim is not available locally")

    create = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--user",
            "1000:1000",
            "--cpus",
            "0.5",
            "--memory",
            "128m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            image,
            "sleep",
            "60",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert create.returncode == 0, create.stderr
    cid = create.stdout.strip()
    try:
        target = DockerTarget(cid, timeout_s=1)
        code, out, err = target.run("id -u")
        assert code == 0
        assert out.strip() == "1000"
        code, out, err = target.run("python -c 'import time; time.sleep(5)'")
        assert code == 124

        inspect = subprocess.run(
            ["docker", "inspect", cid, "--format", "{{json .HostConfig}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        host_config = json.loads(inspect.stdout)
        assert host_config["Memory"] > 0
        assert host_config["NanoCpus"] > 0
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, text=True, timeout=20)
    gone = subprocess.run(["docker", "inspect", cid], capture_output=True, text=True, timeout=10)
    assert gone.returncode != 0
