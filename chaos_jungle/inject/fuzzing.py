"""ChaosFuzzer — random fault exploration for chaos-jungle.

Follows the same pattern as ChaosRunner:

    fuzzer = ChaosFuzzer(fault_pool=[...], target=target)
    # or
    fuzzer = ChaosFuzzer(categories=["llm", "system"], target=target)

    results = fuzzer.measure(workload, n_baseline=3, n_fault=3, n=10)

    for r in results:
        print(r.summary())

Two modes
---------
**explicit pool** — user provides the faults (same as legacy fuzz_scenarios):

    fuzzer = ChaosFuzzer(
        fault_pool=[LLMLatency(3.0), LLMRateLimit(n=2), LLMUnavailable()],
        target=LocalTarget(),
    )

**category-based** — CJ picks faults and randomizes their parameters:

    fuzzer = ChaosFuzzer(
        categories=["llm", "system"],
        target=LocalTarget(),
        seed=42,       # reproducible; omit for truly random
    )

Shared baseline
---------------
A single baseline is measured once before any fault experiments start.
Every MeasurementResult in the returned list carries that same baseline,
making all fault comparisons consistent.

Backward compatibility
----------------------
``fuzz_scenarios()`` and ``summarise_fuzz()`` are thin wrappers that delegate
to ``ChaosFuzzer`` so existing code continues to work unchanged.
"""

from __future__ import annotations

import random
from typing import Callable

from chaos_jungle.faults.base import Fault
from chaos_jungle.core.runner import ChaosRunner, MeasurementResult, _avg_metrics
from chaos_jungle.core.scenario import Scenario
from chaos_jungle.targets.base import Target
from chaos_jungle.targets.local import LocalTarget


# ── Category registry ─────────────────────────────────────────────────────────

def _category_registry() -> "dict[str, list[tuple[type, Callable[[random.Random], dict]]]]":
    """Return the built-in fault category registry.

    Imported lazily to avoid circular imports and to skip categories whose
    faults are not installed.
    """
    registry: dict[str, list] = {}

    # ── system ────────────────────────────────────────────────────────────
    system: list = []
    try:
        from chaos_jungle.faults.network import NetworkDelay, NetworkLoss, NetworkCorrupt
        system += [
            (NetworkDelay,   lambda rng: {
                "delay":  f"{rng.randint(50, 2000)}ms",
                "jitter": f"{rng.randint(0, 50)}ms",
            }),
            (NetworkLoss,    lambda rng: {"rate": f"{round(rng.uniform(1, 20), 1)}%"}),
            (NetworkCorrupt, lambda rng: {"rate": f"{round(rng.uniform(1, 10), 1)}%"}),
        ]
    except Exception:
        pass
    try:
        from chaos_jungle.faults.resources import CPUStress, MemoryStress
        system += [
            (CPUStress,    lambda rng: {"cores": rng.randint(1, 2)}),
            (MemoryStress, lambda rng: {"mb":    rng.randint(128, 512)}),
        ]
    except Exception:
        pass
    if system:
        registry["system"] = system

    # ── llm ───────────────────────────────────────────────────────────────
    llm: list = []
    try:
        from chaos_jungle.faults.llm import (
            LLMLatency, LLMRateLimit, LLMTimeout,
            LLMResponseCorrupt, LLMUnavailable,
        )
        llm += [
            (LLMLatency,         lambda rng: {"delay_s": round(rng.uniform(0.5, 5.0), 1)}),
            (LLMRateLimit,       lambda rng: {"n": rng.randint(1, 10)}),
            (LLMTimeout,         lambda rng: {}),
            (LLMResponseCorrupt, lambda rng: {}),
            (LLMUnavailable,     lambda rng: {}),
        ]
    except Exception:
        pass
    if llm:
        registry["llm"] = llm

    # ── application ───────────────────────────────────────────────────────
    application: list = []
    try:
        from chaos_jungle.faults.skill_file import (
            SkillFileUnavailable, SkillFileBadOutput, SkillFileVersionSkew,
        )
        application += [
            (SkillFileUnavailable, lambda rng: {}),
            (SkillFileBadOutput,   lambda rng: {}),
            (SkillFileVersionSkew, lambda rng: {}),
        ]
    except Exception:
        pass
    if application:
        registry["application"] = application

    return registry


# ── ChaosFuzzer ───────────────────────────────────────────────────────────────

