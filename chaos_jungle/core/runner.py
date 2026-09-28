"""ChaosRunner — orchestrates the fault lifecycle."""

from __future__ import annotations
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, TYPE_CHECKING

from chaos_jungle.core._duration import parse_duration
from chaos_jungle.db.session_db import SessionDB
from chaos_jungle.core.guardrails import apply_guardrails, SafetyPolicy
from chaos_jungle.core.scenario import Scenario
from chaos_jungle.targets.base import Target
from chaos_jungle.targets.local import LocalTarget
from chaos_jungle.targets.logging import LoggingTarget


# ── Resource collection helpers ───────────────────────────────────────────────

def _collect_resources() -> dict:
    """Snapshot CPU, memory, disk I/O and network counters.

    Tries ``psutil`` first; falls back to ``/proc`` files on Linux.
    Returns an empty dict on failure or unsupported platform.
    """
    snap: dict = {}
    try:
        import psutil  # type: ignore[import]
        snap["cpu_pct"]      = psutil.cpu_percent(interval=0.1)
        vm = psutil.virtual_memory()
        snap["mem_pct"]      = vm.percent
        snap["mem_used_mb"]  = round(vm.used / 1_048_576, 1)
        snap["mem_total_mb"] = round(vm.total / 1_048_576, 1)
        try:
            la = psutil.getloadavg()
            snap["load_1"] = la[0]
            snap["load_5"] = la[1]
        except Exception:
            pass
        try:
            dc = psutil.disk_io_counters()
            if dc:
                snap["disk_read_mb"]  = round(dc.read_bytes  / 1_048_576, 2)
                snap["disk_write_mb"] = round(dc.write_bytes / 1_048_576, 2)
        except Exception:
            pass
        try:
            nc = psutil.net_io_counters()
            if nc:
                snap["net_rx_mb"] = round(nc.bytes_recv / 1_048_576, 2)
                snap["net_tx_mb"] = round(nc.bytes_sent / 1_048_576, 2)
        except Exception:
            pass
    except ImportError:
        # psutil not installed — try /proc on Linux
        try:
            with open("/proc/loadavg") as f:
                parts = f.read().split()
                snap["load_1"] = float(parts[0])
                snap["load_5"] = float(parts[1])
        except Exception:
            pass
        try:
            with open("/proc/meminfo") as f:
                info = {}
                for line in f:
                    k, _, v = line.partition(":")
                    info[k.strip()] = int(v.split()[0])
                total = info.get("MemTotal", 0)
                avail = info.get("MemAvailable", 0)
                used  = total - avail
                snap["mem_used_mb"]  = round(used  / 1024, 1)
                snap["mem_total_mb"] = round(total / 1024, 1)
                snap["mem_pct"]      = round(used / total * 100, 1) if total else 0.0
        except Exception:
            pass
    except Exception:
        pass
    return snap

if TYPE_CHECKING:
    from chaos_jungle.analysis.judge import JudgeScore, LLMJudge
    from chaos_jungle.analysis.oracles import Oracle, OracleResult
    from chaos_jungle.metrics.strategy import CollectStrategy
    from chaos_jungle.metrics.metric_set import MetricSet
    from chaos_jungle.metrics.schema import CollectedMetrics
    from chaos_jungle.analysis.hypothesis import Hypothesis, HypothesisResult


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
    collected_metrics: "CollectedMetrics | None" = field(default=None, repr=False)
    llm_calls: list = field(default_factory=list, repr=False)
    hypothesis_result: "HypothesisResult | None" = field(default=None, repr=False)
    # Scientific statistics
    baseline_std: dict = field(default_factory=dict)
    fault_std: dict = field(default_factory=dict)
    baseline_ci95: dict = field(default_factory=dict)
    fault_ci95: dict = field(default_factory=dict)
    # Cohen's d effect size per metric: (mean_fault - mean_baseline) / pooled_std
    effect_size: dict = field(default_factory=dict)
    # Injection validity: None = unchecked; True/False = verified
    injection_valid: "bool | None" = field(default=None)
    injection_valid_reason: str = field(default="")

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
        ]
        if self.injection_valid is not None:
            iv_str = "VALID" if self.injection_valid else "INVALID"
            lines.append(f"Injection: {iv_str}  ({self.injection_valid_reason})")
        lines.append("")
        for k in sorted(set(self.baseline) | set(self.fault)):
            b = self.baseline.get(k, "—")
            f = self.fault.get(k, "—")
            d = self.delta.get(k)
            d_str = f"  Δ {d:+.4g}" if d is not None else ""
            # Append ±CI if available
            b_ci = self.baseline_ci95.get(k)
            f_ci = self.fault_ci95.get(k)
            b_std = self.baseline_std.get(k)
            f_std = self.fault_std.get(k)
            b_str = str(b)
            f_str = str(f)
            if b_ci is not None:
                b_str = f"{b} ±{b_ci:.4g} (σ={b_std:.4g})"
            if f_ci is not None:
                f_str = f"{f} ±{f_ci:.4g} (σ={f_std:.4g})"
            lines.append(f"  {k:<30} baseline={b_str}  fault={f_str}{d_str}")

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

        if self.collected_metrics is not None:
            cm = self.collected_metrics
            lines.append("")
            lines.append(f"  Auto-collected metrics ({cm.strategy}):")
            for name in cm.active_metrics:
                b = cm.baseline.get(name)
                f = cm.fault.get(name)
                d = cm.delta.get(name)
                b_str = f"{b.avg:.4g}" if b else "—"
                f_str = f"{f.avg:.4g}" if f else "—"
                d_str = f"  Δ {d:+.4g}" if d is not None else ""
                lines.append(f"  {name:<30} baseline={b_str}  fault={f_str}{d_str}")
            if cm.recovery:
                lines.append(f"  Recovery samples: {sum(len(v.series) for v in cm.recovery.values())}")

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

        if self.llm_calls:
            lines.append("")
            n = len(self.llm_calls)
            blocked  = sum(1 for c in self.llm_calls if c.get("was_blocked"))
            modified = sum(1 for c in self.llm_calls if c.get("was_modified"))
            lines.append(
                f"  LLM calls captured (n={n}"
                + (f"  blocked={blocked}" if blocked else "")
                + (f"  modified={modified}" if modified else "")
                + "):"
            )
            lines.append(
                f"  {'#':<4} {'model':<20} {'in':>6} {'out':>6} {'tok/s':>6} "
                f"{'cost':>10} {'lat':>7}  {'ttft':>6}  status/finish"
            )
            lines.append("  " + "-" * 84)
            for c in self.llm_calls:
                tps  = f"{c.get('tokens_per_second', 0):.1f}" if c.get("tokens_per_second") else "—"
                ttft = f"{c['ttft_s']:.3f}s" if c.get("ttft_s") is not None else "—"
                tag  = c.get("finish_reason") or f"HTTP {c['http_status']}"
                if c.get("was_blocked"):
                    tag = f"[blocked] {tag}"
                elif c.get("was_modified"):
                    tag = f"[modified] {tag}"
                lines.append(
                    f"  {c['call_index']:<4} {c.get('model',''):<20} "
                    f"{c['prompt_tokens']:>6} {c['completion_tokens']:>6} "
                    f"{tps:>6} ${c['cost_usd']:>9.6f} {c['latency_s']:>6.2f}s  "
                    f"{ttft:>6}  {tag}"
                )
            total_in   = sum(c["prompt_tokens"] for c in self.llm_calls)
            total_out  = sum(c["completion_tokens"] for c in self.llm_calls)
            total_cost = sum(c["cost_usd"] for c in self.llm_calls)
            lats       = [c["latency_s"] for c in self.llm_calls]
            avg_lat    = sum(lats) / n
            sorted_lats = sorted(lats)
            p50 = sorted_lats[int(n * 0.50)]
            p99 = sorted_lats[min(int(n * 0.99), n - 1)]
            ttfts = [c["ttft_s"] for c in self.llm_calls if c.get("ttft_s") is not None]
            lines.append("  " + "-" * 84)
            lines.append(
                f"  {'Total / avg':<25} {total_in:>6} {total_out:>6}        "
                f"${total_cost:>9.6f} {avg_lat:>6.2f}s"
            )
            lines.append(
                f"  Latency  p50={p50:.2f}s  p99={p99:.2f}s"
                + (f"  TTFT avg={sum(ttfts)/len(ttfts):.3f}s" if ttfts else "")
            )

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


