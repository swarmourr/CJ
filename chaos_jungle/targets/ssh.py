"""SSH target — runs commands on a remote machine via Paramiko."""

from __future__ import annotations
import os

import paramiko

from chaos_jungle.targets.base import Target


class SSHTarget(Target):
    """Run fault commands on a remote machine over SSH.

    Parameters
    ----------
    host : str
        Hostname or IP address of the target machine.
    user : str
        SSH username.
    key : str, optional
        Path to private key file. Defaults to ``~/.ssh/id_rsa``.
    port : int, optional
        SSH port. Default ``22``.
    use_sudo : bool, optional
        Prepend ``sudo`` to privileged commands. Default ``True``.
    password : str, optional
        Password for key passphrase or password auth (not recommended).

    Examples
    --------
    >>> target = SSHTarget("worker1", user="ubuntu", key="~/.ssh/id_rsa")
    >>> with target:
    ...     code, out, err = target.run("hostname")
    """

    def __init__(
        self,
        host: str,
        user: str,
        key: str = "~/.ssh/id_rsa",
        port: int = 22,
        use_sudo: bool = True,
        password: str | None = None,
    ) -> None:
        self.host = host
        self.user = user
        self.key = os.path.expanduser(key)
        self.port = port
        self.use_sudo = use_sudo
        self.password = password
        self._client: paramiko.SSHClient | None = None

    def connect(self) -> None:
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._client.connect(
            hostname=self.host,
            port=self.port,
            username=self.user,
            key_filename=self.key,
            password=self.password,
        )

    def disconnect(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def run(self, cmd: str) -> tuple[int, str, str]:
        if self._client is None:
            self.connect()
        _, stdout, stderr = self._client.exec_command(cmd)
        exit_code = stdout.channel.recv_exit_status()
        return exit_code, stdout.read().decode(), stderr.read().decode()

    def sudo(self, cmd: str) -> tuple[int, str, str]:
        prefix = "sudo " if self.use_sudo else ""
        return self.run(f"{prefix}{cmd}")

    def put(self, local_path: str, remote_path: str) -> None:
        if self._client is None:
            self.connect()
        with self._client.open_sftp() as sftp:
            sftp.put(local_path, remote_path)

    def get(self, remote_path: str, local_path: str) -> None:
        if self._client is None:
            self.connect()
        with self._client.open_sftp() as sftp:
            sftp.get(remote_path, local_path)
