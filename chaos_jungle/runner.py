"""ChaosRunner — orchestrates the fault lifecycle."""

from __future__ import annotations
import threading
import time

from chaos_jungle._duration import parse_duration
from chaos_jungle.db.session_db import SessionDB
from chaos_jungle.guardrails import apply_guardrails
from chaos_jungle.scenario import Scenario
from chaos_jungle.targets.base import Target
from chaos_jungle.targets.local import LocalTarget


class ChaosRunner:
    """Orchestrate the start/stop/revert lifecycle of a chaos scenario.

    Handles all four usage modes:

    * **Decorator** — via :func:`chaos_jungle.decorators.chaos`
    * **Context manager** — via :func:`chaos_jungle.decorators.chaos_session`
    * **Explicit** — ``runner.start()`` / ``runner.stop()``
    * **Separate** — ``runner.start()`` returns immediately; use
      :meth:`attach` from another process to stop

    Parameters
    ----------
    scenario : Scenario
        The scenario to run.
    target : Target, optional
        Where to run the faults. Defaults to :class:`~chaos_jungle.targets.local.LocalTarget`.
    db : SessionDB, optional
        Database instance. A default one is created if not provided.
    auto_preflight : bool, optional
        Run preflight checks before starting. Default ``True``.

    Examples
    --------
    Explicit mode::

        runner = ChaosRunner(scenario, SSHTarget("worker1", user="ubuntu"))
        runner.start()
        # ... your workload ...
        runner.stop()

    Separate mode (two processes)::

        # Process 1
        runner = ChaosRunner(scenario, LocalTarget())
        runner.start()   # returns immediately

        # Process 2
        runner = ChaosRunner.attach()
        runner.stop()
    """

    def __init__(
        self,
        scenario: Scenario,
        target: Target | None = None,
        db: SessionDB | None = None,
        auto_preflight: bool = True,
        auto_install: bool = False,
        conflict: str = "raise",
    ) -> None:
        if conflict not in ("raise", "warn", "force"):
            raise ValueError(f"conflict must be 'raise', 'warn', or 'force', got {conflict!r}")
        self.scenario = scenario
        self.target = target or LocalTarget()
        self.db = db or SessionDB()
        self.auto_preflight = auto_preflight
        self.auto_install = auto_install
        self.conflict = conflict
        self._session_id: int | None = None
        self._fault_ids: list[int] = []
        self._timer: threading.Timer | None = None

    # ── Public API ────────────────────────────────────────────────

    def run(self, duration: str | int | float) -> None:
        """Start chaos, wait for ``duration``, then stop and revert.

        Blocking call. Chaos is active for exactly the specified duration
        regardless of any external workload.

        Parameters
        ----------
        duration : str or int or float
            How long to keep chaos active. Accepts human-readable strings
            like ``"10m"``, ``"1h30m"``, ``"90s"``, or a plain number
            of seconds.

        Examples
        --------
        >>> runner = ChaosRunner(scenario, LocalTarget())
        >>> runner.run("10m")   # chaos on for 10 minutes, then off

        >>> runner.run("1h")    # chaos on for 1 hour

        >>> runner.run(30)      # chaos on for 30 seconds
        """
        seconds = parse_duration(duration)
        self.start()
        print(f"[chaos-jungle] Chaos ON — running for {duration} ({seconds:.0f}s)")
        try:
            time.sleep(seconds)
        finally:
            print(f"[chaos-jungle] Duration reached — stopping chaos")
            self.stop()

    def start(self, duration: str | int | float | None = None) -> "ChaosRunner":
        """Inject all faults in the scenario.

        Opens a database session, runs preflight checks if enabled,
        then starts each fault in order.

        Parameters
        ----------
        duration : str or int or float, optional
            If given, a background timer will automatically stop and
            revert all faults after this duration. Accepts the same
            formats as :meth:`run`. Use this for fire-and-forget mode.

        Returns
        -------
        ChaosRunner
            Self, for chaining.
        """
        self.target.connect()

        # guardrails — scenario + runtime checks
        apply_guardrails(
            self.scenario,
            self.target,
            conflict=self.conflict,
            runtime=True,
        )

        self._session_id = self.db.open_session(self.scenario.name)
        self.db.add_event(self._session_id, f"Session started: {self.scenario.name}")

        if self.auto_preflight:
            for fault in self.scenario.faults:
                fault.preflight(self.target, auto_install=self.auto_install)

        self._fault_ids = []
        for fault in self.scenario.faults:
            fid = self.db.record_fault(
                self._session_id,
                fault.__class__.__name__,
                fault._parameters(),
            )
            self._fault_ids.append(fid)
            self.db.add_event(
                self._session_id,
                f"Starting fault: {fault.__class__.__name__}",
                fault_id=fid,
            )
            fault.start(self.target)
            self.db.add_event(
                self._session_id,
                f"Fault started: {fault.__class__.__name__}",
                fault_id=fid,
            )

        if duration is not None:
            seconds = parse_duration(duration)
            self._timer = threading.Timer(seconds, self._auto_stop)
            self._timer.daemon = True
            self._timer.start()
            print(f"[chaos-jungle] Chaos ON — auto-stop in {duration} ({seconds:.0f}s)")

        return self

    def _auto_stop(self) -> None:
        """Called by the background timer when duration expires."""
        print(f"[chaos-jungle] Duration reached — auto-stopping chaos")
        try:
            self.stop()
        except Exception as exc:
            print(f"[chaos-jungle] ERROR during auto-stop: {exc}")

    def stop(self) -> None:
        """Stop and revert all faults in the scenario.

        Always runs even if the workload crashed. Closes the database
        session when done. Cancels any active duration timer.
        """
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

        if self._session_id is None:
            raise RuntimeError("No active session — call start() first or use attach()")

        errors = []
        for fault, fid in zip(reversed(self.scenario.faults), reversed(self._fault_ids)):
            try:
                self.db.add_event(
                    self._session_id,
                    f"Stopping fault: {fault.__class__.__name__}",
                    fault_id=fid,
                )
                fault.stop(self.target)
                fault.revert(self.target)
                self.db.close_fault(fid)
                self.db.add_event(
                    self._session_id,
                    f"Fault stopped and reverted: {fault.__class__.__name__}",
                    fault_id=fid,
                )
            except Exception as exc:
                errors.append(exc)
                self.db.add_event(
                    self._session_id,
                    f"ERROR stopping {fault.__class__.__name__}: {exc}",
                    fault_id=fid,
                )

        self.db.close_session(self._session_id, status="reverted")
        self.db.add_event(self._session_id, "Session closed")
        self.target.disconnect()

        if errors:
            raise RuntimeError(f"Errors during stop: {errors}")

    def summary(self) -> dict:
        """Return a concise, human-readable summary of the session.

        Useful for quick inspection after a run. Contains:

        * ``name``       — scenario name
        * ``session_id`` — database id
        * ``status``     — ``"reverted"``, ``"running"``, etc.
        * ``started_at`` / ``stopped_at`` — ISO-8601 UTC timestamps
        * ``duration_s`` — wall-clock seconds chaos was active (``None`` if still running)
        * ``faults``     — list of ``{kind, parameters}`` dicts
        * ``errors``     — any ERROR lines from the event log

        Returns
        -------
        dict

        Examples
        --------
        >>> runner.stop()
        >>> s = runner.summary()
        >>> print(s["duration_s"], "seconds of chaos")
        >>> print(s["errors"])   # empty list if everything was clean
        """
        if self._session_id is None:
            raise RuntimeError("No active session — call start() first")

        data = self.db.export_session(self._session_id)
        sess = data["session"]

        # compute duration
        duration_s = None
        if sess.get("started_at") and sess.get("stopped_at"):
            from datetime import datetime, timezone
            fmt = "%Y-%m-%dT%H:%M:%S.%f%z"
            try:
                t0 = datetime.fromisoformat(sess["started_at"])
                t1 = datetime.fromisoformat(sess["stopped_at"])
                duration_s = round((t1 - t0).total_seconds(), 1)
            except ValueError:
                pass

        errors = [
            e["message"] for e in data["events"]
            if e["message"].startswith("ERROR")
        ]

        return {
            "name":        sess["name"],
            "session_id":  sess["id"],
            "status":      sess["status"],
            "started_at":  sess["started_at"],
            "stopped_at":  sess["stopped_at"],
            "duration_s":  duration_s,
            "faults": [
                {"kind": f["kind"], "parameters": f["parameters"]}
                for f in data["faults"]
            ],
            "errors": errors,
        }

    def export(self, fmt: str = "dict") -> dict | str:
        """Export the current session data.

        Parameters
        ----------
        fmt : str
            ``"dict"`` or ``"json"``.

        Returns
        -------
        dict or str
        """
        if self._session_id is None:
            raise RuntimeError("No active session")
        data = self.db.export_session(self._session_id)
        if fmt == "json":
            import json
            return json.dumps(data, indent=2)
        return data

    # ── Separate mode ─────────────────────────────────────────────

    @classmethod
    def attach(
        cls,
        db: SessionDB | None = None,
        target: Target | None = None,
    ) -> "ChaosRunner":
        """Attach to the most recent running session.

        Used in separate mode to stop chaos from a different process.

        Parameters
        ----------
        db : SessionDB, optional
            Database to look up the active session in.
        target : Target, optional
            Target to run stop/revert commands on.

        Returns
        -------
        ChaosRunner
            Runner bound to the active session.

        Raises
        ------
        RuntimeError
            If no running session is found.
        """
        db = db or SessionDB()
        session = db.active_session()
        if session is None:
            raise RuntimeError("No running session found in the database")

        # Reconstruct scenario from DB records (for display only — faults
        # are stopped via their known CLI commands, not Python objects)
        runner = cls.__new__(cls)
        runner.scenario = Scenario(session["name"], faults=[])
        runner.target = target or LocalTarget()
        runner.db = db
        runner.auto_preflight = False
        runner._session_id = session["id"]
        runner._fault_ids = []
        return runner
