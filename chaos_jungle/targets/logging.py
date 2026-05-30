"""LoggingTarget — wraps any Target and records every command to the session DB."""

from __future__ import annotations
from typing import TYPE_CHECKING

from chaos_jungle.targets.base import Target

if TYPE_CHECKING:
    from chaos_jungle.db.session_db import SessionDB


class LoggingTarget(Target):
    """Transparent proxy around any Target that saves every command and its
    output to the chaos-jungle session database.

    Used internally by :class:`~chaos_jungle.runner.ChaosRunner` so that
    every ``tc``, ``dd``, ``python3 cj_storage.py …`` command is visible
    in the dashboard's session event log.

    Parameters
    ----------
    target :
        The real target to forward commands to.
    db :
        Open :class:`~chaos_jungle.db.session_db.SessionDB` instance.
    session_id :
        ID of the current session (from ``db.open_session()``).
    fault_id :
        Current fault record ID.  Updated by the runner before each fault.
    """

    def __init__(
        self,
        target: Target,
        db: "SessionDB",
        session_id: int,
        fault_id: int | None = None,
    ) -> None:
        self._target   = target
        self._db       = db
        self._session_id = session_id
        self.fault_id  = fault_id   # set by runner before each fault block

    # ── Target delegation ─────────────────────────────────────────────────

    def connect(self) -> None:
        self._target.connect()

    def disconnect(self) -> None:
        self._target.disconnect()

    def put(self, local_path: str, remote_path: str) -> None:
        self._db.add_event(
            self._session_id,
            f"[upload] {local_path} → {remote_path}",
            fault_id=self.fault_id,
        )
        self._target.put(local_path, remote_path)

    def get(self, remote_path: str, local_path: str) -> None:
        self._db.add_event(
            self._session_id,
            f"[download] {remote_path} → {local_path}",
            fault_id=self.fault_id,
        )
        self._target.get(remote_path, local_path)

    # ── Logged run / sudo ─────────────────────────────────────────────────

    def run(self, cmd: str) -> tuple[int, str, str]:
        code, out, err = self._target.run(cmd)
        self._log_cmd("$", cmd, code, out, err)
        return code, out, err

    def sudo(self, cmd: str) -> tuple[int, str, str]:
        code, out, err = self._target.sudo(cmd)
        self._log_cmd("#", cmd, code, out, err)
        return code, out, err

    # ── Internal ──────────────────────────────────────────────────────────

    def _log_cmd(
        self,
        prefix: str,
        cmd: str,
        code: int,
        out: str,
        err: str,
    ) -> None:
        # trim long output to avoid bloating the DB
        def _trim(s: str, n: int = 400) -> str:
            s = s.strip()
            return s if len(s) <= n else s[:n] + f"… (+{len(s)-n} chars)"

        parts = [f"{prefix} {cmd}", f"exit={code}"]
        if out.strip():
            parts.append(f"stdout: {_trim(out)}")
        if err.strip():
            parts.append(f"stderr: {_trim(err)}")

        status = "OK" if code == 0 else "ERROR"
        message = f"[cmd:{status}] " + " | ".join(parts)

        self._db.add_event(
            self._session_id,
            message,
            fault_id=self.fault_id,
        )
