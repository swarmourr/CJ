"""ChaosRunner — orchestrates the fault lifecycle."""

from __future__ import annotations
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, TYPE_CHECKING

from chaos_jungle._duration import parse_duration
from chaos_jungle.db.session_db import SessionDB
from chaos_jungle.guardrails import apply_guardrails, SafetyPolicy
from chaos_jungle.scenario import Scenario
from chaos_jungle.targets.base import Target
from chaos_jungle.targets.local import LocalTarget
from chaos_jungle.targets.logging import LoggingTarget

if TYPE_CHECKING:
    from chaos_jungle.judge import JudgeScore, LLMJudge
    from chaos_jungle.oracles import Oracle, OracleResult


@dataclass
class MeasurementResult:
    """Result returned by :meth:`ChaosRunner.measure`.

    Attributes
    ----------
    scenario : str
        Scenario name.
    session_id : int
        Database session id of the fault run.
    baseline : dict
        Average metrics from workload runs *without* any fault.
    fault : dict
        Average metrics from workload runs *with* the fault active.
    delta : dict
        ``fault[k] - baseline[k]`` for every numeric metric.
        Positive = fault made things worse; negative = better.
    n_baseline : int
        Number of baseline trials.
    n_fault : int
        Number of fault trials.
    judge_baseline : JudgeScore or None
        Average quality scores from the judge evaluator during baseline runs.
        ``None`` if no evaluator was provided to :meth:`ChaosRunner.measure`.
    judge_fault : JudgeScore or None
        Average quality scores from the judge evaluator during fault runs.
    judge_delta : dict
        Difference between fault and baseline judge scores (fault - baseline).
        Positive hallucination delta = fault caused more hallucination.
    """

    scenario: str
    session_id: int
    baseline: dict
    fault: dict
    delta: dict
    n_baseline: int
    n_fault: int
    raw_baseline: list = field(default_factory=list, repr=False)
    raw_fault: list = field(default_factory=list, repr=False)
    judge_baseline: "JudgeScore | None" = field(default=None, repr=False)
    judge_fault: "JudgeScore | None" = field(default=None, repr=False)
    judge_delta: dict = field(default_factory=dict)
    oracle_results: "list[OracleResult]" = field(default_factory=list)

    def passed(self, key: str, threshold: float) -> bool:
        """Return True if ``abs(delta[key]) <= threshold``."""
        return abs(self.delta.get(key, 0.0)) <= threshold

    def passed_quality(
        self,
        faithfulness_min: float = 0.7,
        hallucination_max: float = 0.3,
    ) -> bool:
        """Return True if the fault did not degrade response quality below thresholds.

        Requires an evaluator to have been passed to :meth:`ChaosRunner.measure`.

        Parameters
        ----------
        faithfulness_min : float
            Minimum acceptable faithfulness during fault runs. Default ``0.7``.
        hallucination_max : float
            Maximum acceptable hallucination during fault runs. Default ``0.3``.
        """
        if self.judge_fault is None:
            raise RuntimeError(
                "No judge scores available — pass evaluator= to ChaosRunner.measure()."
            )
        return self.judge_fault.passed(faithfulness_min, hallucination_max)

    def passed_oracles(self, phase: str | None = None) -> bool:
        """Return ``True`` if all oracle assertions passed.

        Parameters
        ----------
        phase : str, optional
            Filter to a specific phase — ``"baseline"``, ``"fault"``, or
            ``"both"``.  When ``None`` (default), all results are checked.

        Returns
        -------
        bool
            ``True`` only if every oracle in ``oracle_results`` passed.

        Raises
        ------
        RuntimeError
            If no oracles were passed to :meth:`ChaosRunner.measure`.

        Examples
        --------
        ::

            result = runner.measure(workload, oracles=[NoPIILeakage(), MaxCost(0.05)])
            if not result.passed_oracles():
                for r in result.oracle_results:
                    if not r.passed:
                        print(f"FAIL {r.oracle}: {r.reason}")
        """
        if not self.oracle_results:
            raise RuntimeError(
                "No oracle results available — pass oracles= to ChaosRunner.measure()."
            )
        subset = self.oracle_results
        if phase is not None:
            subset = [r for r in self.oracle_results if r.phase in (phase, "both")]
        return all(r.passed for r in subset)

    def summary(self) -> str:
        """Human-readable table of baseline / fault / delta per metric."""
        lines = [
            f"Scenario : {self.scenario}",
            f"Trials   : {self.n_baseline} baseline / {self.n_fault} fault",
            "",
        ]
        for k in sorted(set(self.baseline) | set(self.fault)):
            b = self.baseline.get(k, "—")
            f = self.fault.get(k, "—")
            d = self.delta.get(k)
            d_str = f"  Δ {d:+.4g}" if d is not None else ""
            lines.append(f"  {k:<30} baseline={b}  fault={f}{d_str}")

        if self.judge_baseline is not None and self.judge_fault is not None:
            lines.append("")
            lines.append("  Quality scores (LLM-as-a-Judge):")
            jb, jf = self.judge_baseline, self.judge_fault
            for metric, b_val, f_val in [
                ("faithfulness", jb.faithfulness, jf.faithfulness),
                ("hallucination", jb.hallucination, jf.hallucination),
                ("coherence", jb.coherence, jf.coherence),
            ]:
                delta = round(f_val - b_val, 4)
                d_str = f"  Δ {delta:+.4g}"
                lines.append(
                    f"  {metric:<30} baseline={b_val:.3f}  fault={f_val:.3f}{d_str}"
                )
            lines.append(
                f"  {'guardrail_violation':<30} baseline={jb.guardrail_violation}  "
                f"fault={jf.guardrail_violation}"
            )
            if jf.reasoning:
                lines.append(f"\n  Judge note: {jf.reasoning}")

        if self.oracle_results:
            lines.append("")
            lines.append("  Oracle assertions:")
            for r in self.oracle_results:
                status = "PASS" if r.passed else "FAIL"
                score_str = f"  score={r.score:.2f}" if not r.passed else ""
                lines.append(
                    f"    [{status}] {r.oracle:<30} ({r.phase})  {r.reason}{score_str}"
                )
            n_fail = sum(1 for r in self.oracle_results if not r.passed)
            if n_fail:
                lines.append(f"  {n_fail} oracle(s) FAILED")
            else:
                lines.append(f"  All {len(self.oracle_results)} oracle(s) passed")

        return "\n".join(lines)