def _std_metrics(runs: list[dict]) -> dict:
    """Sample standard deviation for each numeric metric across runs."""
    import math
    if len(runs) < 2:
        return {}
    result: dict[str, float] = {}
    for k in runs[0]:
        vals = [r[k] for r in runs if isinstance(r.get(k), (int, float))]
        if len(vals) >= 2:
            mean = sum(vals) / len(vals)
            variance = sum((x - mean) ** 2 for x in vals) / (len(vals) - 1)
            result[k] = round(math.sqrt(variance), 6)
    return result


# Two-sided t critical values for 95% CI (df = n-1, df >= 1)
_T_TABLE: dict[int, float] = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776,
    5: 2.571,  6: 2.447, 7: 2.365, 8: 2.306,
    9: 2.262, 10: 2.228, 15: 2.131, 20: 2.086,
    30: 2.042, 60: 2.000,
}


def _t_critical(n: int) -> float:
    """Return the two-sided 95% t critical value for *n* observations."""
    import math
    df = n - 1
    if df in _T_TABLE:
        return _T_TABLE[df]
    # For large df, use z=1.96 approximation
    for threshold in sorted(_T_TABLE.keys(), reverse=True):
        if df >= threshold:
            return _T_TABLE[threshold]
    return 12.706  # df=1 fallback


def _confidence_interval_95(std: dict[str, float], n: int) -> dict[str, float]:
    """Return 95% CI half-widths (margin of error) for each metric.

    The CI for the mean is: mean ± t * (std / sqrt(n))
    """
    import math
    if n < 2:
        return {}
    t = _t_critical(n)
    return {k: round(t * s / math.sqrt(n), 6) for k, s in std.items()}


def _cohens_d(
    baseline_runs: list[dict],
    fault_runs: list[dict],
) -> dict[str, float]:
    """Compute Cohen's d effect size for each numeric metric.

    d = (mean_fault - mean_baseline) / pooled_std

    Returns an empty dict when there are fewer than 2 total observations or
    the pooled std is zero (no variance).
    """
    import math
    n_b = len(baseline_runs)
    n_f = len(fault_runs)
    if n_b + n_f < 2:
        return {}
    result: dict[str, float] = {}
    keys = set(baseline_runs[0]) | set(fault_runs[0]) if (baseline_runs and fault_runs) else set()
    for k in keys:
        b_vals = [r[k] for r in baseline_runs if isinstance(r.get(k), (int, float))]
        f_vals = [r[k] for r in fault_runs if isinstance(r.get(k), (int, float))]
        if not b_vals or not f_vals:
            continue
        mean_b = sum(b_vals) / len(b_vals)
        mean_f = sum(f_vals) / len(f_vals)
        var_b = sum((x - mean_b) ** 2 for x in b_vals) / max(len(b_vals) - 1, 1)
        var_f = sum((x - mean_f) ** 2 for x in f_vals) / max(len(f_vals) - 1, 1)
        pooled_var = ((n_b - 1) * var_b + (n_f - 1) * var_f) / max(n_b + n_f - 2, 1)
        pooled_std = math.sqrt(pooled_var)
        if pooled_std == 0:
            continue
        result[k] = round((mean_f - mean_b) / pooled_std, 6)
    return result


def _extract_workload_metrics(runs: list[dict], names: list[str]) -> dict[str, float]:
    """Extract and average named metrics from a list of workload() return dicts."""
    result: dict[str, float] = {}
    for name in names:
        vals = [r[name] for r in runs if isinstance(r.get(name), (int, float))]
        if vals:
            result[name] = round(sum(vals) / len(vals), 6)
    return result


