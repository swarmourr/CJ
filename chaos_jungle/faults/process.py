"""Process, service, and container fault implementations.


These faults operate at the OS/runtime layer — killing processes,
stopping/crashing systemd services, and killing Docker containers.
All require an SSHTarget (or a LocalTarget with appropriate permissions).

Classes
-------
ProcessKill     — kill one or more processes matching a name/command pattern
ServiceFault    — stop, restart, kill, or mask a systemd service
ContainerKill   — kill, stop, pause, or remove a Docker container
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from chaos_jungle.faults.base import Fault, VerificationResult

if TYPE_CHECKING:
    from chaos_jungle.targets.base import Target


class ProcessKill(Fault):
    """Kill processes matching a name or command pattern.

    Uses ``pkill -f`` to match against the full command line.  The PIDs
    that existed before the kill are captured and stored in
    ``killed_pids`` for reporting.

    This fault is **irreversible** — ``stop()`` and ``revert()`` are
    no-ops because a killed process cannot be automatically restarted.
    Start the process yourself in your workload's cleanup step if needed.

    Parameters
    ----------
    pattern : str
        Pattern matched against the full command line (passed to
        ``pkill -f``).  Example: ``"gunicorn"``, ``"my_agent.py"``.
    signal : str
        Signal name without the ``SIG`` prefix. Default ``"KILL"``.
        Other useful values: ``"TERM"``, ``"HUP"``, ``"STOP"``.

    Examples
    --------
    >>> fault = ProcessKill("gunicorn")
    >>> fault = ProcessKill("my_worker.py", signal="TERM")
    """

    dependencies: list[str]    = ["procps"]
    danger_level: int          = 2
    default_metrics: list[str] = ["downtime_s", "recovery_time_s", "error_rate", "restart_count"]

    def __init__(self, pattern: str, signal: str = "KILL") -> None:
        if not pattern or not pattern.strip():
            raise ValueError("ProcessKill requires a non-empty 'pattern'.")
        self.pattern = pattern.strip()
        self.signal = signal.upper().lstrip("SIG")
        self.killed_pids: list[str] = []

    def start(self, target: "Target") -> None:
        # Capture matching PIDs before killing (for metrics / reporting)
        _, out, _ = target.run(
            f"pgrep -f '{self.pattern}' 2>/dev/null || true"
        )
        self.killed_pids = [p.strip() for p in out.splitlines() if p.strip()]

        target.run(
            f"pkill -SIG{self.signal} -f '{self.pattern}' 2>/dev/null || true"
        )

    def stop(self, target: "Target") -> None:
        pass  # irreversible — cannot restart an arbitrary killed process

    def revert(self, target: "Target") -> None:
        pass

    def verify_active(self, target: "Target") -> VerificationResult:
        """Verify the target process is no longer running (kill succeeded)."""
        code, stdout, _ = target.run(
            f"pgrep -f '{self.pattern}' 2>/dev/null || true"
        )
        pids = [p.strip() for p in stdout.splitlines() if p.strip()]
        process_gone = len(pids) == 0
        return VerificationResult(
            verified=process_gone,
            reason=(
                f"Process matching {self.pattern!r} is gone"
                if process_gone
                else f"Process still running: PIDs {pids}"
            ),
            observed={"remaining_pids": pids},
            timestamp_s=time.time(),
        )

    def verify_recovered(self, target: "Target") -> VerificationResult:
        """ProcessKill is irreversible — recovery means accepting the process is gone."""
        return VerificationResult(
            verified=True,
            reason="ProcessKill is irreversible — no automatic recovery expected",
            timestamp_s=time.time(),
        )

    def _parameters(self) -> dict:
        return {
            "pattern":     self.pattern,
            "signal":      self.signal,
            "killed_pids": self.killed_pids,
        }


class ServiceFault(Fault):
    """Stop, restart, kill, or mask a systemd service.

    On ``stop()``, the service is restored to its original state
    (started if it was running; unmasked if the action was ``mask``).

    Parameters
    ----------
    service : str
        Systemd unit name, e.g. ``"nginx"``, ``"postgresql"``.
    action : str
        One of ``"stop"``, ``"restart"``, ``"kill"``, ``"mask"``.
        Default ``"stop"``.

    Examples
    --------
    >>> fault = ServiceFault("nginx")
    >>> fault = ServiceFault("redis", action="restart")
    >>> fault = ServiceFault("postgresql", action="mask")
    """

    dependencies: list[str]    = ["systemd"]
    danger_level: int          = 2
    default_metrics: list[str] = ["downtime_s", "error_rate", "restart_count", "duration_s"]

    VALID_ACTIONS = ("stop", "restart", "kill", "mask")

    def __init__(self, service: str, action: str = "stop") -> None:
        if not service or not service.strip():
            raise ValueError("ServiceFault requires a non-empty 'service' name.")
        if action not in self.VALID_ACTIONS:
            raise ValueError(
                f"ServiceFault 'action' must be one of {self.VALID_ACTIONS}, got {action!r}."
            )
        self.service = service.strip()
        self.action = action
        self._was_active = False

    def start(self, target: "Target") -> None:
        _, out, _ = target.run(
            f"systemctl is-active {self.service} 2>/dev/null || true"
        )
        self._was_active = out.strip() == "active"
        target.sudo(f"systemctl {self.action} {self.service}")

    def stop(self, target: "Target") -> None:
        if self.action == "mask":
            target.sudo(f"systemctl unmask {self.service} 2>/dev/null || true")
        if self._was_active:
            target.sudo(f"systemctl start {self.service} 2>/dev/null || true")

    def revert(self, target: "Target") -> None:
        self.stop(target)

    def verify_active(self, target: "Target") -> VerificationResult:
        """Verify the service is in the expected fault state."""
        code, stdout, _ = target.run(
            f"systemctl is-active {self.service} 2>/dev/null || true"
        )
        current_state = stdout.strip()
        if self.action in ("stop", "kill", "mask"):
            # Fault is active when service is NOT running
            is_down = current_state not in ("active", "activating")
            return VerificationResult(
                verified=is_down,
                reason=f"{self.service!r} state={current_state!r} "
                       f"({'down as expected' if is_down else 'still running'})",
                observed={"state": current_state},
                timestamp_s=time.time(),
            )
        # action=restart: always transient; consider active=ok
        return VerificationResult(
            verified=True,
            reason=f"{self.service!r} restart action applied, state={current_state!r}",
            observed={"state": current_state},
            timestamp_s=time.time(),
        )

    def verify_recovered(self, target: "Target") -> VerificationResult:
        """Verify the service has been restored to its pre-fault state."""
        code, stdout, _ = target.run(
            f"systemctl is-active {self.service} 2>/dev/null || true"
        )
        current_state = stdout.strip()
        if self._was_active:
            recovered = current_state == "active"
            return VerificationResult(
                verified=recovered,
                reason=f"{self.service!r} is {'active (recovered)' if recovered else f'not active: {current_state!r}'}",
                observed={"state": current_state},
                timestamp_s=time.time(),
            )
        # Service was not active before — anything is acceptable
        return VerificationResult(
            verified=True,
            reason=f"{self.service!r} was not active before fault; current={current_state!r}",
            observed={"state": current_state},
            timestamp_s=time.time(),
        )

    def _parameters(self) -> dict:
        return {
            "service":    self.service,
            "action":     self.action,
            "was_active": self._was_active,
        }


class ContainerKill(Fault):
    """Kill, stop, pause, or remove a Docker container.

    On ``stop()`` the container is restarted (except for ``rm``).
    For ``pause``, ``stop()`` unpauses the container instead.

    Parameters
    ----------
    container : str
        Container name or ID.
    action : str
        One of ``"kill"``, ``"stop"``, ``"pause"``, ``"rm"``.
        Default ``"kill"``.

    Examples
    --------
    >>> fault = ContainerKill("my-api")
    >>> fault = ContainerKill("redis-cache", action="pause")
    >>> fault = ContainerKill("old-worker", action="rm")
    """

    dependencies: list[str]    = ["docker-cli"]
    danger_level: int          = 2
    default_metrics: list[str] = ["downtime_s", "recovery_time_s", "error_rate", "exit_code", "restart_count"]

    VALID_ACTIONS = ("kill", "stop", "pause", "rm")

    def __init__(self, container: str, action: str = "kill") -> None:
        if not container or not container.strip():
            raise ValueError("ContainerKill requires a non-empty 'container' name.")
        if action not in self.VALID_ACTIONS:
            raise ValueError(
                f"ContainerKill 'action' must be one of {self.VALID_ACTIONS}, got {action!r}."
            )
        self.container = container.strip()
        self.action = action
        self._was_running = False

    def start(self, target: "Target") -> None:
        _, out, _ = target.run(
            f"docker inspect --format='{{{{.State.Running}}}}' {self.container} "
            f"2>/dev/null || echo false"
        )
        self._was_running = out.strip().lower() == "true"
        target.run(f"docker {self.action} {self.container}")

    def stop(self, target: "Target") -> None:
        if self.action == "pause":
            target.run(f"docker unpause {self.container} 2>/dev/null || true")
        elif self.action != "rm" and self._was_running:
            target.run(f"docker start {self.container} 2>/dev/null || true")

    def revert(self, target: "Target") -> None:
        self.stop(target)

    def verify_active(self, target: "Target") -> VerificationResult:
        """Verify the container is in the fault state."""
        _, out, _ = target.run(
            f"docker inspect --format='{{{{.State.Status}}}}' {self.container} 2>/dev/null || echo missing"
        )
        status = out.strip().strip("'\"")
        if self.action == "kill":
            expected_down = status in ("exited", "dead", "missing")
            return VerificationResult(
                verified=expected_down,
                reason=f"Container {self.container!r} status={status!r}",
                observed={"status": status},
                timestamp_s=time.time(),
            )
        if self.action == "stop":
            expected_down = status in ("exited", "missing")
            return VerificationResult(
                verified=expected_down,
                reason=f"Container {self.container!r} status={status!r}",
                observed={"status": status},
                timestamp_s=time.time(),
            )
        if self.action == "pause":
            paused = status == "paused"
            return VerificationResult(
                verified=paused,
                reason=f"Container {self.container!r} status={status!r}",
                observed={"status": status},
                timestamp_s=time.time(),
            )
        if self.action == "rm":
            gone = status == "missing"
            return VerificationResult(
                verified=gone,
                reason=f"Container {self.container!r} {'removed' if gone else f'still present: {status!r}'}",
                observed={"status": status},
                timestamp_s=time.time(),
            )
        return VerificationResult(verified=True, reason="Unknown action", timestamp_s=time.time())

    def verify_recovered(self, target: "Target") -> VerificationResult:
        """Verify the container has been restored."""
        if self.action == "rm":
            return VerificationResult(
                verified=True,
                reason="ContainerKill rm is irreversible — no automatic recovery",
                timestamp_s=time.time(),
            )
        _, out, _ = target.run(
            f"docker inspect --format='{{{{.State.Status}}}}' {self.container} 2>/dev/null || echo missing"
        )
        status = out.strip().strip("'\"")
        if self._was_running:
            recovered = status == "running"
            return VerificationResult(
                verified=recovered,
                reason=f"Container {self.container!r} is {'running (recovered)' if recovered else f'{status!r}'}",
                observed={"status": status},
                timestamp_s=time.time(),
            )
        return VerificationResult(
            verified=True,
            reason=f"Container was not running before; current={status!r}",
            observed={"status": status},
            timestamp_s=time.time(),
        )

    def _parameters(self) -> dict:
        return {
            "container":   self.container,
            "action":      self.action,
            "was_running": self._was_running,
        }
