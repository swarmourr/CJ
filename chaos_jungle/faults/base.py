"""Base class for all chaos faults."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chaos_jungle.targets.base import Target


class PreflightError(RuntimeError):
    """Raised when required dependencies are missing on the target machine.

    Contains a human-readable message listing every missing binary and
    the exact install command to fix it.
    """


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

        :class:`~chaos_jungle.guardrails.SafetyPolicy` uses this to gate
        which faults are allowed to run.
    """

    #: System packages required on the target machine (canonical names).
    dependencies: list[str] = []

    #: Python (pip) packages required on the target machine.
    pip_dependencies: list[str] = []

    #: Safety classification (0=safe, 1=moderate, 2=destructive).
    danger_level: int = 0

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

    def dry_run(self, target: "Target") -> None:
        """Print what this fault *would* do without actually doing it.

        Called by :class:`~chaos_jungle.runner.ChaosRunner` when
        ``dry_run=True`` is set on the runner or when a
        :class:`~chaos_jungle.guardrails.SafetyPolicy` with ``dry_run=True``
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
        from chaos_jungle.preflight import run_preflight

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