def _avg_metrics(runs: list[dict]) -> dict:
    """Average numeric values across multiple workload runs."""
    if not runs:
        return {}
    result = {}
    for k in runs[0]:
        vals = [r[k] for r in runs if isinstance(r.get(k), (int, float))]
        result[k] = round(sum(vals) / len(vals), 6) if vals else runs[0].get(k)
    return result


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
        policy: SafetyPolicy | None = None,
    ) -> None:
        if conflict not in ("raise", "warn", "force"):
            raise ValueError(f"conflict must be 'raise', 'warn', or 'force', got {conflict!r}")
        self.scenario = scenario
        self.target = target or LocalTarget()
        self.db = db or SessionDB()
        self.auto_preflight = auto_preflight
        self.auto_install = auto_install
        self.conflict = conflict
        self.policy = policy
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

    def start(
        self,
        duration: str | int | float | None = None,
        start_after: float = 0.0,
    ) -> "ChaosRunner":
        """Inject all faults in the scenario.

        Opens a database session, runs preflight checks if enabled,
        then starts each fault in order.

        Parameters
        ----------
        duration : str or int or float, optional
            If given, a background timer will automatically stop and
            revert all faults after this duration. Accepts the same
            formats as :meth:`run`. Use this for fire-and-forget mode.
        start_after : float, optional
            Seconds to wait before injecting the fault. The call returns
            immediately and injection happens in a background thread.
            Useful to inject a fault mid-workload::

                runner.start(start_after=30, duration=60)
                run_my_long_job()   # fault hits after 30 s, clears after 90 s

        Returns
        -------
        ChaosRunner
            Self, for chaining.
        """
        if start_after > 0:
            print(f"[chaos-jungle] Fault injection deferred — starting in {start_after}s")
            t = threading.Timer(start_after, lambda: self.start(duration=duration))
            t.daemon = True
            t.start()
            return self
        self.target.connect()

        # guardrails — scenario + runtime checks
        apply_guardrails(
            self.scenario,
            self.target,
            conflict=self.conflict,
            runtime=True,
        )

        # safety policy — danger level, path allowlist, target allowlist
        if self.policy is not None:
            self.policy.check_scenario(self.scenario)
            self.policy.check_target(self.target)

        self._session_id = self.db.open_session(self.scenario.name)
        self.db.add_event(self._session_id, f"Session started: {self.scenario.name}")

        # wrap target so every command is logged to the session DB
        logged = LoggingTarget(self.target, self.db, self._session_id)

        if self.auto_preflight:
            for fault in self.scenario.faults:
                fault.preflight(logged, auto_install=self.auto_install)

        self._fault_ids = []
        for fault in self.scenario.faults:
            fid = self.db.record_fault(
                self._session_id,
                fault.__class__.__name__,
                fault._parameters(),
            )
            self._fault_ids.append(fid)
            logged.fault_id = fid   # tag subsequent commands with this fault
            self.db.add_event(
                self._session_id,
                f"Starting fault: {fault.__class__.__name__}",
                fault_id=fid,
            )
            _dry = self.policy is not None and self.policy.dry_run
            if _dry:
                fault.dry_run(logged)
            else:
                print(f"[chaos-jungle] Injecting {fault.__class__.__name__}({fault._parameters()})")
                fault.start(logged)
            self.db.add_event(
                self._session_id,
                f"Fault started: {fault.__class__.__name__}",
                fault_id=fid,
            )

        print(f"[chaos-jungle] Chaos ON  — scenario '{self.scenario.name}'  "
              f"(session id: {self._session_id})")

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

        logged = LoggingTarget(self.target, self.db, self._session_id)
        errors = []
        for fault, fid in zip(reversed(self.scenario.faults), reversed(self._fault_ids)):
            try:
                logged.fault_id = fid
                self.db.add_event(
                    self._session_id,
                    f"Stopping fault: {fault.__class__.__name__}",
                    fault_id=fid,
                )
                fault.stop(logged)
                fault.revert(logged)
                self.db.close_fault(fid)
                self.db.add_event(
                    self._session_id,
                    f"Fault stopped and reverted: {fault.__class__.__name__}",
                    fault_id=fid,
                )
                print(f"[chaos-jungle] Reverted {fault.__class__.__name__}")
            except Exception as exc:
                errors.append(exc)
                self.db.add_event(
                    self._session_id,
                    f"ERROR stopping {fault.__class__.__name__}: {exc}",
                    fault_id=fid,
                )
                print(f"[chaos-jungle] ERROR reverting {fault.__class__.__name__}: {exc}")

        self.db.close_session(self._session_id, status="reverted")
        self.db.add_event(self._session_id, "Session closed")
        self.target.disconnect()
        print(f"[chaos-jungle] Chaos OFF — session {self._session_id} reverted.")

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

    def measure(
        self,
        workload: Callable[[], dict],
        n_baseline: int = 1,
        n_fault: int = 1,
        evaluator: "LLMJudge | None" = None,
        oracles: "list[Oracle] | None" = None,
    ) -> "MeasurementResult":
        """Run *workload* under baseline and fault conditions and compare.

        The workload callable must return a ``dict`` of numeric (or
        string) metrics each time it is called::

            def my_workload():
                t0 = time.time()
                errors = run_transfer()
                return {"duration_s": time.time() - t0, "errors": errors}

            result = runner.measure(my_workload, n_baseline=3, n_fault=3)
            print(result.summary())

        For AI quality evaluation, include ``"question"``, ``"context"``,
        and ``"response"`` keys in the returned dict and pass an
        :class:`~chaos_jungle.judge.LLMJudge` as *evaluator*::

            judge = LLMJudge(model="gpt-4o-mini")

            def my_ai_workload():
                response = call_my_agent("What is the capital of France?")
                return {
                    "question": "What is the capital of France?",
                    "context": "France is a Western European country. Its capital is Paris.",
                    "response": response,
                    "duration_s": 1.2,
                }

            result = runner.measure(my_ai_workload, n_baseline=3, n_fault=3, evaluator=judge)
            print(result.summary())   # includes faithfulness / hallucination scores

        Parameters
        ----------
        workload : callable
            Zero-argument callable returning a metrics dict.
        n_baseline : int
            How many times to run the workload *without* any fault.
            More trials reduce noise. Default ``1``.
        n_fault : int
            How many times to run the workload *with* the fault active.
            Default ``1``.
        evaluator : LLMJudge, optional
            An :class:`~chaos_jungle.judge.LLMJudge` instance. When provided,
            each workload result that contains ``"question"``, ``"context"``,
            and ``"response"`` keys is scored for faithfulness, hallucination,
            and coherence. Scores are averaged and included in
            :class:`MeasurementResult`.
        oracles : list[Oracle], optional
            Oracle assertion instances to run against the fault runs. Each
            oracle inspects the raw fault workload results and returns a
            pass/fail :class:`~chaos_jungle.oracles.OracleResult`.  Results
            are stored in :attr:`MeasurementResult.oracle_results` and shown
            in :meth:`MeasurementResult.summary`::

                from chaos_jungle.oracles import NoPIILeakage, MaxCost
                result = runner.measure(
                    workload, n_fault=3,
                    oracles=[NoPIILeakage(), MaxCost(max_usd=0.05)],
                )
                if not result.passed_oracles():
                    raise AssertionError("Oracle failure")

        Returns
        -------
        MeasurementResult
            Contains averaged baseline/fault metrics, their delta,
            optionally LLM quality scores when *evaluator* is provided,
            and oracle assertion results when *oracles* is provided.
        """
        from chaos_jungle.judge import average_scores  # lazy import

        # ── 1. Baseline runs (no fault) ───────────────────────────
        print(f"[chaos-jungle] Measuring baseline ({n_baseline} trial(s)) ...")
        raw_baseline: list[dict] = []
        for _ in range(n_baseline):
            raw_baseline.append(workload())
        baseline = _avg_metrics(raw_baseline)

        # ── 2. Fault runs ─────────────────────────────────────────
        print(f"[chaos-jungle] Measuring under fault ({n_fault} trial(s)) ...")
        self.start()
        raw_fault: list[dict] = []
        try:
            for _ in range(n_fault):
                raw_fault.append(workload())
        finally:
            self.stop()

        fault = _avg_metrics(raw_fault)

        # ── 3. Delta ──────────────────────────────────────────────
        delta = {
            k: round(fault[k] - baseline[k], 6)
            for k in baseline
            if k in fault and isinstance(fault.get(k), (int, float))
                         and isinstance(baseline.get(k), (int, float))
        }

        # ── 4. LLM quality evaluation (optional) ──────────────────
        judge_baseline_score = None
        judge_fault_score = None
        judge_delta: dict = {}

        if evaluator is not None:
            print(f"[chaos-jungle] Evaluating quality ({n_baseline} baseline + {n_fault} fault trial(s)) ...")

            b_scores = [
                evaluator.score(
                    question=r.get("question", ""),
                    context=r.get("context", ""),
                    response=r.get("response", ""),
                )
                for r in raw_baseline
                if "response" in r
            ]
            f_scores = [
                evaluator.score(
                    question=r.get("question", ""),
                    context=r.get("context", ""),
                    response=r.get("response", ""),
                )
                for r in raw_fault
                if "response" in r
            ]

            if b_scores:
                judge_baseline_score = average_scores(b_scores)
            if f_scores:
                judge_fault_score = average_scores(f_scores)

            if judge_baseline_score and judge_fault_score:
                jb, jf = judge_baseline_score, judge_fault_score
                judge_delta = {
                    "faithfulness": round(jf.faithfulness - jb.faithfulness, 4),
                    "hallucination": round(jf.hallucination - jb.hallucination, 4),
                    "coherence": round(jf.coherence - jb.coherence, 4),
                }

        # ── 5. Oracle assertions (optional) ───────────────────────
        oracle_results: list = []
        if oracles:
            from chaos_jungle.oracles import run_oracles
            print(f"[chaos-jungle] Running {len(oracles)} oracle assertion(s) ...")
            baseline_oracle = run_oracles(oracles, raw_baseline, phase="baseline")
            fault_oracle    = run_oracles(oracles, raw_fault,    phase="fault")
            # Interleave: baseline result then fault result for each oracle
            for b_res, f_res in zip(baseline_oracle, fault_oracle):
                oracle_results.append(b_res)
                oracle_results.append(f_res)
            n_fail = sum(1 for r in oracle_results if not r.passed)
            if n_fail:
                print(f"[chaos-jungle] Oracle: {n_fail} assertion(s) FAILED")
            else:
                print(f"[chaos-jungle] Oracle: all {len(oracles)} assertion(s) passed")

        result = MeasurementResult(
            scenario=self.scenario.name,
            session_id=self._session_id,
            baseline=baseline,
            fault=fault,
            delta=delta,
            n_baseline=n_baseline,
            n_fault=n_fault,
            raw_baseline=raw_baseline,
            raw_fault=raw_fault,
            judge_baseline=judge_baseline_score,
            judge_fault=judge_fault_score,
            judge_delta=judge_delta,
            oracle_results=oracle_results,
        )

        # ── 6. Persist to DB ──────────────────────────────────────
        db_result: dict = {"baseline": baseline, "fault": fault, "delta": delta}
        if judge_delta:
            db_result["judge_delta"] = judge_delta
            if judge_fault_score:
                db_result["judge_fault"] = judge_fault_score.to_dict()
        self.record_result(db_result)

        if oracle_results and self._session_id is not None:
            for r in oracle_results:
                self.db.add_trace_event(
                    self._session_id,
                    "oracle_result",
                    {
                        "oracle": r.oracle,
                        "passed": r.passed,
                        "score":  r.score,
                        "reason": r.reason,
                        "phase":  r.phase,
                    },
                )

        return result

    def door(
        self,
        fault_duration: "str | int | float" = 30,
        rest_duration: "str | int | float" = 30,
        cycles: int = 3,
        workload: "Callable[[], dict] | None" = None,
    ) -> "list[dict]":
        """Cycle between normal and fault states N times (door open / door closed).

        Each cycle:

        1. **Fault ON** — inject all faults, optionally run *workload*, wait for
           *fault_duration*.
        2. **Rest** — revert all faults, optionally run *workload* again to
           observe recovery, wait for *rest_duration*.

        Repeat *cycles* times.

        Parameters
        ----------
        fault_duration : str or int or float
            How long to keep faults active per cycle.
            Accepts ``"30s"``, ``"2m"``, or a plain number of seconds.
            Default ``30``.
        rest_duration : str or int or float
            How long to rest (no fault) between cycles.
            The workload is run at the start of the rest window if provided.
            Default ``30``.
        cycles : int
            Number of fault / rest cycles. Default ``3``.
        workload : callable, optional
            Zero-argument callable that returns a ``dict`` of metrics.
            Called once at the start of each **fault** phase and once at the
            start of each **rest** phase.  Return values are recorded to the
            session database and included in the result list.

        Returns
        -------
        list[dict]
            One dict per phase (fault + rest) per cycle::

                [
                  {"cycle": 1, "phase": "fault", "metrics": {...}, "session_id": 5},
                  {"cycle": 1, "phase": "rest",  "metrics": {...}, "session_id": 5},
                  {"cycle": 2, "phase": "fault", "metrics": {...}, "session_id": 6},
                  ...
                ]

        Examples
        --------
        No workload — pure timing::

            runner = ChaosRunner(
                Scenario("door", [NetworkDelay("200ms")]),
                SSHTarget("worker1"),
            )
            runner.door(fault_duration=30, rest_duration=30, cycles=5)

        With workload — measure impact and recovery::

            def call_llm():
                t0 = time.time()
                resp = openai.OpenAI().chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "user", "content": "ping"}],
                )
                return {"duration_s": round(time.time() - t0, 2), "ok": 1}

            results = runner.door(
                fault_duration="30s",
                rest_duration="30s",
                cycles=3,
                workload=call_llm,
            )

            for r in results:
                print(r["cycle"], r["phase"], r["metrics"])

        With the intercept layer (no proxy setup needed)::

            from chaos_jungle.intercept import door, Latency

            results = door(
                Latency(3.0),
                fault_duration=30,
                rest_duration=30,
                cycles=3,
                workload=call_llm,
            )
        """
        fault_s = parse_duration(fault_duration)
        rest_s = parse_duration(rest_duration)
        results: list[dict] = []

        print(
            f"[chaos-jungle] Door test START — {cycles} cycle(s), "
            f"fault={fault_s:.0f}s / rest={rest_s:.0f}s"
        )

        for i in range(1, cycles + 1):
            print(f"\n[chaos-jungle] ── Cycle {i}/{cycles} ─────────────────────────")

            # ── Fault phase ───────────────────────────────────────
            print(f"[chaos-jungle]   FAULT ON  ({fault_s:.0f}s)")
            self.start()
            fault_metrics: dict = {}
            try:
                t0 = time.time()
                if workload is not None:
                    fault_metrics = workload() or {}
                elapsed = time.time() - t0
                remaining = fault_s - elapsed
                if remaining > 0:
                    time.sleep(remaining)
            finally:
                self.stop()

            if fault_metrics:
                self.record_result({**fault_metrics, "_phase": "fault", "_cycle": i})

            results.append({
                "cycle":      i,
                "phase":      "fault",
                "metrics":    fault_metrics,
                "session_id": self._session_id,
            })

            # ── Rest phase ────────────────────────────────────────
            if rest_s > 0:
                print(f"[chaos-jungle]   REST     ({rest_s:.0f}s)")
                rest_metrics: dict = {}
                t0 = time.time()
                if workload is not None:
                    rest_metrics = workload() or {}
                elapsed = time.time() - t0
                remaining = rest_s - elapsed
                if remaining > 0:
                    time.sleep(remaining)

                results.append({
                    "cycle":      i,
                    "phase":      "rest",
                    "metrics":    rest_metrics,
                    "session_id": self._session_id,
                })

        print(f"\n[chaos-jungle] Door test DONE — {cycles} cycle(s) completed.")
        return results

    def record_result(self, metrics: dict) -> None:
        """Attach workflow outcome metrics to the current session.

        Call this after your workload completes to link observed results
        (throughput, retries, integrity failures …) to the chaos session.
        Results appear in the dashboard session drawer.

        Parameters
        ----------
        metrics : dict
            Any JSON-serializable dict, e.g.::

                runner.record_result({
                    "files_transferred": 120,
                    "files_corrupted":    3,
                    "retries":            7,
                    "throughput_mbps":   42.1,
                    "integrity_failures": 3,
                })
        """
        if self._session_id is None:
            raise RuntimeError("No active session — call start() first")
        self.db.record_result(self._session_id, metrics)
        self.db.add_event(
            self._session_id,
            f"Result recorded: {metrics}",
        )

    def commands(
        self,
        fault_id: int | None = None,
        failed_only: bool = False,
    ) -> list[dict]:
        """Return all command records captured during the current session.

        Every ``run()`` and ``sudo()`` call made by faults is stored in full
        (untruncated stdout + stderr) in a dedicated ``commands`` table.

        Parameters
        ----------
        fault_id : int, optional
            Filter to a specific fault record id.
        failed_only : bool
            If ``True``, return only commands that exited non-zero.

        Returns
        -------
        list[dict]
            Each dict contains:

            * ``cmd``       — the shell command
            * ``exit_code`` — return code
            * ``stdout``    — full standard output
            * ``stderr``    — full standard error
            * ``privileged``— ``1`` if run with sudo, ``0`` otherwise
            * ``timestamp`` — ISO-8601 UTC time
            * ``fault_id``  — associated fault id (or ``None``)

        Examples
        --------
        Print all commands from the last run::

            runner.stop()
            for cmd in runner.commands():
                print(cmd["cmd"], "→", cmd["exit_code"])

        Print only failed commands::

            for cmd in runner.commands(failed_only=True):
                print(cmd["cmd"])
                print(cmd["stderr"])

        Print commands for a specific fault::

            for cmd in runner.commands(fault_id=runner._fault_ids[0]):
                print(cmd["stdout"])
        """
        if self._session_id is None:
            raise RuntimeError("No active session — call start() first")
        return self.db.get_commands(
            self._session_id,
            fault_id=fault_id,
            failed_only=failed_only,
        )

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
