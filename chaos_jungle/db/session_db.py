"""Unified SQLite session database for chaos-jungle."""

from __future__ import annotations
import json
import os
import sqlite3
from datetime import datetime, timezone


_DEFAULT_DB = os.path.expanduser("~/.chaos-jungle/chaos_jungle.db")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionDB:
    """Persist chaos session metadata to a local SQLite database.

    Records three types of data:

    * **sessions** — one row per chaos run (name, start/stop time, status)
    * **faults** — one row per fault within a session
    * **events** — timestamped log of everything that happened

    The existing ``cj.db`` (storage bit-flip records) is never touched.

    Parameters
    ----------
    path : str, optional
        Path to the SQLite file. Defaults to
        ``~/.chaos-jungle/chaos_jungle.db``.

    Examples
    --------
    >>> db = SessionDB()
    >>> sid = db.open_session("my-scenario")
    >>> fid = db.record_fault(sid, "NetworkDelay", {"delay": "100ms"})
    >>> db.add_event(sid, fid, "tc qdisc added on eth0")
    >>> db.close_session(sid)
    """

    def __init__(self, path: str = _DEFAULT_DB) -> None:
        self.path = path
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    # ── Schema ────────────────────────────────────────────────────

    def _create_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL,
                started_at  TEXT    NOT NULL,
                stopped_at  TEXT,
                status      TEXT    NOT NULL DEFAULT 'running'
            );

            CREATE TABLE IF NOT EXISTS faults (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  INTEGER NOT NULL REFERENCES sessions(id),
                kind        TEXT    NOT NULL,
                parameters  TEXT    NOT NULL DEFAULT '{}',
                started_at  TEXT    NOT NULL,
                stopped_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  INTEGER NOT NULL REFERENCES sessions(id),
                fault_id    INTEGER REFERENCES faults(id),
                timestamp   TEXT    NOT NULL,
                message     TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS results (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   INTEGER NOT NULL REFERENCES sessions(id),
                recorded_at  TEXT    NOT NULL,
                metrics      TEXT    NOT NULL DEFAULT '{}'
            );
        """)
        self._conn.commit()

    # ── Sessions ──────────────────────────────────────────────────

    def open_session(self, name: str) -> int:
        """Create a new session and return its id.

        Parameters
        ----------
        name : str
            Human-readable scenario name.

        Returns
        -------
        int
            Session id.
        """
        cur = self._conn.execute(
            "INSERT INTO sessions (name, started_at, status) VALUES (?, ?, 'running')",
            (name, _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def close_session(self, session_id: int, status: str = "stopped") -> None:
        """Mark a session as finished.

        Parameters
        ----------
        session_id : int
        status : str
            ``"stopped"`` or ``"reverted"``.
        """
        self._conn.execute(
            "UPDATE sessions SET stopped_at = ?, status = ? WHERE id = ?",
            (_now(), status, session_id),
        )
        self._conn.commit()

    def active_session(self) -> sqlite3.Row | None:
        """Return the most recent running session, or ``None``."""
        return self._conn.execute(
            "SELECT * FROM sessions WHERE status = 'running' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def get_session(self, session_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()

    def list_sessions(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM sessions ORDER BY id DESC"
        ).fetchall()

    # ── Faults ────────────────────────────────────────────────────

    def record_fault(self, session_id: int, kind: str, parameters: dict) -> int:
        """Record a fault being started.

        Parameters
        ----------
        session_id : int
        kind : str
            Fault class name, e.g. ``"NetworkDelay"``.
        parameters : dict
            Fault parameters serialized to dict.

        Returns
        -------
        int
            Fault record id.
        """
        cur = self._conn.execute(
            "INSERT INTO faults (session_id, kind, parameters, started_at) VALUES (?, ?, ?, ?)",
            (session_id, kind, json.dumps(parameters), _now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def close_fault(self, fault_id: int) -> None:
        """Mark a fault as stopped."""
        self._conn.execute(
            "UPDATE faults SET stopped_at = ? WHERE id = ?",
            (_now(), fault_id),
        )
        self._conn.commit()

    # ── Events ────────────────────────────────────────────────────

    def add_event(
        self,
        session_id: int,
        message: str,
        fault_id: int | None = None,
    ) -> None:
        """Append a timestamped event to the session log.

        Parameters
        ----------
        session_id : int
        message : str
            Human-readable description of the event.
        fault_id : int, optional
            Associate the event with a specific fault.
        """
        self._conn.execute(
            "INSERT INTO events (session_id, fault_id, timestamp, message) VALUES (?, ?, ?, ?)",
            (session_id, fault_id, _now(), message),
        )
        self._conn.commit()

    def get_events(self, session_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM events WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()

    # ── Export ────────────────────────────────────────────────────

    def export_session(self, session_id: int) -> dict:
        """Export a full session as a plain dict (JSON-serializable).

        Parameters
        ----------
        session_id : int

        Returns
        -------
        dict
            Session metadata, faults list, and events list.
        """
        session = self.get_session(session_id)
        if session is None:
            raise ValueError(f"Session {session_id} not found")

        faults = self._conn.execute(
            "SELECT * FROM faults WHERE session_id = ?", (session_id,)
        ).fetchall()

        events = self.get_events(session_id)

        # parse the parameters JSON string back into a dict for convenience
        fault_list = []
        for f in faults:
            row = dict(f)
            try:
                row["parameters"] = json.loads(row["parameters"])
            except (TypeError, ValueError):
                pass
            fault_list.append(row)

        return {
            "session": dict(session),
            "faults": fault_list,
            "events": [dict(e) for e in events],
        }

    # ── Results ───────────────────────────────────────────────────

    def record_result(self, session_id: int, metrics: dict) -> int:
        """Store workflow outcome metrics for a session.

        Call this after your workload finishes to attach observed results
        (throughput, retries, integrity failures, etc.) to the chaos session
        so they appear in the dashboard.

        Parameters
        ----------
        session_id : int
        metrics : dict
            Any JSON-serializable dict, e.g.::

                {
                    "files_transferred": 120,
                    "files_corrupted":    3,
                    "files_missing":      0,
                    "retries":            7,
                    "throughput_mbps":    42.1,
                    "integrity_failures": 3,
                }

        Returns
        -------
        int
            Result record id.
        """
        cur = self._conn.execute(
            "INSERT INTO results (session_id, recorded_at, metrics) VALUES (?, ?, ?)",
            (session_id, _now(), json.dumps(metrics)),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_results(self, session_id: int) -> list[dict]:
        """Return all result records for a session."""
        rows = self._conn.execute(
            "SELECT * FROM results WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        out = []
        for r in rows:
            row = dict(r)
            try:
                row["metrics"] = json.loads(row["metrics"])
            except (TypeError, ValueError):
                pass
            out.append(row)
        return out

    def close(self) -> None:
        self._conn.close()
