"""SSH target — runs commands on a remote machine via Paramiko."""

from __future__ import annotations
import os

import paramiko

from chaos_jungle.targets.base import Target


class SSHTarget(Target):
    """Run fault commands on a remote machine over SSH.

    Authentication is attempted in this order (same as the OpenSSH client):

    1. **Explicit key file** — if ``key`` points to an existing file.
    2. **SSH agent** — if ``allow_agent=True`` (default) and an agent is
       running (e.g. ``ssh-add`` was used).
    3. **Default key search** — if ``look_for_keys=True`` (default),
       Paramiko tries ``~/.ssh/id_rsa``, ``~/.ssh/id_ecdsa``,
       ``~/.ssh/id_ed25519``, etc.
    4. **Password** — if ``password`` is provided (last resort).

    Parameters
    ----------
    host : str
        Hostname or IP address of the target machine.
    user : str
        SSH username.
    key : str, optional
        Path to a private key file, e.g. ``"~/.ssh/id_ed25519"``.
        Skipped if the file does not exist. Default ``None`` (auto-detect).
    port : int, optional
        SSH port. Default ``22``.
    use_sudo : bool, optional
        Prepend ``sudo`` to privileged commands. Default ``True``.
    password : str, optional
        Password for keyboard-interactive or password auth, or the
        passphrase for an encrypted private key.
    allow_agent : bool, optional
        Try the SSH agent (``ssh-agent`` / ``ssh-add``). Default ``True``.
    look_for_keys : bool, optional
        Let Paramiko search ``~/.ssh/`` for standard key filenames.
        Default ``True``.

    Examples
    --------
    Key-based (agent or default key)::

        target = SSHTarget("worker1", user="ubuntu")

    Explicit key file::

        target = SSHTarget("worker1", user="ubuntu", key="~/.ssh/id_ed25519")

    Encrypted key with passphrase::

        target = SSHTarget("worker1", user="ubuntu",
                           key="~/.ssh/id_rsa", password="my-passphrase")

    Password-only (no key)::

        target = SSHTarget("worker1", user="ubuntu",
                           password="hunter2",
                           allow_agent=False, look_for_keys=False)

    Custom port::

        target = SSHTarget("worker1", user="ubuntu", port=2222)
    """

    def __init__(
        self,
        host: str,
        user: str,
        key: str | None = None,
        port: int = 22,
        use_sudo: bool = True,
        password: str | None = None,
        allow_agent: bool = True,
        look_for_keys: bool = True,
    ) -> None:
        self.host = host
        self.user = user
        self.key = os.path.expanduser(key) if key else None
        self.port = port
        self.use_sudo = use_sudo
        self.password = password
        self.allow_agent = allow_agent
        self.look_for_keys = look_for_keys
        self._client: paramiko.SSHClient | None = None

    def connect(self) -> None:
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        # Only pass key_filename if the file actually exists
        key_filename = None
        if self.key and os.path.isfile(self.key):
            key_filename = self.key

        self._client.connect(
            hostname=self.host,
            port=self.port,
            username=self.user,
            key_filename=key_filename,
            password=self.password,
            allow_agent=self.allow_agent,
            look_for_keys=self.look_for_keys,
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
