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

            CREATE TABLE IF NOT EXISTS commands (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  INTEGER NOT NULL REFERENCES sessions(id),
                fault_id    INTEGER REFERENCES faults(id),
                timestamp   TEXT    NOT NULL,
                privileged  INTEGER NOT NULL DEFAULT 0,
                cmd         TEXT    NOT NULL,
                exit_code   INTEGER NOT NULL,
                stdout      TEXT    NOT NULL DEFAULT '',
                stderr      TEXT    NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_commands_session
                ON commands(session_id);

            CREATE TABLE IF NOT EXISTS traces (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  INTEGER NOT NULL REFERENCES sessions(id),
                seq         INTEGER NOT NULL DEFAULT 0,
                timestamp   TEXT    NOT NULL,
                kind        TEXT    NOT NULL,
                data        TEXT    NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_traces_session
                ON traces(session_id);

            CREATE TABLE IF NOT EXISTS llm_calls (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id        INTEGER NOT NULL REFERENCES sessions(id),
                phase             TEXT    NOT NULL DEFAULT 'fault',
                call_index        INTEGER NOT NULL DEFAULT 0,
                timestamp         TEXT    NOT NULL,
                model             TEXT    NOT NULL DEFAULT '',
                prompt_tokens     INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd          REAL    NOT NULL DEFAULT 0.0,
                finish_reason     TEXT    NOT NULL DEFAULT '',
                prompt_text       TEXT    NOT NULL DEFAULT '',
                response_text     TEXT    NOT NULL DEFAULT '',
                latency_s         REAL    NOT NULL DEFAULT 0.0,
                http_status       INTEGER NOT NULL DEFAULT 200
            );

            CREATE INDEX IF NOT EXISTS idx_llm_calls_session
                ON llm_calls(session_id);
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
            "commands": self.get_commands(session_id),
            "llm_calls": self.get_llm_calls(session_id),
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

    # ── Commands ──────────────────────────────────────────────────

    def record_command(
        self,
        session_id: int,
        cmd: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        fault_id: int | None = None,
        privileged: bool = False,
    ) -> int:
        """Store the full output of a shell command executed during a session.

        Unlike :meth:`add_event`, this stores stdout and stderr as separate
        columns with no truncation, making them queryable and exportable.

        Parameters
        ----------
        session_id : int
        cmd : str
            The shell command that was run.
        exit_code : int
            Return code of the command.
        stdout : str
            Full standard output (not trimmed).
        stderr : str
            Full standard error (not trimmed).
        fault_id : int, optional
            Associate with a specific fault record.
        privileged : bool
            ``True`` if the command was run with ``sudo``.

        Returns
        -------
        int
            Command record id.
        """
        cur = self._conn.execute(
            "INSERT INTO commands "
            "(session_id, fault_id, timestamp, privileged, cmd, exit_code, stdout, stderr) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                fault_id,
                _now(),
                1 if privileged else 0,
                cmd,
                exit_code,
                stdout,
                stderr,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_commands(
        self,
        session_id: int,
        fault_id: int | None = None,
        failed_only: bool = False,
    ) -> list[dict]:
        """Return command records for a session.

        Parameters
        ----------
        session_id : int
        fault_id : int, optional
            Filter to a specific fault. Returns all faults if ``None``.
        failed_only : bool
            If ``True``, return only commands that exited non-zero.

        Returns
        -------
        list[dict]
            Each dict has keys: ``id``, ``session_id``, ``fault_id``,
            ``timestamp``, ``privileged``, ``cmd``, ``exit_code``,
            ``stdout``, ``stderr``.
        """
        clauses = ["session_id = ?"]
        params: list = [session_id]

        if fault_id is not None:
            clauses.append("fault_id = ?")
            params.append(fault_id)
        if failed_only:
            clauses.append("exit_code != 0")

        where = " AND ".join(clauses)
        rows = self._conn.execute(
            f"SELECT * FROM commands WHERE {where} ORDER BY id",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Traces ────────────────────────────────────────────────────

    def add_trace_event(
        self,
        session_id: int,
        kind: str,
        data: dict,
    ) -> int:
        """Append a structured trace event to the session.

        Trace events capture the LLM interaction boundary: prompts sent,
        responses received, tool calls made, token usage, cost, and retries.
        They are the foundation for oracle assertions and replay.

        Parameters
        ----------
        session_id : int
        kind : str
            Event type — one of ``"prompt"``, ``"response"``, ``"tool_call"``,
            ``"tool_return"``, ``"retry"``, ``"fault_active"``, ``"oracle_result"``.
        data : dict
            Arbitrary JSON-serializable payload. Recommended keys by type:

            * ``"prompt"``      — ``question``, ``context``, ``messages``
            * ``"response"``    — ``response``, ``tokens_used``, ``cost_usd``,
              ``model``, ``finish_reason``
            * ``"tool_call"``   — ``tool_name``, ``args``
            * ``"tool_return"`` — ``tool_name``, ``output``, ``duration_s``
            * ``"retry"``       — ``attempt``, ``reason``
            * ``"oracle_result"`` — ``oracle``, ``passed``, ``score``, ``reason``

        Returns
        -------
        int
            Trace event id.
        """
        cur = self._conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM traces WHERE session_id = ?",
            (session_id,),
        )
        seq = cur.fetchone()[0]
        cur = self._conn.execute(
            "INSERT INTO traces (session_id, seq, timestamp, kind, data) VALUES (?, ?, ?, ?, ?)",
            (session_id, seq, _now(), kind, json.dumps(data)),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_trace(self, session_id: int) -> list[dict]:
        """Return all trace events for a session in sequence order.

        Parameters
        ----------
        session_id : int

        Returns
        -------
        list[dict]
            Each dict has keys: ``id``, ``session_id``, ``seq``,
            ``timestamp``, ``kind``, ``data`` (parsed from JSON).
        """
        rows = self._conn.execute(
            "SELECT * FROM traces WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        out = []
        for r in rows:
            row = dict(r)
            try:
                row["data"] = json.loads(row["data"])
            except (TypeError, ValueError):
                pass
            out.append(row)
        return out

    def get_trace_events_by_kind(self, session_id: int, kind: str) -> list[dict]:
        """Return trace events of a specific kind for a session.

        Parameters
        ----------
        session_id : int
        kind : str
            Event kind to filter by (e.g. ``"response"``, ``"tool_call"``).

        Returns
        -------
        list[dict]
        """
        rows = self._conn.execute(
            "SELECT * FROM traces WHERE session_id = ? AND kind = ? ORDER BY seq",
            (session_id, kind),
        ).fetchall()
        out = []
        for r in rows:
            row = dict(r)
            try:
                row["data"] = json.loads(row["data"])
            except (TypeError, ValueError):
                pass
            out.append(row)
        return out

    # ── LLM Calls ─────────────────────────────────────────────────

    def record_llm_call(
        self,
        session_id: int,
        phase: str,
        call_index: int,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        finish_reason: str,
        prompt_text: str,
        response_text: str,
        latency_s: float,
        http_status: int = 200,
    ) -> int:
        """Store a single LLM API call captured by the proxy.

        Called by the LLM proxy subprocess after each forwarded request.

        Returns
        -------
        int
            LLM call record id.
        """
        cur = self._conn.execute(
            "INSERT INTO llm_calls "
            "(session_id, phase, call_index, timestamp, model, "
            " prompt_tokens, completion_tokens, cost_usd, finish_reason, "
            " prompt_text, response_text, latency_s, http_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id, phase, call_index, _now(), model,
                prompt_tokens, completion_tokens, cost_usd, finish_reason,
                prompt_text, response_text, latency_s, http_status,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_llm_calls(
        self,
        session_id: int,
        phase: str | None = None,
    ) -> list[dict]:
        """Return LLM call records for a session.

        Parameters
        ----------
        session_id : int
        phase : str, optional
            Filter to a specific phase (``"fault"``, ``"baseline"``).
            Returns all phases if ``None``.

        Returns
        -------
        list[dict]
            Each dict has keys: ``id``, ``session_id``, ``phase``,
            ``call_index``, ``timestamp``, ``model``, ``prompt_tokens``,
            ``completion_tokens``, ``cost_usd``, ``finish_reason``,
            ``prompt_text``, ``response_text``, ``latency_s``,
            ``http_status``.
        """
        if phase is not None:
            rows = self._conn.execute(
                "SELECT * FROM llm_calls WHERE session_id = ? AND phase = ? ORDER BY call_index",
                (session_id, phase),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM llm_calls WHERE session_id = ? ORDER BY call_index",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
