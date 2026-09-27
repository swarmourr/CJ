"""Shared pytest fixtures and test utilities."""
from __future__ import annotations
import threading
from typing import Callable
import pytest

from chaos_jungle.faults.base import Fault, VerificationResult


class MockTarget:  # not a Fault subclass — it's a target double
    """Test double for Target that records all commands and returns configurable responses."""

    def __init__(self, responses: dict | None = None):
        # responses: dict mapping command substring → (exit_code, stdout, stderr)
        self.responses: dict[str, tuple] = responses or {}
        self.commands: list[tuple[str, bool]] = []   # (cmd, privileged)

    def _respond(self, cmd: str) -> tuple[int, str, str]:
        for pattern, response in self.responses.items():
            if pattern in cmd:
                return response
        return (0, "", "")

    def run(self, cmd: str) -> tuple[int, str, str]:
        self.commands.append((cmd, False))
        return self._respond(cmd)

    def sudo(self, cmd: str) -> tuple[int, str, str]:
        self.commands.append((cmd, True))
        return self._respond(cmd)

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def put(self, local: str, remote: str) -> None:
        pass

    def get(self, remote: str, local: str) -> None:
        pass


class FailingFault(Fault):
    """Fake fault whose start() always raises."""
    danger_level = 0
    default_metrics: list = []
    dependencies: list = []
    pip_dependencies: list = []

    def __init__(self, exc: Exception | None = None):
        self._exc = exc or RuntimeError("FailingFault injection error")
        self.started = False
        self.stopped = False
        self.reverted = False

    def start(self, target):
        raise self._exc

    def stop(self, target):
        self.stopped = True

    def revert(self, target):
        self.reverted = True

    def preflight(self, target, auto_install=False):
        pass

    def dry_run(self, target):
        pass

    def _parameters(self):
        return {}

    def verify_active(self, target):
        from chaos_jungle.faults.base import VerificationResult
        return VerificationResult(verified=False, reason="not started")

    def verify_recovered(self, target):
        from chaos_jungle.faults.base import VerificationResult
        return VerificationResult(verified=True, reason="never started")


class TrackingFault(Fault):
    """Fake fault that records lifecycle calls and optionally fails on stop."""
    danger_level = 0
    default_metrics: list = []
    dependencies: list = []
    pip_dependencies: list = []

    def __init__(self, fail_on_stop: bool = False):
        self._fail_on_stop = fail_on_stop
        self.started = False
        self.stopped = False
        self.reverted = False

    def start(self, target):
        self.started = True

    def stop(self, target):
        if self._fail_on_stop:
            raise RuntimeError("TrackingFault stop error")
        self.stopped = True

    def revert(self, target):
        self.reverted = True

    def preflight(self, target, auto_install=False):
        pass

    def dry_run(self, target):
        pass

    def _parameters(self):
        return {}

    def verify_active(self, target):
        from chaos_jungle.faults.base import VerificationResult
        return VerificationResult(verified=self.started, reason="tracking")

    def verify_recovered(self, target):
        from chaos_jungle.faults.base import VerificationResult
        return VerificationResult(verified=self.stopped or self.reverted, reason="tracking")


@pytest.fixture
def mock_target():
    return MockTarget()