def _target_info(target) -> tuple[str, str]:
    """Return (target_type, target_addr) for a target object."""
    cls = type(target).__name__
    if cls == "HTTPTarget":
        return "http", getattr(target, "url", "")
    if cls == "SSHTarget":
        user = getattr(target, "user", "") or ""
        host = getattr(target, "host", "") or ""
        return "ssh", f"{user}@{host}" if user else host
    # LocalTarget or anything else
    import socket
    try:
        return "local", socket.gethostname()
    except Exception:
        return "local", "localhost"


def _start_shared_llm_proxy(
    faults: list,
    session_id: "int | None",
    db,
) -> "tuple[object | None, str | None, str | None]":
    """Start one shared proxy for all _LLMProxyFault instances in the scenario.

    When multiple LLM proxy faults are present, instead of each spawning its own
    proxy (which would conflict over OPENAI_BASE_URL), we start a single proxy
    with a --fault-chain JSON payload that applies all faults in sequence on
    every request — same process, same port, no inter-proxy TCP overhead.

    Returns (proc, base_url_env, saved_env).
    Returns (None, None, None) when fewer than 2 LLM proxy faults are found.
    """
    import json as _json
    import os as _os
    import subprocess
    import sys

    try:
        from chaos_jungle.faults.llm import _LLMProxyFault, _proxy_script_path
    except ImportError:
        return None, None, None

    llm = [f for f in faults if isinstance(f, _LLMProxyFault)]
    if len(llm) < 2:
        return None, None, None

    chain    = [f._fault_config() for f in llm]
    port     = llm[0].port
    upstream = llm[-1].upstream
    env_var  = llm[0].base_url_env
    script   = _proxy_script_path()

    cmd = [
        sys.executable, script,
        "--port",        str(port),
        "--upstream",    upstream,
        "--fault-chain", _json.dumps(chain),
    ]
    _db_path = getattr(db, "path", None)
    if session_id and _db_path:
        cmd += ["--db-path", _db_path, "--session-id", str(session_id), "--phase", "fault"]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(0.4)
    if proc.poll() is not None:
        out = proc.stdout.read().decode(errors="replace") if proc.stdout else ""
        raise RuntimeError(f"Shared LLM proxy failed to start.\nOutput: {out}")

    saved_env = _os.environ.get(env_var)
    _os.environ[env_var] = f"http://127.0.0.1:{port}/v1"

    for f in llm:
        f._managed_externally = True

    names = " + ".join(f.__class__.__name__ for f in llm)
    print(f"[chaos-jungle] Shared LLM proxy ({names}) on port {port}")
    return proc, env_var, saved_env


