"""Experiment protocol: paired baseline + fault execution per task.

For every (system, benchmark_task, fault, seed) combination:
  1. Create a fresh isolated task environment.
  2. Execute the clean baseline (agent run, no fault active).
  3. Activate and verify the CJ fault.
  4. Execute the same task under the fault.
  5. Revert the fault and verify recovery.
  6. Persist configuration, metrics, lifecycle evidence, and artifacts.
  7. Remove transient task state (handled by sandbox_exec's tmpdir cleanup).

No agent memory, generated files, caches, or conversation history leak
between baseline and fault executions — each call to agent.run() is
stateless (new ModelClient, new message histories).
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from evaluation.agents.base import AgentRunResult, AgentSystem
from evaluation.benchmarks.base import BenchmarkTask
from evaluation.benchmarks.executor import score_task


@dataclass
class LifecycleEvidence:
    """CJ injection lifecycle evidence for one run."""
    configured:   bool = False
    activated:    bool | None = None   # None = not checked
    triggered:    bool | None = None
    manifested:   bool | None = None
    recovered:    bool | None = None
    verdict:      str = "pending"      # VALID / INVALID / INCONCLUSIVE
    session_id:   int | None = None
    details:      str = ""


@dataclass
class RunRecord:
    """Immutable raw record for one baseline or fault execution.

    Stored as one JSONL line per execution.
    """
    run_id:          str
    timestamp:       str
    cj_commit:       str
    agent_system:    str
    benchmark:       str
    task_id:         str
    model:           str
    endpoint_type:   str          # "openai", "local", "mock", "dry_run"
    seed:            int
    fault_type:      str          # "none" for baseline
    fault_parameters: dict
    target:          str
    phase:           str          # "baseline" or "fault"

    # Task outcome
    success:         float
    duration_s:      float
    reported_error:  float
    retries:         int
    llm_calls:       int
    tool_calls:      int
    turns:           int
    prompt_tokens:   int
    completion_tokens: int
    total_tokens:    int
    cost_usd:        float
    tests_passed:    int
    tests_total:     int
    generated_code_hash: str      # sha256[:12] of generated code (not the code itself)
    termination_reason: str
    exception:       str

    # Oracle / judge
    oracle_outcome:  dict = field(default_factory=dict)

    # Injection validity (fault phase only)
    lifecycle:       LifecycleEvidence = field(default_factory=LifecycleEvidence)

    # Validity classification
    validity:        str = "unchecked"  # valid / invalid / inconclusive / untriggered

    # Pairing: baseline and fault records for the same (task, repeat) share a pair_id
    pair_id:         str = ""

    # Artifact location
    artifact_path:   str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


def _detect_cj_commit() -> str:
    """Return HEAD SHA of the CJ package repo, falling back to env/hardcoded."""
    import subprocess
    pkg_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=pkg_root,
            stderr=subprocess.DEVNULL,
            timeout=2,
        ).decode().strip()
        if sha:
            return sha
    except Exception:
        pass
    return os.environ.get("CJ_COMMIT", "unknown")


_CJ_COMMIT = _detect_cj_commit()


def _code_hash(code: str) -> str:
    import hashlib
    return hashlib.sha256(code.encode()).hexdigest()[:12]


def _endpoint_type() -> str:
    url = os.environ.get("CJ_EVAL_BASE_URL", "")
    if not url:
        return "dry_run"
    if "127.0.0.1" in url or "localhost" in url:
        return "local"
    if "openai.com" in url:
        return "openai"
    return "openai_compat"


def _make_baseline_record(
    agent: AgentSystem,
    task: BenchmarkTask,
    result: AgentRunResult,
    seed: int,
    ok: bool,
    tests_passed: int,
    tests_total: int,
    exec_output: str,
    pair_id: str = "",
) -> RunRecord:
    return RunRecord(
        run_id=str(uuid.uuid4()),
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        cj_commit=_CJ_COMMIT,
        agent_system=agent.name,
        benchmark=task.benchmark,
        task_id=task.task_id,
        model=agent.client.model,
        endpoint_type=_endpoint_type(),
        seed=seed,
        fault_type="none",
        fault_parameters={},
        target="local",
        phase="baseline",
        success=1.0 if ok else 0.0,
        duration_s=result.duration_s,
        reported_error=result.reported_error,
        retries=result.retries,
        llm_calls=result.llm_calls,
        tool_calls=result.tool_calls,
        turns=result.turns,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        total_tokens=result.total_tokens,
        cost_usd=result.cost_usd,
        tests_passed=tests_passed,
        tests_total=tests_total,
        generated_code_hash=_code_hash(result.generated_code),
        termination_reason=result.termination_reason,
        exception=result.exception,
        validity="unchecked",
        pair_id=pair_id,
    )


class ExperimentProtocol:
    """Run paired baseline + fault experiments and emit RunRecords.

    Parameters
    ----------
    agent : AgentSystem
    fault_name : str
        One of the fault catalog names; ``"none"`` for baseline-only.
    output_dir : str
        Directory where JSONL records are appended.
    dry_run : bool
        When True no CJ proxy is started; fault phase uses the same
        (clean) agent client to verify protocol plumbing only.
    exec_timeout_s : float
        Sandbox execution timeout per task.
    """

    def __init__(
        self,
        agent: AgentSystem,
        fault_name: str,
        output_dir: str = "results",
        dry_run: bool = False,
        exec_timeout_s: float = 10.0,
    ) -> None:
        self.agent          = agent
        self.fault_name     = fault_name
        self.output_dir     = output_dir
        self.dry_run        = dry_run
        self.exec_timeout_s = exec_timeout_s
        os.makedirs(output_dir, exist_ok=True)

    def run_task(
        self,
        task: BenchmarkTask,
        seed: int = 0,
        repeats: int = 1,
    ) -> list[RunRecord]:
        """Run baseline + fault for one task, ``repeats`` times each.

        Returns a list of RunRecords (baseline records first, then fault).
        """
        records: list[RunRecord] = []

        for rep in range(repeats):
            task_seed = seed + rep * 1000
            # Shared ID ties baseline and fault record for this (task, repeat)
            pair_id = str(uuid.uuid4())

            # ── Baseline execution ─────────────────────────────────────────────
            b_result = self.agent.run(task.agent_prompt(), seed=task_seed)
            b_ok, b_tp, b_tt, b_out = score_task(
                task, b_result.generated_code, timeout_s=self.exec_timeout_s
            )
            b_rec = _make_baseline_record(
                self.agent, task, b_result, task_seed, b_ok, b_tp, b_tt, b_out,
                pair_id=pair_id,
            )
            records.append(b_rec)
            self._append_jsonl(b_rec)

            # ── Fault execution ────────────────────────────────────────────────
            if self.fault_name != "none":
                f_rec = self._run_fault_phase(task, task_seed, b_ok, pair_id=pair_id)
                records.append(f_rec)
                self._append_jsonl(f_rec)

        return records

    def run_campaign(
        self,
        tasks: list[BenchmarkTask],
        seed: int = 42,
        repeats: int = 1,
    ) -> list[RunRecord]:
        """Run full campaign across all tasks."""
        all_records: list[RunRecord] = []
        for i, task in enumerate(tasks):
            print(f"[eval] Task {i+1}/{len(tasks)}: {task.task_id}  "
                  f"fault={self.fault_name}  system={self.agent.name}")
            recs = self.run_task(task, seed=seed + i * 7, repeats=repeats)
            all_records.extend(recs)
        return all_records

    # ── Fault phase ────────────────────────────────────────────────────────────

    def _run_fault_phase(
        self,
        task: BenchmarkTask,
        seed: int,
        baseline_success: bool,
        pair_id: str = "",
    ) -> RunRecord:
        from evaluation.experiments.fault_campaign import build_cj_fault, get_fault_spec
        from chaos_jungle import Scenario, ChaosRunner
        from chaos_jungle.targets.local import LocalTarget

        spec  = get_fault_spec(self.fault_name)
        ev    = LifecycleEvidence(configured=True)

        if self.dry_run:
            # In dry-run: record configured but skip real injection
            ev.verdict = "INCONCLUSIVE"
            ev.details = "dry-run: fault not started"
            f_result = self.agent.run(task.agent_prompt(), seed=seed)
            f_ok, f_tp, f_tt, _ = score_task(
                task, f_result.generated_code, timeout_s=self.exec_timeout_s
            )
            validity = "inconclusive"
        else:
            # Real CJ injection
            fault    = build_cj_fault(self.fault_name)
            scenario = Scenario(f"eval-{self.fault_name}", [fault])
            runner   = ChaosRunner(scenario, LocalTarget(), auto_preflight=False)

            try:
                runner.start()
                ev.activated = True
                ev.session_id = runner._session_id
            except RuntimeError as exc:
                ev.activated = False
                ev.verdict   = "INVALID"
                ev.details   = f"start() failed: {exc}"
                return self._error_fault_record(task, seed, spec, ev, str(exc), pair_id=pair_id)

            # Run agent under the fault
            t0 = time.time()
            try:
                f_result = self.agent.run(task.agent_prompt(), seed=seed)
            except Exception as exc:
                f_result = AgentRunResult(
                    success=0.0, duration_s=time.time()-t0,
                    reported_error=1.0, exception=str(exc),
                    termination_reason="agent_exception",
                )

            try:
                runner.stop()
                ev.recovered = True
            except RuntimeError as exc:
                ev.recovered = False
                ev.details   = f"stop() errors: {exc}"

            # Pull real evidence from CJ DB proxy call log
            if runner._session_id is not None:
                try:
                    sess = runner.db.get_session(runner._session_id)
                    raw_verdict = (
                        str(sess["verdict"]) if sess and "verdict" in sess.keys()
                        else "INCONCLUSIVE"
                    )
                    ev.verdict = raw_verdict

                    # Read proxy-observed LLM calls for the fault phase
                    llm_call_rows = runner.db.get_llm_calls(
                        runner._session_id, phase="fault"
                    )
                    # triggered: at least one request reached the proxy
                    ev.triggered = ev.activated and len(llm_call_rows) > 0
                    # manifested: at least one request was blocked or modified by the fault
                    ev.manifested = any(
                        row.get("was_blocked") or row.get("was_modified")
                        for row in llm_call_rows
                    )
                except Exception as _db_exc:
                    ev.verdict    = "INCONCLUSIVE"
                    # Fall back to llm_calls count from agent self-report
                    ev.triggered  = ev.activated and (f_result.llm_calls > 0)
                    ev.manifested = False
                    ev.details    = f"DB query failed: {_db_exc}"

            f_ok, f_tp, f_tt, _ = score_task(
                task, f_result.generated_code, timeout_s=self.exec_timeout_s
            )

            # Classify validity
            if ev.activated and not ev.triggered:
                validity = "untriggered"
            elif ev.triggered and not ev.manifested:
                # Reached proxy but fault had no effect (e.g. rate limit not yet hit)
                validity = "untriggered"
            else:
                validity = {
                    "VALID":       "valid",
                    "INVALID":     "invalid",
                    "INCONCLUSIVE":"inconclusive",
                }.get(ev.verdict, "inconclusive")

        # ── Detect silent failure ──────────────────────────────────────────────
        # Silent failure: tests fail but agent did not report an error
        silent_failure = (not f_ok) and (f_result.reported_error < 0.5)

        return RunRecord(
            run_id=str(uuid.uuid4()),
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            cj_commit=_CJ_COMMIT,
            agent_system=self.agent.name,
            benchmark=task.benchmark,
            task_id=task.task_id,
            model=self.agent.client.model,
            endpoint_type=_endpoint_type(),
            seed=seed,
            fault_type=self.fault_name,
            fault_parameters=spec["parameters"],
            target="local",
            phase="fault",
            success=1.0 if f_ok else 0.0,
            duration_s=f_result.duration_s,
            reported_error=f_result.reported_error,
            retries=f_result.retries,
            llm_calls=f_result.llm_calls,
            tool_calls=f_result.tool_calls,
            turns=f_result.turns,
            prompt_tokens=f_result.prompt_tokens,
            completion_tokens=f_result.completion_tokens,
            total_tokens=f_result.total_tokens,
            cost_usd=f_result.cost_usd,
            tests_passed=f_tp,
            tests_total=f_tt,
            generated_code_hash=_code_hash(f_result.generated_code),
            termination_reason=f_result.termination_reason,
            exception=f_result.exception,
            lifecycle=ev,
            validity=validity,
            oracle_outcome={"silent_failure": silent_failure},
            pair_id=pair_id,
        )

    def _error_fault_record(
        self, task, seed, spec, ev, exc_str, pair_id: str = ""
    ) -> RunRecord:
        return RunRecord(
            run_id=str(uuid.uuid4()),
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            cj_commit=_CJ_COMMIT,
            agent_system=self.agent.name,
            benchmark=task.benchmark,
            task_id=task.task_id,
            model=self.agent.client.model,
            endpoint_type=_endpoint_type(),
            seed=seed,
            fault_type=self.fault_name,
            fault_parameters=spec["parameters"],
            target="local",
            phase="fault",
            success=0.0,
            duration_s=0.0,
            reported_error=1.0,
            retries=0,
            llm_calls=0,
            tool_calls=0,
            turns=0,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            cost_usd=0.0,
            tests_passed=0,
            tests_total=0,
            generated_code_hash="",
            termination_reason="injection_failed",
            exception=exc_str,
            lifecycle=ev,
            validity="invalid",
            pair_id=pair_id,
        )

    def _append_jsonl(self, record: RunRecord) -> None:
        import json
        path = os.path.join(self.output_dir, "runs.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record.to_dict()) + "\n")
