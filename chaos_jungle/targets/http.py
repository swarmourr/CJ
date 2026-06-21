"""HTTP target — controls a remote chaos-daemon over HTTP/HTTPS."""

from __future__ import annotations
import os
from typing import TYPE_CHECKING

import requests

from chaos_jungle.targets.base import Target

if TYPE_CHECKING:
    from chaos_jungle.scenario import Scenario


class HTTPTarget(Target):
    """Control a remote machine through the chaos-jungle daemon API.

    The daemon must be running on the target machine (``cj-daemon``).
    This target sends HTTP requests to the daemon instead of running
    commands directly over SSH.

    Parameters
    ----------
    url : str
        Base URL of the chaos daemon, e.g. ``"http://nodeB:7777"``.
    token : str, optional
        Bearer token for daemon authentication.
    tls_verify : bool, optional
        Verify TLS certificate. Default ``True``. Set to ``False`` for
        self-signed certs in test environments.

    Examples
    --------
    >>> target = HTTPTarget("http://worker1:7777", token="secret")
    >>> target.connect()   # verifies daemon is reachable
    """

    def __init__(
        self,
        url: str,
        token: str = "",
        tls_verify: bool = True,
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.tls_verify = tls_verify
        self._session: requests.Session | None = None

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def connect(self) -> None:
        self._session = requests.Session()
        self._session.headers.update(self._headers())
        self._session.verify = self.tls_verify
        resp = self._session.get(f"{self.url}/health", timeout=5)
        resp.raise_for_status()

    def disconnect(self) -> None:
        if self._session:
            self._session.close()
            self._session = None

    def run(self, cmd: str) -> tuple[int, str, str]:
        """Send a raw shell command to the daemon.

        Parameters
        ----------
        cmd : str
            Shell command to run on the remote machine.

        Returns
        -------
        tuple[int, str, str]
            ``(exit_code, stdout, stderr)``
        """
        if self._session is None:
            self.connect()
        resp = self._session.post(
            f"{self.url}/exec",
            json={"cmd": cmd},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["exit_code"], data.get("stdout", ""), data.get("stderr", "")

    def sudo(self, cmd: str) -> tuple[int, str, str]:
        # daemon runs as root, no sudo prefix needed
        return self.run(cmd)

    def put(self, local_path: str, remote_path: str) -> None:
        if self._session is None:
            self.connect()
        with open(local_path, "rb") as f:
            self._session.post(
                f"{self.url}/files/upload",
                files={"file": f},
                data={"dest": remote_path},
                timeout=60,
            ).raise_for_status()

    def get(self, remote_path: str, local_path: str) -> None:
        if self._session is None:
            self.connect()
        resp = self._session.get(
            f"{self.url}/files/download",
            params={"path": remote_path},
            timeout=60,
        )
        resp.raise_for_status()
        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(resp.content)

    # ── Scenario Registry ────────────────────────────────────────

    def _push_scenario(self, scenario: "Scenario") -> str:
        """Register a scenario on the remote daemon.

        POSTs the serialized scenario to ``POST /scenarios`` on the daemon
        and registers it locally as type ``"http"``.

        Parameters
        ----------
        scenario : Scenario

        Returns
        -------
        str
            The scenario UUID.
        """
        from chaos_jungle.registry import ScenarioRegistry
        from urllib.parse import urlparse

        if self._session is None:
            self.connect()

        # Register locally as type=http
        host = urlparse(self.url).hostname or self.url
        local_registry = ScenarioRegistry()
        local_registry.register(scenario, type="http", target_ip=host)

        # Push to daemon
        resp = self._session.post(
            f"{self.url}/scenarios",
            json=scenario.to_dict(),
            timeout=10,
        )
        resp.raise_for_status()
        return scenario.id

    def _run_scenario(self, scenario_id: str) -> None:
        """Tell the daemon to run a previously pushed scenario (non-blocking).

        The daemon starts the run in the background and returns immediately
        with HTTP 202.  Use :meth:`scenario_status` to poll for completion.

        Parameters
        ----------
        scenario_id : str
            UUID of a scenario registered via :meth:`push_scenario`.
        """
        if self._session is None:
            self.connect()
        resp = self._session.post(
            f"{self.url}/scenarios/{scenario_id}/run",
            timeout=10,
        )
        resp.raise_for_status()

    def _scenario_status(self, scenario_id: str) -> dict | None:
        """Query the daemon for a scenario's current status.

        Parameters
        ----------
        scenario_id : str

        Returns
        -------
        dict or None
            Registry entry with ``status``, ``session_id``, etc.
        """
        if self._session is None:
            self.connect()
        resp = self._session.get(
            f"{self.url}/scenarios/{scenario_id}/status",
            timeout=10,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
