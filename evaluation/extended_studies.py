"""Scientific primitives for the additive E7, E8, and E9 studies.

The existing canonical studies remain immutable.  This module provides new
coordination and measurement logic without changing CJ fault implementations.
"""

from __future__ import annotations

import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Protocol


class VerifiableFault(Protocol):
    def start(self, target: Any) -> None: ...
    def stop(self, target: Any) -> None: ...
    def verify_active(self, target: Any) -> Any: ...
    def verify_recovered(self, target: Any) -> Any: ...


@dataclass(frozen=True)
class CoordinatedFaultBinding:
    """One independently verifiable fault bound to one explicit target."""

    target_id: str
    layer: str
    fault: VerifiableFault
    target: Any
    role: str = ""


@dataclass
class TargetLifecycle:
    target_id: str
    layer: str
    role: str = ""
    configured: bool = True
    activated: bool = False
    triggered: bool = False
    manifested: bool = False
    reverted: bool = False
    recovered: bool = False
    activation_started_s: float | None = None
    activation_verified_s: float | None = None
    reversion_started_s: float | None = None
    recovery_verified_s: float | None = None
    activation_evidence: dict[str, Any] = field(default_factory=dict)
    recovery_evidence: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class CoordinatedFaultResult:
    study: str
    target_lifecycles: list[TargetLifecycle]
    workload_result: Any = None
    workload_started_s: float | None = None
    workload_finished_s: float | None = None
    partial_activation: bool = False
    group_valid: bool = False
    rollback_success: bool = False
    activation_skew_ms: float | None = None
    rollback_skew_ms: float | None = None
    verdict: str = "inconclusive"
    details: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _verification_dict(result: Any) -> dict[str, Any]:
    if result is None:
        return {"verified": False, "reason": "verification returned no result"}
    if isinstance(result, dict):
        return dict(result)
    try:
        return asdict(result)
    except TypeError:
        return {
            "verified": bool(getattr(result, "verified", False)),
            "reason": str(getattr(result, "reason", "")),
            "observed": dict(getattr(result, "observed", {}) or {}),
            "timestamp_s": getattr(result, "timestamp_s", None),
        }


class CoordinatedFaultGroup:
    """Activate and roll back a fault set as one evidence boundary.

    Activation and verification run concurrently across independent targets.
    Any partial activation aborts the workload and rolls back every target that
    may have changed.  Recovery is verified independently per target.
    """

    def __init__(
        self,
        bindings: list[CoordinatedFaultBinding],
        *,
        study: str,
        max_workers: int | None = None,
    ) -> None:
        if not bindings:
            raise ValueError("a coordinated fault group requires at least one target")
        ids = [binding.target_id for binding in bindings]
        if len(ids) != len(set(ids)):
            raise ValueError("coordinated target identifiers must be unique")
        self.bindings = list(bindings)
        self.study = study
        self.max_workers = max_workers or len(bindings)

    def run(self, workload: Callable[[], Any] | None = None) -> CoordinatedFaultResult:
        states = {
            binding.target_id: TargetLifecycle(
                target_id=binding.target_id,
                layer=binding.layer,
                role=binding.role,
            )
            for binding in self.bindings
        }
        result = CoordinatedFaultResult(
            study=self.study,
            target_lifecycles=list(states.values()),
        )
        activated_bindings: list[CoordinatedFaultBinding] = []
        activation_error = ""
        try:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(self._activate_one, binding, states[binding.target_id]): binding
                    for binding in self.bindings
                }
                for future in as_completed(futures):
                    binding = futures[future]
                    try:
                        if future.result():
                            activated_bindings.append(binding)
                    except Exception as exc:  # noqa: BLE001
                        state = states[binding.target_id]
                        state.error = repr(exc)
                        activation_error = state.error

            verified_times = [
                state.activation_verified_s
                for state in states.values()
                if state.activation_verified_s is not None
            ]
            result.activation_skew_ms = _skew_ms(verified_times)
            active_count = sum(state.activated for state in states.values())
            result.partial_activation = 0 < active_count < len(states)
            all_active = active_count == len(states)
            if all_active:
                result.workload_started_s = time.time()
                result.workload_result = workload() if workload else None
                result.workload_finished_s = time.time()
                for state in states.values():
                    state.triggered = True
                    state.manifested = True
            else:
                result.details = activation_error or "not every target verified activation"
        finally:
            # Roll back every binding, not only verified bindings: start() may
            # have changed a target before verification failed.
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(self._recover_one, binding, states[binding.target_id]): binding
                    for binding in self.bindings
                }
                for future in as_completed(futures):
                    binding = futures[future]
                    try:
                        future.result()
                    except Exception as exc:  # noqa: BLE001
                        state = states[binding.target_id]
                        state.error = state.error or repr(exc)

        recovery_times = [
            state.recovery_verified_s
            for state in states.values()
            if state.recovery_verified_s is not None
        ]
        result.rollback_skew_ms = _skew_ms(recovery_times)
        result.rollback_success = all(state.reverted and state.recovered for state in states.values())
        result.group_valid = all(
            state.activated and state.triggered and state.manifested
            for state in states.values()
        )
        if result.group_valid and result.rollback_success:
            result.verdict = "valid"
        elif result.partial_activation or not result.rollback_success:
            result.verdict = "invalid"
        elif not any(state.activated for state in states.values()):
            result.verdict = "invalid"
        else:
            result.verdict = "inconclusive"
        return result

    @staticmethod
    def _activate_one(binding: CoordinatedFaultBinding, state: TargetLifecycle) -> bool:
        state.activation_started_s = time.time()
        binding.fault.start(binding.target)
        verification = _verification_dict(binding.fault.verify_active(binding.target))
        state.activation_verified_s = time.time()
        state.activation_evidence = verification
        state.activated = bool(verification.get("verified"))
        return state.activated

    @staticmethod
    def _recover_one(binding: CoordinatedFaultBinding, state: TargetLifecycle) -> None:
        state.reversion_started_s = time.time()
        binding.fault.stop(binding.target)
        state.reverted = True
        verification = _verification_dict(binding.fault.verify_recovered(binding.target))
        state.recovery_verified_s = time.time()
        state.recovery_evidence = verification
        state.recovered = bool(verification.get("verified"))


