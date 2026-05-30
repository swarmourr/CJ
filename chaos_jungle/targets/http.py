"""HTTP target — controls a remote chaos-daemon over HTTP/HTTPS."""

from __future__ import annotations
import os

import requests

from chaos_jungle.targets.base import Target


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