class ChaosFuzzer:
    """Random fault exploration — same pattern as ChaosRunner.

    Construct with a fault source and target, then call ``.measure()``.

    Parameters
    ----------
    fault_pool : list[Fault], optional
        Explicit pool of fault instances to draw from.  CJ randomly picks
        subsets but the faults themselves are fixed (explicit mode).
    categories : list[str], optional
        Category names to draw from the built-in registry.  CJ picks
        faults AND randomizes their parameters (category mode).
        Available: ``"system"``, ``"llm"``, ``"application"``.
    target : Target, optional
        Where to run the faults.  Defaults to ``LocalTarget()``.
    seed : int or None, optional
        Random seed for reproducibility.  ``None`` (default) = random.
    max_faults_per_run : int, optional
        Maximum number of faults active simultaneously per experiment.
        Default ``2``.
    exclude : list[str], optional
        Fault class names to skip (e.g. ``["DiskFull", "ProcessKill"]``).
    """

    def __init__(
        self,
        fault_pool: "list[Fault] | None" = None,
        categories: "list[str] | None" = None,
        target: "Target | None" = None,
        seed: "int | None" = None,
        max_faults_per_run: int = 2,
        exclude: "list[str] | None" = None,
    ) -> None:
        if fault_pool is None and categories is None:
            raise ValueError(
                "ChaosFuzzer requires either fault_pool= or categories=."
            )
        if fault_pool is not None and categories is not None:
            raise ValueError(
                "Provide either fault_pool= or categories=, not both."
            )
        self.fault_pool         = fault_pool
        self.categories         = list(categories or [])
        self.target             = target or LocalTarget()
        self.max_faults_per_run = max(1, max_faults_per_run)
        self.exclude: set[str]  = set(exclude or [])
        self._rng               = random.Random(seed)

    # ── Pool building ─────────────────────────────────────────────────────

    def _build_pool(self) -> "list[Fault]":
        """Return the fault pool for this run."""
        if self.fault_pool is not None:
            return [f for f in self.fault_pool if type(f).__name__ not in self.exclude]

        reg = _category_registry()
        pool: list[Fault] = []
        for cat in self.categories:
            if cat not in reg:
                available = list(reg)
                raise ValueError(
                    f"Unknown category {cat!r}. Available: {available}"
                )
            for fault_cls, param_fn in reg[cat]:
                if fault_cls.__name__ in self.exclude:
                    continue
                try:
                    pool.append(fault_cls(**param_fn(self._rng)))
                except Exception:
                    pass  # skip faults that fail to instantiate
        return pool

    # ── Single experiment ─────────────────────────────────────────────────

    def _run_experiment(
        self,
        name: str,
        faults: "list[Fault]",
        workload: Callable,
        n_fault: int,
        baseline: dict,
        raw_baseline: "list[dict]",
    ) -> MeasurementResult:
        runner    = ChaosRunner(Scenario(name, faults), self.target, conflict="force")
        raw_fault: list[dict] = []
        runner.start()
        try:
            for _ in range(n_fault):
                raw_fault.append(workload())
        finally:
            runner.stop()

        fault_avg = _avg_metrics(raw_fault)
        delta = {
            k: round(fault_avg[k] - baseline[k], 6)
            for k in set(fault_avg) & set(baseline)
            if isinstance(fault_avg.get(k), (int, float))
            and isinstance(baseline.get(k), (int, float))
        }
        return MeasurementResult(
            scenario    = name,
            session_id  = runner._session_id or 0,
            baseline    = baseline,
            fault       = fault_avg,
            delta       = delta,
            n_baseline  = len(raw_baseline),
            n_fault     = n_fault,
            raw_baseline= raw_baseline,
            raw_fault   = raw_fault,
        )

    # ── Public measure() ──────────────────────────────────────────────────

    def measure(
        self,
        workload: "Callable[[], dict]",
        n_baseline: int = 3,
        n_fault: int = 3,
        n: int = 10,
        stop_on_first_failure: bool = False,
    ) -> "list[MeasurementResult]":
        """Run *n* random fault experiments against a shared baseline.

        1. Measures a shared baseline (``n_baseline`` trials, no fault).
        2. Picks *n* random fault combinations from the pool.
        3. For each: starts the fault, runs ``n_fault`` trials, stops it,
           and builds a :class:`~chaos_jungle.core.runner.MeasurementResult`
           with the shared baseline.

        Parameters
        ----------
        workload : callable
            Zero-argument callable returning a ``dict`` of metrics —
            same contract as ``ChaosRunner.measure()``.
        n_baseline : int
            Shared baseline trials. Default ``3``.
        n_fault : int
            Fault trials per experiment. Default ``3``.
        n : int
            Number of random experiments to run. Default ``10``.
        stop_on_first_failure : bool
            Stop after the first experiment whose oracles all fail.
            Default ``False``.

        Returns
        -------
        list[MeasurementResult]
            One result per completed experiment, each sharing the same
            baseline.
        """
        pool = self._build_pool()
        if not pool:
            raise ValueError("Fault pool is empty — cannot run experiments.")

        max_k   = min(self.max_faults_per_run, len(pool))
        seen: set[tuple[int, ...]] = set()

        # ── Shared baseline ───────────────────────────────────────────────
        print(f"[chaos-jungle] ChaosFuzzer: shared baseline ({n_baseline} trial(s)) ...")
        raw_baseline: list[dict] = [workload() for _ in range(n_baseline)]
        baseline = _avg_metrics(raw_baseline)

        # ── Random experiments ────────────────────────────────────────────
        results: list[MeasurementResult] = []
        attempts  = 0
        max_attempts = n * 5

        while len(results) < n and attempts < max_attempts:
            attempts += 1
            k     = self._rng.randint(1, max_k)
            combo = tuple(sorted(self._rng.sample(range(len(pool)), k)))
            if combo in seen:
                continue
            seen.add(combo)

            faults = [pool[i] for i in combo]
            name   = "fuzz/" + "+".join(type(f).__name__ for f in faults)

            try:
                result = self._run_experiment(
                    name, faults, workload, n_fault, baseline, raw_baseline
                )
                results.append(result)
                if stop_on_first_failure and result.oracle_results:
                    if not all(r.passed for r in result.oracle_results):
                        break
            except Exception as exc:
                print(f"  [fuzz] {name}: error — {exc}")

        return results


