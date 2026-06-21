"""SSH target — runs commands on a remote machine via Paramiko."""

from __future__ import annotations
import json
import os
from typing import TYPE_CHECKING

import paramiko

from chaos_jungle.targets.base import Target

if TYPE_CHECKING:
    from chaos_jungle.scenario import Scenario


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
        if not host or not host.strip():
            raise ValueError(
                "SSHTarget requires 'host' — hostname or IP of the target machine.\n"
                "  Example: SSHTarget('worker1', user='ubuntu')"
            )
        if not user or not user.strip():
            raise ValueError(
                "SSHTarget requires 'user' — SSH username on the target machine.\n"
                "  Example: SSHTarget('worker1', user='ubuntu')"
            )
        if not (1 <= port <= 65535):
            raise ValueError(
                f"SSHTarget 'port' must be between 1 and 65535, got {port}."
            )
        if key and not os.path.isfile(os.path.expanduser(key)):
            raise ValueError(
                f"SSHTarget 'key' file not found: {key!r}\n"
                f"  Fix A: check the path\n"
                f"  Fix B: omit 'key' to use ssh-agent or default ~/.ssh/ keys"
            )
        self.host = host.strip()
        self.user = user.strip()
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

    # ── Scenario Registry ────────────────────────────────────────

    def _push_scenario(self, scenario: "Scenario") -> str:
        """Register a scenario on the remote machine via SSH.

        Serializes the scenario to JSON and calls
        ``cj scenarios register`` on the remote so it appears in the
        remote registry with the same UUID.  Also registers locally
        as type ``"ssh"`` pointing to this host.

        Parameters
        ----------
        scenario : Scenario
            The scenario to push.

        Returns
        -------
        str
            The scenario UUID (same on both sides).
        """
        from chaos_jungle.registry import ScenarioRegistry

        # Register locally as type=ssh (we sent it to a remote)
        local_registry = ScenarioRegistry()
        local_registry.register(scenario, type="ssh", target_ip=self.host)

        # Register on remote (same UUID, type=local there, source=our host)
        payload = json.dumps(scenario.to_dict())
        escaped = payload.replace("'", "'\\''")
        rc, out, err = self.run(
            f"python3 -c \""
            f"import json, sys; "
            f"from chaos_jungle.scenario import Scenario; "
            f"from chaos_jungle.registry import ScenarioRegistry; "
            f"d = json.loads(sys.argv[1]); "
            f"s = Scenario.from_dict(d); "
            f"ScenarioRegistry().register(s, type='local', source_ip=sys.argv[2])"
            f"\" '{escaped}' '{self.host}'"
        )
        if rc != 0:
            raise RuntimeError(
                f"Failed to register scenario on remote {self.host}: {err.strip()}"
            )
        return scenario.id

    def _run_scenario(self, scenario_id: str) -> None:
        """Fire a scenario run on the remote machine (non-blocking).

        The SSH exec channel returns immediately after the remote process
        is started in the background.  Use :meth:`scenario_status` or
        :class:`~chaos_jungle.registry.ScenarioRegistry` to poll for
        completion.

        Parameters
        ----------
        scenario_id : str
            UUID of a scenario that has already been pushed via
            :meth:`push_scenario`.
        """
        rc, _, err = self.run(
            f"nohup python3 -m chaos_jungle.runner_cli --scenario-id {scenario_id} "
            f"> ~/.chaos-jungle/scenario_{scenario_id[:8]}.log 2>&1 &"
        )
        if rc != 0:
            raise RuntimeError(
                f"Failed to start scenario {scenario_id} on {self.host}: {err.strip()}"
            )

    def _scenario_status(self, scenario_id: str) -> dict | None:
        """Query the remote registry for a scenario's status.

        Issues a brief SSH connection and returns the registry entry as a
        dict.  Returns ``None`` if the scenario is not found on the remote.

        Parameters
        ----------
        scenario_id : str
            UUID to look up.

        Returns
        -------
        dict or None
        """
        rc, out, _ = self.run(
            f"python3 -c \""
            f"import json; "
            f"from chaos_jungle.registry import ScenarioRegistry; "
            f"e = ScenarioRegistry().get('{scenario_id}'); "
            f"print(json.dumps(e))"
            f"\""
        )
        if rc != 0 or not out.strip():
            return None
        try:
            return json.loads(out.strip())
        except (ValueError, TypeError):
            return None