class ChaosRunner:
    """Orchestrate the start/stop/revert lifecycle of a chaos scenario.

    Handles all four usage modes:

    * **Decorator** — via :func:`chaos_jungle.inject.decorators.chaos`
    * **Context manager** — via :func:`chaos_jungle.inject.decorators.chaos_session`
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
        monitor_resources: bool = False,
        resource_interval_s: float = 2.0,
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
        self.monitor_resources = monitor_resources
        self.resource_interval_s = resource_interval_s
        self._session_id: int | None = None
        self._fault_ids: list[int] = []
        # Tracks (fault, fault_id) pairs confirmed as activated — used for rollback
        self._activated: list[tuple] = []
        self._stopped: bool = False
        self._timer: threading.Timer | None = None
        self._resource_thread: threading.Thread | None = None
        self._resource_stop: threading.Event = threading.Event()
        self._abort_thread: threading.Thread | None = None
        self._abort_stop: threading.Event = threading.Event()
        self._fault_start_ts: float | None = None
        self._shared_llm_proc = None
        # fid -> "VALID" | "INVALID" | "INCONCLUSIVE" — populated by start()
        self._activation_verdicts: dict[int, str] = {}

        # Auto-register scenario in registry with the correct type/target_ip
        # so the local DB always reflects where the scenario will run.
        try:
            from chaos_jungle.control.registry import ScenarioRegistry
            _reg = ScenarioRegistry(db=self.db)
            if _reg.get(scenario.id) is None:
                _ttype, _taddr = _target_info(self.target)
                _reg.register(scenario, type=_ttype, target_ip=_taddr)
        except Exception:
            pass
        self._shared_llm_env_var: str | None = None
        self._shared_llm_saved_env: str | None = None
        # InjectionGroup runners started alongside this scenario
        self._group_runners: list = []
        self._group_evidence: list = []

    # ── Plan compilation ──────────────────────────────────────────

    def to_plan(
        self,
        duration: "str | int | float | None" = None,
        source: str = "python",
    ) -> "ExperimentPlan":
        """Compile this runner into a canonical :class:`~chaos_jungle.plan.ExperimentPlan`.

        The plan can be saved alongside results for reproducibility::

            plan = runner.to_plan(duration=30)
            plan.save("results/resolved_plan.json")

        Parameters
        ----------
        duration :
            Fault duration (passed through to ``SafetySpec.max_duration_s``).
            Accepts the same formats as :meth:`run`.
        source :
            Authoring interface label for provenance. Use ``"decorator"``
            when called from the decorator interface, ``"context_manager"``
            from a context manager, ``"python"`` otherwise.
        """
        from chaos_jungle.plan import ExperimentPlan
        return ExperimentPlan.from_scenario(
            self.scenario, self.target, duration=duration, source=source
        )

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

        self._stopped = False
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

        _ttype, _taddr = _target_info(self.target)
        if self._session_id is None:
            self._session_id = self.db.open_session(
                self.scenario.name, target_type=_ttype, target_addr=_taddr
            )
            self.db.add_event(self._session_id, f"Session started: {self.scenario.name}")
            try:
                from chaos_jungle.control.registry import ScenarioRegistry
                ScenarioRegistry(db=self.db).set_running(self.scenario.id)
            except Exception:
                pass

        self.db.update_session_status(self._session_id, "preflight")

        # wrap target so every command is logged to the session DB
        logged = LoggingTarget(self.target, self.db, self._session_id)

        if self.auto_preflight:
            for fault in self.scenario.faults:
                fault.preflight(logged, auto_install=self.auto_install)

        self.db.update_session_status(self._session_id, "injecting")

        self._shared_llm_proc, self._shared_llm_env_var, self._shared_llm_saved_env = (
            _start_shared_llm_proxy(self.scenario.faults, self._session_id, self.db)
        )

        self._fault_ids = []
        self._activated = []

        for fault in self.scenario.faults:
            fid = self.db.record_fault(
                self._session_id,
                fault.__class__.__name__,
                fault._parameters(),
            )
            self._fault_ids.append(fid)
            logged.fault_id = fid
            self.db.add_event(
                self._session_id,
                f"Starting fault: {fault.__class__.__name__}",
                fault_id=fid,
            )
            _dry = self.policy is not None and self.policy.dry_run

            if self.monitor_resources:
                try:
                    snap_before = _collect_resources()
                    self.db.update_fault_snapshot(fid, snapshot_before=snap_before)
                except Exception:
                    pass

            try:
                if _dry:
                    fault.dry_run(logged)
                else:
                    print(f"[chaos-jungle] Injecting {fault.__class__.__name__}({fault._parameters()})")
                    fault.start(logged)
            except Exception as inject_exc:
                # Record this fault as failed before rolling back
                self.db.update_fault_status(fid, "injection_failed")
                self.db.add_event(
                    self._session_id,
                    f"ERROR injecting {fault.__class__.__name__}: {inject_exc}",
                    fault_id=fid,
                )
                # Roll back all previously activated faults in reverse order
                self._rollback(logged)
                raise

            # Add to _activated IMMEDIATELY so rollback covers it if verify fails
            self._activated.append((fault, fid))

            # Verify activation — exceptions are never swallowed
            if not _dry:
                try:
                    vr = fault.verify_active(logged)
                except Exception as vexc:
                    self.db.update_fault_status(fid, "injection_failed")
                    self.db.add_event(
                        self._session_id,
                        f"VERIFY ERROR {fault.__class__.__name__}: {vexc}",
                        fault_id=fid,
                    )
                    self._rollback(logged)
                    raise RuntimeError(
                        f"verify_active raised for {fault.__class__.__name__}: {vexc}"
                    ) from vexc

                # Store verification result
                self.db.update_fault_verification(
                    fid, verified_active=vr.verified, note=vr.reason
                )
                self.db.update_fault_snapshot(
                    fid,
                    injection_verified=vr.verified,
                    verification_output=vr.reason,
                )

                if vr.not_implemented:
                    # No override — warn but continue; not a real failure
                    self._activation_verdicts[fid] = "INCONCLUSIVE"
                    print(
                        f"[chaos-jungle] WARNING: {fault.__class__.__name__} "
                        f"has no verify_active() — cannot confirm injection"
                    )
                    self.db.add_event(
                        self._session_id,
                        f"WARN: {fault.__class__.__name__} verify_active not implemented",
                        fault_id=fid,
                    )
                elif not vr.verified:
                    # Real check ran and reported fault is not active — roll back
                    self._activation_verdicts[fid] = "INVALID"
                    self.db.update_fault_status(fid, "injection_failed")
                    self.db.add_event(
                        self._session_id,
                        f"VERIFY FAILED {fault.__class__.__name__}: {vr.reason}",
                        fault_id=fid,
                    )
                    self._rollback(logged)
                    raise RuntimeError(
                        f"verify_active failed for {fault.__class__.__name__}: {vr.reason}"
                    )
                else:
                    # verified=True, not_implemented=False → injection confirmed
                    self._activation_verdicts[fid] = "VALID"

            self.db.update_fault_status(fid, "active")

            if self.monitor_resources:
                try:
                    snap_after = _collect_resources()
                    self.db.update_fault_snapshot(fid, snapshot_after=snap_after)
                except Exception:
                    pass

            self.db.add_event(
                self._session_id,
                f"Fault started: {fault.__class__.__name__}",
                fault_id=fid,
            )

        # Record fault start time and start continuous resource monitoring (optional)
        self._fault_start_ts = time.time()
        if self.monitor_resources and self._fault_ids:
            self._resource_stop.clear()
            _fid = self._fault_ids[0]
            _sid = self._session_id
            _interval = self.resource_interval_s
            _t0 = self._fault_start_ts

            def _monitor_loop() -> None:
                while not self._resource_stop.wait(timeout=_interval):
                    try:
                        snap = _collect_resources()
                        elapsed = round(time.time() - _t0, 2)
                        self.db.add_resource_sample(
                            _sid, elapsed,
                            fault_id=_fid, phase="fault",
                            **snap,
                        )
                    except Exception:
                        pass

            self._resource_thread = threading.Thread(
                target=_monitor_loop, daemon=True, name="cj-resource-monitor"
            )
            self._resource_thread.start()

        # Start injection groups (multi-target coordinated injection)
        self._group_runners = []
        for group in getattr(self.scenario, "groups", []):
            from chaos_jungle.inject.group import InjectionGroupRunner as _IGR
            gr = _IGR(group)
            try:
                print(f"[chaos-jungle] Starting InjectionGroup {group.name!r}  "
                      f"(mode={group.synchronization}  members={len(group.injections)})")
                gr.start()
                self._group_runners.append(gr)
            except RuntimeError as _grp_exc:
                for _started in self._group_runners:
                    try:
                        _started.stop()
                    except Exception:
                        pass
                self._rollback(logged)
                raise RuntimeError(
                    f"InjectionGroup {group.name!r} failed to activate: {_grp_exc}"
                ) from _grp_exc

        self.db.update_session_status(self._session_id, "active")
        print(f"[chaos-jungle] Chaos ON  — scenario '{self.scenario.name}'  "
              f"(session id: {self._session_id})")

        # Safety policy monitoring — always start when a policy is present so that
        # emergency_stop() and all threshold checks work even when no specific
        # threshold was set at construction time.
        if self.policy is not None:
            self._abort_stop.clear()
            _policy = self.policy
            _interval = _policy.monitor_interval_s
            _t0 = self._fault_start_ts or time.time()

            _consecutive_violations = 0

            def _abort_loop() -> None:
                nonlocal _consecutive_violations
                from chaos_jungle.core.guardrails import AbortError as _AbortError

                while not self._abort_stop.wait(timeout=_interval):
                    try:
                        # Pass real live metrics so every threshold is checked.
                        _er = _cost = _p99 = None
                        _retries: "int | None" = None
                        if self._session_id is not None and (
                            _policy.max_error_rate is not None
                            or _policy.max_cost_usd is not None
                            or _policy.max_latency_p99_s is not None
                            or _policy.max_retries is not None
                        ):
                            try:
                                rows = self.db._conn.execute(
                                    "SELECT http_status, cost_usd, latency_s, is_retry "
                                    "FROM llm_calls WHERE session_id = ?",
                                    (self._session_id,),
                                ).fetchall()
                                if rows:
                                    n = len(rows)
                                    _er = sum(
                                        1 for r in rows if (r[0] or 0) >= 400
                                    ) / n
                                    _cost = sum(r[1] or 0.0 for r in rows)
                                    _lats = sorted(r[2] or 0.0 for r in rows)
                                    _p99 = _lats[min(int(n * 0.99), n - 1)]
                                    _retries = sum(r[3] or 0 for r in rows)
                            except Exception:
                                pass
                        _policy.check_abort(
                            elapsed_s=time.time() - _t0,
                            error_rate=_er,
                            cost_usd=_cost,
                            latency_p99_s=_p99,
                            retries=_retries,
                        )
                        # Successful check — reset consecutive violation counter.
                        _consecutive_violations = 0
                    except _AbortError as _exc:
                        # Emergency stop bypasses the consecutive-violation gate;
                        # metric threshold violations require violation_threshold
                        # consecutive ticks before triggering.
                        _is_emergency = _policy._emergency_stop.is_set()
                        if _is_emergency:
                            _consecutive_violations = _policy.violation_threshold
                        else:
                            _consecutive_violations += 1

                        if _consecutive_violations < _policy.violation_threshold:
                            continue  # not enough consecutive violations yet

                        print(f"[chaos-jungle] ABORT: {_exc} — stopping chaos")
                        if self._session_id is not None:
                            try:
                                self.db.add_event(
                                    self._session_id,
                                    f"ABORT: {_exc}",
                                )
                            except Exception:
                                pass
                        if _policy.abort_callback is not None:
                            try:
                                _policy.abort_callback(str(_exc))
                            except Exception:
                                pass
                        # Launch stop() in a new thread to avoid self-join:
                        # _abort_loop → stop() → _stop_shared_resources()
                        # → abort_thread.join() would deadlock.
                        threading.Thread(
                            target=self.stop,
                            kwargs={"_status_override": "aborted"},
                            daemon=True,
                            name="cj-abort-stop",
                        ).start()
                        return
                    except Exception:
                        return  # unexpected error — exit abort loop gracefully

            self._abort_thread = threading.Thread(
                target=_abort_loop, daemon=True, name="cj-abort-monitor"
            )
            self._abort_thread.start()

        if duration is not None:
            seconds = parse_duration(duration)
            self._timer = threading.Timer(seconds, self._auto_stop)
            self._timer.daemon = True
            self._timer.start()
            print(f"[chaos-jungle] Chaos ON — auto-stop in {duration} ({seconds:.0f}s)")

        return self

    @staticmethod
    def _has_abort_conditions(policy: "SafetyPolicy") -> bool:
        """Return True if the policy has any active runtime abort conditions."""
        return any([
            policy.max_duration_s is not None,
            policy.max_error_rate is not None,
            policy.max_cost_usd is not None,
            policy.max_latency_p99_s is not None,
            policy.max_retries is not None,
            policy._emergency_stop.is_set(),
        ])

    def _rollback(self, logged) -> None:
        """Revert all confirmed-activated faults in reverse order.

        Called internally when fault injection fails mid-scenario.
        Records each revert attempt regardless of individual failures.
        """
        cleanup_errors = []
        for fault, fid in reversed(self._activated):
            try:
                logged.fault_id = fid
                self.db.update_fault_status(fid, "stopping")
                self.db.add_event(
                    self._session_id,
                    f"Rollback: reverting {fault.__class__.__name__}",
                    fault_id=fid,
                )
                fault.stop(logged)
                fault.revert(logged)
                self.db.update_fault_status(fid, "reverted")
                self.db.close_fault(fid)
                self.db.add_event(
                    self._session_id,
                    f"Rollback: {fault.__class__.__name__} reverted",
                    fault_id=fid,
                )
                print(f"[chaos-jungle] Rollback: reverted {fault.__class__.__name__}")
            except Exception as exc:
                cleanup_errors.append(exc)
                self.db.update_fault_status(fid, "revert_failed")
                self.db.add_event(
                    self._session_id,
                    f"Rollback ERROR reverting {fault.__class__.__name__}: {exc}",
                    fault_id=fid,
                )
                print(f"[chaos-jungle] Rollback ERROR: {fault.__class__.__name__}: {exc}")

        self._activated = []
        self._stop_shared_resources()
        final_status = "partially_reverted" if cleanup_errors else "injection_failed"
        self.db.close_session(self._session_id, status=final_status)
        self.target.disconnect()

    def _stop_shared_resources(self) -> None:
        """Stop the shared LLM proxy and resource monitoring thread. Idempotent."""
        if self._shared_llm_proc is not None:
            import os as _os, subprocess as _sp
            if self._shared_llm_env_var:
                if self._shared_llm_saved_env is None:
                    _os.environ.pop(self._shared_llm_env_var, None)
                else:
                    _os.environ[self._shared_llm_env_var] = self._shared_llm_saved_env
            if self._shared_llm_proc.poll() is None:
                self._shared_llm_proc.terminate()
                try:
                    self._shared_llm_proc.wait(timeout=5)
                except _sp.TimeoutExpired:
                    self._shared_llm_proc.kill()
            self._shared_llm_proc = None
            self._shared_llm_env_var = None
            self._shared_llm_saved_env = None

        if self._resource_thread is not None:
            self._resource_stop.set()
            self._resource_thread.join(timeout=5)
            self._resource_thread = None

        if self._abort_thread is not None:
            self._abort_stop.set()
            self._abort_thread.join(timeout=2)
            self._abort_thread = None

    def _auto_stop(self) -> None:
        """Called by the background timer when duration expires."""
        print(f"[chaos-jungle] Duration reached — auto-stopping chaos")
        try:
            self.stop()
        except Exception as exc:
            print(f"[chaos-jungle] ERROR during auto-stop: {exc}")

    def stop(self, *, _status_override: str | None = None) -> None:
        """Stop and revert all active faults in the scenario.

        Idempotent — safe to call more than once. Only faults confirmed as
        activated are reverted. Faults that failed to activate are skipped.
        The session is marked ``reverted`` only when every activated fault
        passes cleanup. Otherwise it is marked ``revert_failed`` or
        ``partially_reverted``.

        Parameters
        ----------
        _status_override : str or None
            Internal — allows the abort path to force ``aborted`` status.
        """
        if self._stopped:
            return
        self._stopped = True

        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

        if self._session_id is None:
            raise RuntimeError("No active session — call start() first or use attach()")

        self.db.update_session_status(self._session_id, "stopping")
        logged = LoggingTarget(self.target, self.db, self._session_id)
        errors: list[Exception] = []
        reverted_count = 0

        # ── Revert injection groups (reverse order, before individual faults) ──
        for gr in reversed(self._group_runners):
            try:
                ev = gr.stop()
                self._group_evidence.append(ev)
                try:
                    self.db.store_group_evidence(self._session_id, ev)
                except Exception as dexc:
                    print(f"[chaos-jungle] WARNING: could not persist group evidence: {dexc}")
            except Exception as exc:
                errors.append(exc)
                print(f"[chaos-jungle] ERROR reverting InjectionGroup: {exc}")
        self._group_runners = []

        # ── Phase 1: stop + revert all faults ────────────────────────────────
        # Collect faults that completed phase 1 successfully for phase 2.
        phase1_done: list[tuple] = []
        for fault, fid in reversed(self._activated):
            try:
                logged.fault_id = fid
                self.db.update_fault_status(fid, "stopping")
                self.db.add_event(
                    self._session_id,
                    f"Stopping fault: {fault.__class__.__name__}",
                    fault_id=fid,
                )
                fault.stop(logged)
                fault.revert(logged)
                phase1_done.append((fault, fid))
            except Exception as exc:
                errors.append(exc)
                self.db.update_fault_status(fid, "revert_failed")
                self.db.add_event(
                    self._session_id,
                    f"ERROR stopping {fault.__class__.__name__}: {exc}",
                    fault_id=fid,
                )
                print(f"[chaos-jungle] ERROR reverting {fault.__class__.__name__}: {exc}")

        self._activated = []

        # ── Between phases: kill shared resources BEFORE verify_recovered ─────
        # This ensures verify_recovered() sees a clean environment
        # (env vars restored, proxy terminated) rather than the live proxy.
        self._stop_shared_resources()

        # ── Phase 2: verify recovery ──────────────────────────────────────────
        recovery_verdicts: dict[int, str] = {}
        for fault, fid in phase1_done:
            try:
                vr = fault.verify_recovered(logged)
            except Exception as vexc:
                errors.append(vexc)
                self.db.update_fault_status(fid, "revert_failed")
                self.db.add_event(
                    self._session_id,
                    f"RECOVER VERIFY ERROR {fault.__class__.__name__}: {vexc}",
                    fault_id=fid,
                )
                print(
                    f"[chaos-jungle] RECOVER VERIFY ERROR "
                    f"{fault.__class__.__name__}: {vexc}"
                )
                recovery_verdicts[fid] = "INVALID"
                continue

            self.db.update_fault_verification(
                fid, verified_recovered=vr.verified, note=vr.reason
            )

            if vr.not_implemented:
                recovery_verdicts[fid] = "INCONCLUSIVE"
                print(
                    f"[chaos-jungle] WARNING: {fault.__class__.__name__} "
                    f"has no verify_recovered() — cannot confirm cleanup"
                )
            elif not vr.verified:
                recovery_verdicts[fid] = "INVALID"
                errors.append(RuntimeError(
                    f"verify_recovered failed for {fault.__class__.__name__}: {vr.reason}"
                ))
                self.db.update_fault_status(fid, "revert_failed")
                self.db.add_event(
                    self._session_id,
                    f"RECOVER VERIFY FAILED {fault.__class__.__name__}: {vr.reason}",
                    fault_id=fid,
                )
                print(
                    f"[chaos-jungle] RECOVER VERIFY FAILED "
                    f"{fault.__class__.__name__}: {vr.reason}"
                )
                self.db.close_fault(fid)
                continue  # do NOT count as reverted — recovery verification failed
            else:
                recovery_verdicts[fid] = "VALID"

            self.db.update_fault_status(fid, "reverted")
            self.db.close_fault(fid)
            self.db.add_event(
                self._session_id,
                f"Fault stopped and reverted: {fault.__class__.__name__}",
                fault_id=fid,
            )
            print(f"[chaos-jungle] Reverted {fault.__class__.__name__}")
            reverted_count += 1

        # ── Compute and store session verdict ─────────────────────────────────
        _all_verdicts = (
            list(self._activation_verdicts.values()) + list(recovery_verdicts.values())
        )
        if _all_verdicts:
            if "INVALID" in _all_verdicts:
                _session_verdict = "INVALID"
            elif "INCONCLUSIVE" in _all_verdicts:
                _session_verdict = "INCONCLUSIVE"
            else:
                _session_verdict = "VALID"
            try:
                self.db.set_session_verdict(self._session_id, _session_verdict)
                self.db.add_event(
                    self._session_id,
                    f"Session verdict: {_session_verdict}",
                )
            except Exception:
                pass

        try:
            self.db.compute_and_store_impact(self._session_id)
        except Exception:
            pass

        if _status_override:
            final_status = _status_override
        elif errors and reverted_count > 0:
            final_status = "partially_reverted"
        elif errors:
            final_status = "revert_failed"
        else:
            final_status = "reverted"

        self.db.close_session(self._session_id, status=final_status)
        self.db.add_event(self._session_id, f"Session closed ({final_status})")
        try:
            from chaos_jungle.control.registry import ScenarioRegistry
            ScenarioRegistry(db=self.db).set_done(
                self.scenario.id, session_id=self._session_id
            )
        except Exception:
            pass
        try:
            self.target.disconnect()
        except Exception:
            pass
        print(f"[chaos-jungle] Chaos OFF — session {self._session_id} {final_status}.")

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
        strategy: "CollectStrategy | None" = None,
        metric_set: "MetricSet | None" = None,
        on_session_start: "Callable[[int], None] | None" = None,
        on_fault_start: "Callable[[int], None] | None" = None,
        cooldown_s: float = 0.0,
        hypothesis: "Hypothesis | None" = None,
        n_warmup: int = 0,
        randomize_order: bool = False,
        seed: "int | None" = None,
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
        :class:`~chaos_jungle.analysis.judge.LLMJudge` as *evaluator*::

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
            An :class:`~chaos_jungle.analysis.judge.LLMJudge` instance. When provided,
            each workload result that contains ``"question"``, ``"context"``,
            and ``"response"`` keys is scored for faithfulness, hallucination,
            and coherence. Scores are averaged and included in
            :class:`MeasurementResult`.
        oracles : list[Oracle], optional
            Oracle assertion instances to run against the fault runs. Each
            oracle inspects the raw fault workload results and returns a
            pass/fail :class:`~chaos_jungle.analysis.oracles.OracleResult`.  Results
            are stored in :attr:`MeasurementResult.oracle_results` and shown
            in :meth:`MeasurementResult.summary`::

                from chaos_jungle.analysis.oracles import NoPIILeakage, MaxCost
                result = runner.measure(
                    workload, n_fault=3,
                    oracles=[NoPIILeakage(), MaxCost(max_usd=0.05)],
                )
                if not result.passed_oracles():
                    raise AssertionError("Oracle failure")

        strategy : CollectStrategy, optional
            Controls *when* metrics are sampled. Use
            :attr:`~chaos_jungle.metrics.CollectStrategy.SNAPSHOT` (3 fixed
            points: before / during / after fault) or
            :attr:`~chaos_jungle.metrics.CollectStrategy.RECOVERY` (same +
            a post-fault time-series window). When ``None`` (default), no
            automatic metric collection is performed.
        metric_set : MetricSet, optional
            Controls *which* of the fault's ``default_metrics`` are collected.
            Defaults to :attr:`~chaos_jungle.metrics.MetricSet.DEFAULT` (all
            fault defaults) when *strategy* is given. Ignored when *strategy*
            is ``None``.

        Returns
        -------
        MeasurementResult
            Contains averaged baseline/fault metrics, their delta,
            optionally LLM quality scores when *evaluator* is provided,
            oracle assertion results when *oracles* is provided, and
            auto-collected metrics in ``result.collected_metrics`` when
            *strategy* is given.
        """
        from chaos_jungle.analysis.judge import average_scores  # lazy import

        # ── Resolve active metrics (if strategy provided) ──────────
        _active: list[str] = []
        _active_system: list[str] = []
        _active_workload: list[str] = []
        if strategy is not None:
            from chaos_jungle.metrics.metric_set import MetricSet as _MS
            from chaos_jungle.metrics.strategy import _SYSTEM_CMDS
            _ms = metric_set if metric_set is not None else _MS.DEFAULT
            _all_defaults = [
                m for fault in self.scenario.faults for m in fault.default_metrics
            ]
            _active = _ms.resolve(_all_defaults)
            _active_system   = [m for m in _active if m in _SYSTEM_CMDS]
            _active_workload = [m for m in _active if m not in _SYSTEM_CMDS]

        # Pre-create DB session so baseline LLM calls are tracked
        _ttype, _taddr = _target_info(self.target)
        self._session_id = self.db.open_session(
            self.scenario.name, target_type=_ttype, target_addr=_taddr
        )
        self.db.add_event(self._session_id, f"Session started: {self.scenario.name}")
        if on_session_start is not None:
            try:
                on_session_start(self._session_id)
            except Exception:
                pass

        # Decide execution order: baseline-first (default) or fault-first
        import random as _random
        _rng = _random.Random(seed)
        _fault_first = randomize_order and _rng.random() < 0.5
        if _fault_first:
            print("[chaos-jungle] Randomized order: fault phase runs first")

        raw_baseline: list[dict] = []
        raw_fault: list[dict] = []
        _b_sample = None
        _f_sample = None

        def _collect_baseline() -> None:
            nonlocal _b_sample
            if n_warmup > 0:
                print(f"[chaos-jungle] Baseline warm-up ({n_warmup} run(s), discarded) ...")
                for _ in range(n_warmup):
                    workload()
            print(f"[chaos-jungle] Measuring baseline ({n_baseline} trial(s)) ...")
            for _ in range(n_baseline):
                raw_baseline.append(workload())
            if strategy is not None:
                from chaos_jungle.metrics.strategy import collect_system_snapshot
                from chaos_jungle.metrics.schema import MetricSample as _MS2
                _b_sys = collect_system_snapshot(self.target, _active_system)
                _b_wl  = _extract_workload_metrics(raw_baseline, _active_workload)
                _b_sample = _MS2(
                    timestamp_s=time.time(),
                    phase="baseline",
                    trial=0,
                    values={**_b_sys, **_b_wl},
                )

        def _collect_fault() -> None:
            nonlocal _f_sample
            print(f"[chaos-jungle] Measuring under fault ({n_fault} trial(s)) ...")
            self.start()
            if on_fault_start is not None and self._session_id is not None:
                try:
                    on_fault_start(self._session_id)
                except Exception:
                    pass
            try:
                if n_warmup > 0:
                    print(f"[chaos-jungle] Fault warm-up ({n_warmup} run(s), discarded) ...")
                    for _ in range(n_warmup):
                        workload()
                for _ in range(n_fault):
                    raw_fault.append(workload())
                if strategy is not None:
                    from chaos_jungle.metrics.strategy import collect_system_snapshot
                    from chaos_jungle.metrics.schema import MetricSample as _MS2
                    _f_sys = collect_system_snapshot(self.target, _active_system)
                    _f_wl  = _extract_workload_metrics(raw_fault, _active_workload)
                    _f_sample = _MS2(
                        timestamp_s=time.time(),
                        phase="fault",
                        trial=0,
                        values={**_f_sys, **_f_wl},
                    )
            finally:
                self.stop()

        # ── 1 & 2. Run baseline and fault phases in chosen order ──────────────
        if _fault_first:
            _collect_fault()
            if cooldown_s > 0:
                print(f"[chaos-jungle] Cooldown — waiting {cooldown_s:.1f}s ...")
                time.sleep(cooldown_s)
            _collect_baseline()
        else:
            _collect_baseline()
            if cooldown_s > 0:
                print(f"[chaos-jungle] Cooldown — waiting {cooldown_s:.1f}s before fault phase ...")
                time.sleep(cooldown_s)
            _collect_fault()

        baseline = _avg_metrics(raw_baseline)
        fault = _avg_metrics(raw_fault)

        # ── 2b. Post-stop metric collection ───────────────────────
        _r_sample = None
        _recovery_samples: list = []
        if strategy is not None:
            from chaos_jungle.metrics.strategy import (
                collect_system_snapshot,
                collect_recovery_samples,
            )
            from chaos_jungle.metrics.schema import MetricSample as _MS2
            # LocalTarget.connect() is a no-op; SSH targets reconnect cleanly
            try:
                self.target.connect()
                _r_sys = collect_system_snapshot(self.target, _active_system)
                if _r_sys:
                    _r_sample = _MS2(
                        timestamp_s=time.time(),
                        phase="recovery",
                        trial=0,
                        values=_r_sys,
                    )
                if strategy.mode == "recovery" and _active_system:
                    print(
                        f"[chaos-jungle] Collecting recovery metrics "
                        f"({strategy.recovery_window_s:.0f}s window) ..."
                    )
                    _recovery_samples = collect_recovery_samples(
                        self.target,
                        _active_system,
                        window_s=strategy.recovery_window_s,
                        interval_s=strategy.recovery_interval_s,
                    )
            except Exception:
                pass
            finally:
                try:
                    self.target.disconnect()
                except Exception:
                    pass

        # ── 3. Delta + statistics ─────────────────────────────────
        delta = {
            k: round(fault[k] - baseline[k], 6)
            for k in baseline
            if k in fault and isinstance(fault.get(k), (int, float))
                         and isinstance(baseline.get(k), (int, float))
        }

        baseline_std  = _std_metrics(raw_baseline)
        fault_std     = _std_metrics(raw_fault)
        baseline_ci95 = _confidence_interval_95(baseline_std, n_baseline)
        fault_ci95    = _confidence_interval_95(fault_std, n_fault)
        effect_size   = _cohens_d(raw_baseline, raw_fault)

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
            from chaos_jungle.analysis.oracles import run_oracles
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

        # ── 5b. Build CollectedMetrics (if strategy was used) ─────
        collected_metrics = None
        if strategy is not None and _active:
            from chaos_jungle.metrics.schema import CollectedMetrics as _CM
            _b_samples = [_b_sample] if _b_sample else []
            _f_samples = [_f_sample] if _f_sample else []
            # SNAPSHOT recovery = single snapshot; RECOVERY = full window
            if strategy.mode == "recovery":
                _rec_samples = _recovery_samples
            else:
                _rec_samples = [_r_sample] if _r_sample else []
            collected_metrics = _CM.build(
                strategy=strategy.mode,
                active_metrics=_active,
                baseline_samples=_b_samples,
                fault_samples=_f_samples,
                recovery_samples=_rec_samples,
            )

        # ── 5c. Fetch captured LLM calls from DB ──────────────────
        _llm_calls: list = []
        if self._session_id is not None:
            try:
                _llm_calls = self.db.get_llm_calls(self._session_id)
            except Exception:
                pass

        # ── 5d. Hypothesis check (optional) ───────────────────────
        hypothesis_result = None
        if hypothesis is not None:
            hypothesis_result = hypothesis.check(
                MeasurementResult(
                    scenario=self.scenario.name,
                    session_id=self._session_id or 0,
                    baseline=baseline,
                    fault=fault,
                    delta=delta,
                    n_baseline=n_baseline,
                    n_fault=n_fault,
                )
            )
            status = "PASS" if hypothesis_result.passed else "FAIL"
            print(f"[chaos-jungle] Hypothesis [{status}]: {hypothesis.name}")
            if not hypothesis_result.passed:
                for a in hypothesis_result.assertions:
                    if not a.passed:
                        print(f"[chaos-jungle]   FAIL {a.metric}: {a.reason}")

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
            collected_metrics=collected_metrics,
            llm_calls=_llm_calls,
            hypothesis_result=hypothesis_result,
            baseline_std=baseline_std,
            fault_std=fault_std,
            baseline_ci95=baseline_ci95,
            fault_ci95=fault_ci95,
            effect_size=effect_size,
        )

        # ── 6. Persist to DB ──────────────────────────────────────
        db_result: dict = {"baseline": baseline, "fault": fault, "delta": delta}
        if judge_delta:
            db_result["judge_delta"] = judge_delta
            if judge_fault_score:
                db_result["judge_fault"] = judge_fault_score.to_dict()
            if judge_baseline_score:
                db_result["judge_baseline"] = judge_baseline_score.to_dict()
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

        # ── 7. Propagate session verdict → MeasurementResult ──────────────────
        # The verdict (VALID / INCONCLUSIVE / INVALID) is written to the DB by
        # stop() which ran inside _collect_fault().  Read it back here so callers
        # get a single source of truth instead of checking the DB separately.
        if self._session_id is not None:
            try:
                _sess = self.db.get_session(self._session_id)
                if _sess:
                    _verdict = str(_sess["verdict"]) if "verdict" in _sess.keys() else "INCONCLUSIVE"
                    if _verdict == "VALID":
                        result.injection_valid = True
                    elif _verdict == "INVALID":
                        result.injection_valid = False
                    else:  # INCONCLUSIVE — injection could not be verified either way
                        result.injection_valid = None
                    result.injection_valid_reason = f"session verdict: {_verdict}"
                    if _verdict != "VALID":
                        # Effect size is unreliable when injection is not confirmed.
                        result.effect_size = {}
            except Exception:
                pass

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

            from chaos_jungle.inject.intercept import door, Latency

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
