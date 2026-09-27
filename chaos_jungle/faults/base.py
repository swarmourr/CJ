"""Base class for all chaos faults."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chaos_jungle.targets.base import Target


class PreflightError(RuntimeError):
    """Raised when required dependencies are missing on the target machine.

    Contains a human-readable message listing every missing binary and
    the exact install command to fix it.
    """


class CommandError(RuntimeError):
    """Raised when a shell command exits with a non-zero exit code.

    Attributes
    ----------
    cmd : str
        The command that was run.
    exit_code : int
        The exit code returned by the command.
    stdout : str
        Standard output captured from the command.
    stderr : str
        Standard error captured from the command.
    """

    def __init__(self, cmd: str, exit_code: int, stdout: str, stderr: str) -> None:
        self.cmd = cmd
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"Command failed (exit {exit_code}): {cmd!r}\n"
            f"  stdout: {stdout.strip()!r}\n"
            f"  stderr: {stderr.strip()!r}"
        )


@dataclass
class VerificationResult:
    """Result of a fault activation or recovery verification check.

    Attributes
    ----------
    verified : bool
        ``True`` if the expected system state was confirmed.
    reason : str
        Human-readable explanation of the verification outcome.
    observed : dict
        Optional snapshot of the observed system state (e.g. tc qdisc output,
        process list, proxy health response).
    timestamp_s : float
        Unix timestamp when the verification was performed.
    """

    verified: bool
    reason: str
    observed: dict = field(default_factory=dict)
    timestamp_s: float = field(default_factory=time.time)


class Fault(ABC):
    """Abstract base class for all fault types.

    A fault knows how to start itself, stop itself, and revert any
    side effects it has caused. It also declares what system packages
    it needs on the target machine.

    Notes
    -----
    All fault methods receive a :class:`~chaos_jungle.targets.base.Target`
    instance so they can execute commands on the correct machine.

    Class attributes
    ----------------
    danger_level : int
        Safety classification of this fault type:

        * ``0`` — **safe** — reversible, no persistent side effects
          (e.g. network delay, LLM latency injection).
        * ``1`` — **moderate** — resource consumption, may affect other
          workloads on the same machine (e.g. CPU stress, memory stress).
        * ``2`` — **destructive** — may cause data loss, service outages,
          or require manual cleanup (e.g. process kill, disk fill, storage
          corruption).

        :class:`~chaos_jungle.core.guardrails.SafetyPolicy` uses this to gate
        which faults are allowed to run.
    """

    #: System packages required on the target machine (canonical names).
    dependencies: list[str] = []

    #: Python (pip) packages required on the target machine.
    pip_dependencies: list[str] = []

    #: Safety classification (0=safe, 1=moderate, 2=destructive).
    danger_level: int = 0

    #: Metrics automatically collected for this fault by CollectStrategy.
    #: Subclasses override to declare fault-specific metrics.
    #: Universal metrics (duration_s, error_rate, success) are always added.
    default_metrics: list[str] = []

    @abstractmethod
    def start(self, target: "Target") -> None:
        """Inject the fault on the target machine."""

    @abstractmethod
    def stop(self, target: "Target") -> None:
        """Remove the fault from the target machine."""

    @abstractmethod
    def revert(self, target: "Target") -> None:
        """Undo any persistent side effects left by this fault.

        For stateless faults (e.g. network rules) this is a no-op.
        For stateful faults (e.g. storage corruption) this restores
        original data.
        """

    def verify_active(self, target: "Target") -> VerificationResult:
        """Verify that the fault is currently active on the target.

        Override in subclasses to implement meaningful verification:

        * Network faults: inspect ``tc qdisc`` output.
        * CPU/memory faults: verify the stress process is running.
        * Proxy-based faults: query the proxy health endpoint.
        * Intercept faults: check the in-process patch is applied.

        The default implementation returns ``verified=True`` with a note
        that verification is not implemented for this fault type.

        Parameters
        ----------
        target :
            The machine to verify against.

        Returns
        -------
        VerificationResult
        """
        return VerificationResult(
            verified=True,
            reason=f"{self.__class__.__name__}: no verification implemented",
        )

    def verify_recovered(self, target: "Target") -> VerificationResult:
        """Verify that the fault has been fully reverted on the target.

        Override in subclasses to confirm the system has returned to a
        clean state after :meth:`stop` and :meth:`revert`.

        The default implementation returns ``verified=True`` with a note
        that verification is not implemented for this fault type.

        Parameters
        ----------
        target :
            The machine to verify against.

        Returns
        -------
        VerificationResult
        """
        return VerificationResult(
            verified=True,
            reason=f"{self.__class__.__name__}: no verification implemented",
        )

    def dry_run(self, target: "Target") -> None:
        """Print what this fault *would* do without actually doing it.

        Called by :class:`~chaos_jungle.core.runner.ChaosRunner` when
        ``dry_run=True`` is set on the runner or when a
        :class:`~chaos_jungle.core.guardrails.SafetyPolicy` with ``dry_run=True``
        is enforced.

        The default implementation prints the fault name and parameters.
        Subclasses may override to produce more detailed output.

        Parameters
        ----------
        target :
            The machine that would be targeted.
        """
        print(
            f"[chaos-jungle] DRY-RUN {self.__class__.__name__}({self._parameters()}) "
            f"on {target.__class__.__name__} — not executed"
        )

    def preflight(
        self,
        target: "Target",
        auto_install: "bool | str" = False,
    ) -> None:
        """Check dependencies on the target and optionally install missing ones.

        Parameters
        ----------
        target :
            The machine to check.
        auto_install : bool or str
            Pass ``False`` (default) to raise :exc:`PreflightError` when
            packages are missing.  Pass ``True`` to auto-detect the package
            manager (apt / dnf / yum / apk / brew) and install automatically.
            Pass ``"prompt"`` to show a summary and ask for confirmation before
            proceeding.

        Raises
        ------
        PreflightError
            When ``auto_install=False`` and dependencies are missing, when no
            supported package manager is found, or when the user declines the
            prompt.

        Examples
        --------
        Silent auto-install::

            fault.preflight(target, auto_install=True)

        Interactive prompt (the user is shown what will be installed)::

            fault.preflight(target, auto_install="prompt")

        """
        from chaos_jungle.core.preflight import run_preflight

        run_preflight(
            target=target,
            fault_name=self.__class__.__name__,
            dependencies=self.dependencies,
            pip_dependencies=self.pip_dependencies,
            auto_install=auto_install,
        )

    def to_dict(self) -> dict:
        """Serialize fault parameters to a plain dict (stored as JSON in DB).

        Returns
        -------
        dict
            Fault kind and parameters.
        """
        return {
            "kind": self.__class__.__name__,
            "parameters": self._parameters(),
        }

    def _parameters(self) -> dict:
        """Return fault-specific parameters. Override in subclasses."""
        return {}

    @staticmethod
    def run_checked(target: "Target", cmd: str, privileged: bool = False) -> tuple[int, str, str]:
        """Run *cmd* on *target* and raise :exc:`CommandError` on non-zero exit.

        Parameters
        ----------
        target :
            Where to run the command.
        cmd : str
            Shell command to execute.
        privileged : bool
            When ``True``, run with ``target.sudo()`` instead of ``target.run()``.

        Returns
        -------
        tuple[int, str, str]
            ``(exit_code, stdout, stderr)`` on success.

        Raises
        ------
        CommandError
            If the command exits with a non-zero exit code.
        """
        fn = target.sudo if privileged else target.run
        code, stdout, stderr = fn(cmd)
        if code != 0:
            raise CommandError(cmd, code, stdout, stderr)
        return code, stdout, stderr
