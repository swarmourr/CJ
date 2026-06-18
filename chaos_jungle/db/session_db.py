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
                status      TEXT    NOT NULL DEFAULT 'running',
                target_type TEXT    NOT NULL DEFAULT '',
                target_addr TEXT    NOT NULL DEFAULT ''
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
                id                             INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id                     INTEGER NOT NULL REFERENCES sessions(id),
                phase                          TEXT    NOT NULL DEFAULT 'fault',
                call_index                     INTEGER NOT NULL DEFAULT 0,
                timestamp                      TEXT    NOT NULL,
                model                          TEXT    NOT NULL DEFAULT '',
                prompt_tokens                  INTEGER NOT NULL DEFAULT 0,
                completion_tokens              INTEGER NOT NULL DEFAULT 0,
                cost_usd                       REAL    NOT NULL DEFAULT 0.0,
                finish_reason                  TEXT    NOT NULL DEFAULT '',
                prompt_text                    TEXT    NOT NULL DEFAULT '',
                response_text                  TEXT    NOT NULL DEFAULT '',
                latency_s                      REAL    NOT NULL DEFAULT 0.0,
                http_status                    INTEGER NOT NULL DEFAULT 200,
                fault_name                     TEXT    NOT NULL DEFAULT '',
                was_blocked                    INTEGER NOT NULL DEFAULT 0,
                was_modified                   INTEGER NOT NULL DEFAULT 0,
                total_tokens                   INTEGER NOT NULL DEFAULT 0,
                tokens_per_second              REAL    NOT NULL DEFAULT 0.0,
                request_size_bytes             INTEGER NOT NULL DEFAULT 0,
                response_size_bytes            INTEGER NOT NULL DEFAULT 0,
                message_count                  INTEGER NOT NULL DEFAULT 0,
                tool_count                     INTEGER NOT NULL DEFAULT 0,
                response_tool_calls            INTEGER NOT NULL DEFAULT 0,
                is_streaming                   INTEGER NOT NULL DEFAULT 0,
                temperature                    REAL,
                max_tokens_requested           INTEGER,
                response_length_chars          INTEGER NOT NULL DEFAULT 0,
                ttft_s                         REAL,
                system_fingerprint             TEXT    NOT NULL DEFAULT '',
                rate_limit_remaining_requests  INTEGER,
                rate_limit_remaining_tokens    INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_llm_calls_session
                ON llm_calls(session_id);

            CREATE TABLE IF NOT EXISTS tool_calls (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id   INTEGER NOT NULL REFERENCES sessions(id),
                llm_call_id  INTEGER NOT NULL DEFAULT 0,
                timestamp    TEXT    NOT NULL,
                phase        TEXT    NOT NULL DEFAULT '',
                seq          INTEGER NOT NULL DEFAULT 0,
                tool_name    TEXT    NOT NULL DEFAULT '',
                tool_id      TEXT    NOT NULL DEFAULT '',
                arguments    TEXT    NOT NULL DEFAULT '{}',
                result       TEXT    NOT NULL DEFAULT '',
                was_error    INTEGER NOT NULL DEFAULT 0,
                agent_addr   TEXT    NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_tool_calls_session
                ON tool_calls(session_id);

            CREATE INDEX IF NOT EXISTS idx_tool_calls_llm_call
                ON tool_calls(llm_call_id);

            CREATE TABLE IF NOT EXISTS resource_samples (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id     INTEGER NOT NULL REFERENCES sessions(id),
                fault_id       INTEGER REFERENCES faults(id),
                timestamp      TEXT    NOT NULL,
                elapsed_s      REAL    NOT NULL DEFAULT 0.0,
                phase          TEXT    NOT NULL DEFAULT 'fault',
                cpu_pct        REAL,
                mem_pct        REAL,
                mem_used_mb    REAL,
                mem_total_mb   REAL,
                disk_read_mb   REAL,
                disk_write_mb  REAL,
                net_rx_mb      REAL,
                net_tx_mb      REAL,
                load_1         REAL,
                load_5         REAL,
                extra          TEXT    NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_resource_samples_session
                ON resource_samples(session_id);

            CREATE TABLE IF NOT EXISTS fault_impact (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id              INTEGER NOT NULL REFERENCES sessions(id),
                computed_at             TEXT    NOT NULL,
                calls_baseline          INTEGER NOT NULL DEFAULT 0,
                calls_fault             INTEGER NOT NULL DEFAULT 0,
                error_rate_baseline     REAL    NOT NULL DEFAULT 0.0,
                error_rate_fault        REAL    NOT NULL DEFAULT 0.0,
                avg_latency_baseline    REAL    NOT NULL DEFAULT 0.0,
                avg_latency_fault       REAL    NOT NULL DEFAULT 0.0,
                p99_latency_baseline    REAL    NOT NULL DEFAULT 0.0,
                p99_latency_fault       REAL    NOT NULL DEFAULT 0.0,
                cost_baseline           REAL    NOT NULL DEFAULT 0.0,
                cost_fault              REAL    NOT NULL DEFAULT 0.0,
                tokens_baseline         INTEGER NOT NULL DEFAULT 0,
                tokens_fault            INTEGER NOT NULL DEFAULT 0,
                blocked_count           INTEGER NOT NULL DEFAULT 0,
                modified_count          INTEGER NOT NULL DEFAULT 0,
                retry_count             INTEGER NOT NULL DEFAULT 0,
                tool_calls_baseline     INTEGER NOT NULL DEFAULT 0,
                tool_calls_fault        INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_fault_impact_session
                ON fault_impact(session_id);
        """)
        # Migrate existing llm_calls tables that are missing the new columns
        _new_cols = [
            ("fault_name",                    "TEXT NOT NULL DEFAULT ''"),
            ("was_blocked",                   "INTEGER NOT NULL DEFAULT 0"),
            ("was_modified",                  "INTEGER NOT NULL DEFAULT 0"),
            ("total_tokens",                  "INTEGER NOT NULL DEFAULT 0"),
            ("tokens_per_second",             "REAL NOT NULL DEFAULT 0.0"),
            ("request_size_bytes",            "INTEGER NOT NULL DEFAULT 0"),
            ("response_size_bytes",           "INTEGER NOT NULL DEFAULT 0"),
            ("message_count",                 "INTEGER NOT NULL DEFAULT 0"),
            ("tool_count",                    "INTEGER NOT NULL DEFAULT 0"),
            ("response_tool_calls",           "INTEGER NOT NULL DEFAULT 0"),
            ("is_streaming",                  "INTEGER NOT NULL DEFAULT 0"),
            ("temperature",                   "REAL"),
            ("max_tokens_requested",          "INTEGER"),
            ("response_length_chars",         "INTEGER NOT NULL DEFAULT 0"),
            ("ttft_s",                        "REAL"),
            ("system_fingerprint",            "TEXT NOT NULL DEFAULT ''"),
            ("rate_limit_remaining_requests", "INTEGER"),
            ("rate_limit_remaining_tokens",   "INTEGER"),
            # New enrichment columns
            ("system_prompt",                 "TEXT NOT NULL DEFAULT ''"),
            ("full_messages_json",            "TEXT NOT NULL DEFAULT ''"),
            ("error_type",                    "TEXT NOT NULL DEFAULT 'none'"),
            ("is_retry",                      "INTEGER NOT NULL DEFAULT 0"),
            ("is_final_response",             "INTEGER NOT NULL DEFAULT 0"),
            ("fault_offset_s",                "REAL"),
        ]
        for col, defn in _new_cols:
            try:
                self._conn.execute(f"ALTER TABLE llm_calls ADD COLUMN {col} {defn}")
            except Exception:
                pass  # column already exists

        _fault_cols = [
            ("injection_verified",  "INTEGER NOT NULL DEFAULT 0"),
            ("verification_output", "TEXT NOT NULL DEFAULT ''"),
            ("snapshot_before",     "TEXT NOT NULL DEFAULT '{}'"),
            ("snapshot_after",      "TEXT NOT NULL DEFAULT '{}'"),
        ]
        for col, defn in _fault_cols:
            try:
                self._conn.execute(f"ALTER TABLE faults ADD COLUMN {col} {defn}")
            except Exception:
                pass

        _session_cols = [
            ("target_type", "TEXT NOT NULL DEFAULT ''"),
            ("target_addr", "TEXT NOT NULL DEFAULT ''"),
        ]
        for col, defn in _session_cols:
            try:
                self._conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {defn}")
            except Exception:
                pass
        self._conn.commit()

    # ── Tool Calls ────────────────────────────────────────────────

    def add_tool_call(
        self,
        session_id: int,
        tool_name: str,
        arguments: dict | str,
        result: str = "",
        *,
        llm_call_id: int = 0,
        phase: str = "",
        seq: int = 0,
        tool_id: str = "",
        was_error: bool = False,
        agent_addr: str = "",
    ) -> int:
        """Record a single tool call captured by the proxy."""
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments)
        cur = self._conn.execute(
            "INSERT INTO tool_calls "
            "(session_id, llm_call_id, timestamp, phase, seq, tool_name, tool_id, arguments, result, was_error, agent_addr) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, llm_call_id, _now(), phase, seq, tool_name, tool_id, arguments, result, 1 if was_error else 0, agent_addr),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_tool_calls(
        self,
        session_id: int,
        phase: str | None = None,
        llm_call_id: int | None = None,
    ) -> list[dict]:
        """Return tool call records for a session."""
        clauses = ["session_id = ?"]
        params: list = [session_id]
        if phase is not None:
            clauses.append("phase = ?")
            params.append(phase)
        if llm_call_id is not None:
            clauses.append("llm_call_id = ?")
            params.append(llm_call_id)
        where = " AND ".join(clauses)
        rows = self._conn.execute(
            f"SELECT * FROM tool_calls WHERE {where} ORDER BY id",
            params,
        ).fetchall()
        out = []
        for r in rows:
            row = dict(r)
            try:
                row["arguments"] = json.loads(row["arguments"])
            except (TypeError, ValueError):
                pass
            out.append(row)
        return out

    # ── Resource Samples ──────────────────────────────────────────

    def add_resource_sample(
        self,
        session_id: int,
        elapsed_s: float,
        *,
        fault_id: int | None = None,
        phase: str = "fault",
        cpu_pct: float | None = None,
        mem_pct: float | None = None,
        mem_used_mb: float | None = None,
        mem_total_mb: float | None = None,
        disk_read_mb: float | None = None,
        disk_write_mb: float | None = None,
        net_rx_mb: float | None = None,
        net_tx_mb: float | None = None,
        load_1: float | None = None,
        load_5: float | None = None,
        extra: dict | None = None,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO resource_samples "
            "(session_id, fault_id, timestamp, elapsed_s, phase, "
            " cpu_pct, mem_pct, mem_used_mb, mem_total_mb, "
            " disk_read_mb, disk_write_mb, net_rx_mb, net_tx_mb, "
            " load_1, load_5, extra) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, fault_id, _now(), elapsed_s, phase,
             cpu_pct, mem_pct, mem_used_mb, mem_total_mb,
             disk_read_mb, disk_write_mb, net_rx_mb, net_tx_mb,
             load_1, load_5, json.dumps(extra or {})),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_resource_samples(self, session_id: int, fault_id: int | None = None) -> list[dict]:
        if fault_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM resource_samples WHERE session_id=? AND fault_id=? ORDER BY elapsed_s",
                (session_id, fault_id),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM resource_samples WHERE session_id=? ORDER BY elapsed_s",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Fault enrichment ──────────────────────────────────────────

    def update_fault_snapshot(
        self,
        fault_id: int,
        *,
        snapshot_before: dict | None = None,
        snapshot_after: dict | None = None,
        injection_verified: bool | None = None,
        verification_output: str | None = None,
    ) -> None:
        sets, params = [], []
        if snapshot_before is not None:
            sets.append("snapshot_before=?"); params.append(json.dumps(snapshot_before))
        if snapshot_after is not None:
            sets.append("snapshot_after=?"); params.append(json.dumps(snapshot_after))
        if injection_verified is not None:
            sets.append("injection_verified=?"); params.append(1 if injection_verified else 0)
        if verification_output is not None:
            sets.append("verification_output=?"); params.append(verification_output)
        if not sets:
            return
        params.append(fault_id)
        self._conn.execute(f"UPDATE faults SET {', '.join(sets)} WHERE id=?", params)
        self._conn.commit()

    # ── Fault Impact ──────────────────────────────────────────────

    def compute_and_store_impact(self, session_id: int) -> dict:
        """Compute fault impact summary from llm_calls and store it."""
        rows = self._conn.execute(
            "SELECT phase, latency_s, http_status, was_blocked, was_modified, "
            "       is_retry, total_tokens, cost_usd, response_tool_calls "
            "FROM llm_calls WHERE session_id=?",
            (session_id,),
        ).fetchall()

        def _stats(calls: list) -> dict:
            if not calls:
                return {"count": 0, "error_rate": 0.0, "avg_lat": 0.0, "p99_lat": 0.0,
                        "cost": 0.0, "tokens": 0, "tool_calls": 0, "retries": 0}
            lats = sorted(r["latency_s"] or 0 for r in calls)
            errors = sum(1 for r in calls if (r["http_status"] or 200) >= 400 or r["was_blocked"])
            p99 = lats[max(0, int(len(lats) * 0.99) - 1)] if lats else 0.0
            return {
                "count":      len(calls),
                "error_rate": round(errors / len(calls), 4),
                "avg_lat":    round(sum(lats) / len(lats), 4),
                "p99_lat":    round(p99, 4),
                "cost":       round(sum(r["cost_usd"] or 0 for r in calls), 8),
                "tokens":     sum(r["total_tokens"] or 0 for r in calls),
                "tool_calls": sum(r["response_tool_calls"] or 0 for r in calls),
                "retries":    sum(1 for r in calls if r["is_retry"]),
            }

        baseline = [r for r in rows if r["phase"] == "baseline"]
        fault    = [r for r in rows if r["phase"] != "baseline"]
        bs = _stats(baseline)
        fs = _stats(fault)

        cur = self._conn.execute(
            "INSERT INTO fault_impact "
            "(session_id, computed_at, calls_baseline, calls_fault, "
            " error_rate_baseline, error_rate_fault, "
            " avg_latency_baseline, avg_latency_fault, "
            " p99_latency_baseline, p99_latency_fault, "
            " cost_baseline, cost_fault, "
            " tokens_baseline, tokens_fault, "
            " blocked_count, modified_count, retry_count, "
            " tool_calls_baseline, tool_calls_fault) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, _now(),
             bs["count"], fs["count"],
             bs["error_rate"], fs["error_rate"],
             bs["avg_lat"], fs["avg_lat"],
             bs["p99_lat"], fs["p99_lat"],
             bs["cost"], fs["cost"],
             bs["tokens"], fs["tokens"],
             sum(1 for r in rows if r["was_blocked"]),
             sum(1 for r in rows if r["was_modified"]),
             bs["retries"] + fs["retries"],
             bs["tool_calls"], fs["tool_calls"]),
        )
        self._conn.commit()
        return {"baseline": bs, "fault": fs}

    def get_fault_impact(self, session_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM fault_impact WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    # ── Sessions ──────────────────────────────────────────────────

    def open_session(
        self,
        name: str,
        target_type: str = "",
        target_addr: str = "",
    ) -> int:
        """Create a new session and return its id.

        Parameters
        ----------
        name : str
            Human-readable scenario name.
        target_type : str, optional
            Target kind: ``"local"``, ``"http"``, or ``"ssh"``.
        target_addr : str, optional
            Address of the target, e.g. ``"http://localhost:7781"`` or
            ``"user@host"``.

        Returns
        -------
        int
            Session id.
        """
        cur = self._conn.execute(
            "INSERT INTO sessions (name, started_at, status, target_type, target_addr)"
            " VALUES (?, ?, 'running', ?, ?)",
            (name, _now(), target_type, target_addr),
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
        fault_name: str = "",
        was_blocked: bool = False,
        was_modified: bool = False,
        total_tokens: int = 0,
        tokens_per_second: float = 0.0,
        request_size_bytes: int = 0,
        response_size_bytes: int = 0,
        message_count: int = 0,
        tool_count: int = 0,
        response_tool_calls: int = 0,
        is_streaming: bool = False,
        temperature: float | None = None,
        max_tokens_requested: int | None = None,
        response_length_chars: int = 0,
        ttft_s: float | None = None,
        system_fingerprint: str = "",
        rate_limit_remaining_requests: int | None = None,
        rate_limit_remaining_tokens: int | None = None,
    ) -> int:
        """Store a single LLM API call captured by the proxy."""
        cur = self._conn.execute(
            "INSERT INTO llm_calls ("
            "  session_id, phase, call_index, timestamp, model,"
            "  prompt_tokens, completion_tokens, cost_usd, finish_reason,"
            "  prompt_text, response_text, latency_s, http_status,"
            "  fault_name, was_blocked, was_modified, total_tokens, tokens_per_second,"
            "  request_size_bytes, response_size_bytes, message_count, tool_count,"
            "  response_tool_calls, is_streaming, temperature, max_tokens_requested,"
            "  response_length_chars, ttft_s, system_fingerprint,"
            "  rate_limit_remaining_requests, rate_limit_remaining_tokens"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                session_id, phase, call_index, _now(), model,
                prompt_tokens, completion_tokens, cost_usd, finish_reason,
                prompt_text, response_text, latency_s, http_status,
                fault_name, 1 if was_blocked else 0, 1 if was_modified else 0,
                total_tokens, tokens_per_second,
                request_size_bytes, response_size_bytes,
                message_count, tool_count,
                response_tool_calls, 1 if is_streaming else 0,
                temperature, max_tokens_requested,
                response_length_chars, ttft_s, system_fingerprint,
                rate_limit_remaining_requests, rate_limit_remaining_tokens,
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