# ── Backward-compat wrappers ──────────────────────────────────────────────────

def fuzz_scenarios(
    fault_pool: "list[Fault]",
    workload: Callable,
    target: "Target",
    n_combinations: int = 10,
    max_faults_per_run: int = 2,
    n_baseline: int = 2,
    n_fault: int = 2,
    seed: "int | None" = None,
    stop_on_first_failure: bool = False,
    **_measure_kwargs,
) -> "list[MeasurementResult]":
    """Randomly combine faults from *fault_pool* and measure each combination.

    .. deprecated::
        Use :class:`ChaosFuzzer` directly::

            fuzzer = ChaosFuzzer(fault_pool=fault_pool, target=target, seed=seed)
            results = fuzzer.measure(workload, n_baseline=n_baseline,
                                     n_fault=n_fault, n=n_combinations)
    """
    fuzzer = ChaosFuzzer(
        fault_pool=fault_pool,
        target=target,
        seed=seed,
        max_faults_per_run=max_faults_per_run,
    )
    return fuzzer.measure(
        workload,
        n_baseline=n_baseline,
        n_fault=n_fault,
        n=n_combinations,
        stop_on_first_failure=stop_on_first_failure,
    )


def summarise_fuzz(results: "list[MeasurementResult]") -> str:
    """Return a human-readable table of fuzz results."""
    if not results:
        return "No fuzz results."

    lines = [
        f"{'Scenario':<48}  {'Pass':>4}  {'Fail':>4}  {'Cost':>8}  {'AvgLat':>8}",
        "-" * 78,
    ]
    for r in results:
        pass_n = sum(1 for o in (r.oracle_results or []) if o.passed)
        fail_n = sum(1 for o in (r.oracle_results or []) if not o.passed)
        cost   = r.fault.get("cost_usd", 0) if r.fault else 0
        lat    = r.fault.get("duration_s", 0) if r.fault else 0
        name   = r.scenario[:48]
        lines.append(
            f"{name:<48}  {pass_n:>4}  {fail_n:>4}  "
            f"${cost:>7.5f}  {lat:>7.2f}s"
        )
    lines.append("-" * 78)
    total_fail = sum(
        1 for r in results
        if any(not o.passed for o in (r.oracle_results or []))
    )
    lines.append(
        f"  {len(results)} combinations  —  {total_fail} caused oracle failures"
    )
    return "\n".join(lines)
