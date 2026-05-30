"""Base class for all chaos targets."""

from abc import ABC, abstractmethod


class Target(ABC):
    """Abstract base for a machine that faults can be applied to.

    A target knows how to run shell commands, transfer files, and
    execute commands with elevated privileges on a specific machine.

    Notes
    -----
    All ``run()`` calls return a 3-tuple ``(exit_code, stdout, stderr)``.
    """

    @abstractmethod
    def connect(self) -> None:
        """Open a connection to the target machine."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the connection to the target machine."""

    @abstractmethod
    def run(self, cmd: str) -> tuple[int, str, str]:
        """Run a shell command on the target.

        Parameters
        ----------
        cmd : str
            Shell command to execute.

        Returns
        -------
        tuple[int, str, str]
            ``(exit_code, stdout, stderr)``
        """

    @abstractmethod
    def sudo(self, cmd: str) -> tuple[int, str, str]:
        """Run a shell command with elevated privileges on the target.

        Parameters
        ----------
        cmd : str
            Shell command to execute as root.

        Returns
        -------
        tuple[int, str, str]
            ``(exit_code, stdout, stderr)``
        """

    @abstractmethod
    def put(self, local_path: str, remote_path: str) -> None:
        """Upload a file to the target machine.

        Parameters
        ----------
        local_path : str
            Path on the local machine.
        remote_path : str
            Destination path on the target machine.
        """

    @abstractmethod
    def get(self, remote_path: str, local_path: str) -> None:
        """Download a file from the target machine.

        Parameters
        ----------
        remote_path : str
            Path on the target machine.
        local_path : str
            Destination path on the local machine.
        """

    def __enter__(self) -> "Target":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()
