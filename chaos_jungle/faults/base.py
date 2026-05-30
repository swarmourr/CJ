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
    """

    #: System packages required on the target machine (canonical names).
    dependencies: list[str] = []

    #: Python (pip) packages required on the target machine.
    pip_dependencies: list[str] = []

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
        auto_install : bool or ``"prompt"``
            * ``False`` *(default)* — raise :exc:`PreflightError` listing all
              missing packages with the exact fix command.
            * ``True`` — detect the package manager (apt / dnf / yum / apk /
              brew) and install missing packages automatically.
            * ``"prompt"`` — print a summary of what will be installed and ask
              the user for confirmation before proceeding.

        Raises
        ------
        PreflightError
            When ``auto_install=False`` and dependencies are missing, when no
            supported package manager is found, or when the user declines the
            prompt.

        Examples
        --------
        Silent auto-install:

        >>> fault.preflight(target, auto_install=True)

        Interactive prompt:

        >>> fault.preflight(target, auto_install="prompt")
        [preflight] NetworkDelay — missing dependencies:
          System packages:
            - 'iproute2'  (binary: tc)
        Install now? [y/N]
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