def _skew_ms(values: list[float]) -> float | None:
    if not values:
        return None
    return (max(values) - min(values)) * 1000.0


def compound_interaction_effect(
    *,
    control: float,
    fault_a: float,
    fault_b: float,
    compound: float,
) -> float:
    """Difference-in-differences interaction for loss-like outcomes.

    Positive values mean the compound degradation exceeds the sum of the two
    isolated degradations.
    """

    degradation_a = control - fault_a
    degradation_b = control - fault_b
    degradation_compound = control - compound
    return degradation_compound - (degradation_a + degradation_b)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if not 0.0 <= q <= 1.0:
        raise ValueError("percentile must be between zero and one")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


@dataclass
class LoadSample:
    duration_s: float
    success: bool
    status_code: int | None = None
    bytes_received: int = 0
    error: str = ""


def summarize_load(samples: list[LoadSample], wall_duration_s: float) -> dict[str, Any]:
    durations_ms = [sample.duration_s * 1000.0 for sample in samples]
    successes = sum(sample.success for sample in samples)
    return {
        "request_count": len(samples),
        "success_count": successes,
        "error_count": len(samples) - successes,
        "error_rate": ((len(samples) - successes) / len(samples)) if samples else None,
        "latency_p50_ms": percentile(durations_ms, 0.50),
        "latency_p95_ms": percentile(durations_ms, 0.95),
        "latency_p99_ms": percentile(durations_ms, 0.99),
        "latency_mean_ms": statistics.fmean(durations_ms) if durations_ms else None,
        "throughput_rps": (len(samples) / wall_duration_s) if wall_duration_s > 0 else None,
        "bytes_received": sum(sample.bytes_received for sample in samples),
        "wall_duration_s": wall_duration_s,
    }


@dataclass
class RestorationCycle:
    cycle_index: int
    activated: bool
    manifested: bool
    reverted: bool
    recovered: bool
    activation_time_s: float | None
    cleanup_time_s: float | None
    error: str = ""


def summarize_restoration(cycles: list[RestorationCycle]) -> dict[str, Any]:
    cleanup = [cycle.cleanup_time_s for cycle in cycles if cycle.cleanup_time_s is not None]
    activation = [cycle.activation_time_s for cycle in cycles if cycle.activation_time_s is not None]
    count = len(cycles)
    return {
        "cycle_count": count,
        "activation_success_rate": sum(c.activated for c in cycles) / count if count else None,
        "manifestation_rate": sum(c.manifested for c in cycles) / count if count else None,
        "reversion_success_rate": sum(c.reverted for c in cycles) / count if count else None,
        "verified_restoration_rate": sum(c.recovered for c in cycles) / count if count else None,
        "cleanup_p50_s": percentile(cleanup, 0.50),
        "cleanup_p95_s": percentile(cleanup, 0.95),
        "activation_p50_s": percentile(activation, 0.50),
        "activation_p95_s": percentile(activation, 0.95),
    }
