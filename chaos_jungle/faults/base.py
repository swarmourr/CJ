"""Base class for all chaos faults."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chaos_jungle.targets.base import Target


class PreflightError(RuntimeError):
    """Raised when required dependencies are missing on the target machine.

    Contains a human-readable message listing every missing binary and
    the apt package needed to install it.
    """


class Fault(ABC):
    """Abstract base class for all fault types.

    A fault knows how to start itself, stop itself, and revert any
    side effects it has caused. It also declares what system packages
    it needs on the target machine.

    Parameters
    ----------
    None — subclasses define their own parameters.

    Notes
    -----
    All fault methods receive a :class:`~chaos_jungle.targets.base.Target`
    instance so they can execute commands on the correct machine.
    """

    #: System packages required on the target machine.
    dependencies: list[str] = []

    @abstractmethod
    def start(self, target: "Target") -> None:
        """Inject the fault on the target machine.

        Parameters
        ----------
        target :
            The machine to inject the fault on.
        """

    @abstractmethod
    def stop(self, target: "Target") -> None:
        """Remove the fault from the target machine.

        Parameters
        ----------
        target :
            The machine to remove the fault from.
        """

    @abstractmethod
    def revert(self, target: "Target") -> None:
        """Undo any persistent side effects left by this fault.

        For stateless faults (e.g. network rules) this is a no-op.
        For stateful faults (e.g. storage corruption) this restores
        original data.

        Parameters
        ----------
        target :
            The machine to revert on.
        """

    # Map apt package names → binary to check with `which`
    _PKG_TO_BIN: dict[str, str] = {
        "iproute2":      "tc",
        "e2fsprogs":     "filefrag",
        "inotify-tools": "inotifywait",
        "coreutils":     "dd",
        "python3":       "python3",
    }

    def preflight(self, target: "Target", auto_install: bool = False) -> None:
        """Check dependencies on the target and optionally install missing ones.

        Parameters
        ----------
        target :
            The machine to check.
        auto_install : bool
            If ``True``, install missing packages via ``apt-get``.
            If ``False`` (default), raise :exc:`PreflightError` listing
            all missing packages.

        Raises
        ------
        PreflightError
            When ``auto_install=False`` and one or more dependencies are missing.
        """
        missing = []
        for pkg in self.dependencies:
            binary = self._PKG_TO_BIN.get(pkg, pkg)
            code, _, _ = target.run(f"which {binary} 2>/dev/null")
            if code != 0:
                missing.append((pkg, binary))

        if not missing:
            return

        if auto_install:
            for pkg, binary in missing:
                print(f"[preflight] Installing missing package: {pkg}")
                code, _, stderr = target.sudo(f"apt-get install -y {pkg}")
                if code != 0:
                    raise PreflightError(
                        f"Failed to install '{pkg}' on target: {stderr.strip()}"
                    )
                print(f"[preflight] Installed: {pkg}")
        else:
            lines = "\n".join(
                f"  - {binary!r}  (apt package: {pkg})" for pkg, binary in missing
            )
            raise PreflightError(
                f"{self.__class__.__name__} preflight failed — missing on target:\n{lines}\n"
                f"Fix: run with auto_install=True  or  apt-get install "
                + " ".join(pkg for pkg, _ in missing)
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
