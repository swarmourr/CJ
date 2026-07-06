"""Hypothesis — declare expected behaviour before an experiment, verify after.

A Hypothesis is a named set of metric assertions attached to an experiment.
Declare it before running, check it against the MeasurementResult afterward.

Example::

    from chaos_jungle import Hypothesis, ChaosRunner, Scenario, NetworkDelay
    from chaos_jungle.targets import LocalTarget

    h = (
        Hypothesis("system handles 200ms delay gracefully")
        .max_delta_pct("duration_s", 50)        # latency may grow by at most 50%
        .max_fault_value("error_rate", 0.05)    # error rate must stay under 5%
        .no_regression("completion_rate")        # completion rate must not drop
    )

    runner = ChaosRunner(
        Scenario("delay", [NetworkDelay("200ms")]),
        LocalTarget(),
    )
    result = runner.measure(workload, n_baseline=5, n_fault=5, hypothesis=h)
    print(result.hypothesis_result.summary())
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chaos_jungle.runner import MeasurementResult


@dataclass
class AssertionResult:
    metric: str
    kind: str
    passed: bool
    expected: str
    actual: str
    reason: str


@dataclass
class HypothesisResult:
    """Result of evaluating a Hypothesis against a MeasurementResult."""

    name: str
    passed: bool
    assertions: list[AssertionResult] = field(default_factory=list)

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [f"Hypothesis [{status}]: {self.name}"]
        for a in self.assertions:
            s = "PASS" if a.passed else "FAIL"
            lines.append(f"  [{s}] {a.metric:<30} {a.reason}")
        n_fail = sum(1 for a in self.assertions if not a.passed)
        if n_fail:
            lines.append(f"  {n_fail} assertion(s) FAILED")
        else:
            lines.append(f"  All {len(self.assertions)} assertion(s) passed")
        return "\n".join(lines)


class Hypothesis:
    """Declare the expected behaviour of a system under fault before running.

    Build the hypothesis with chained assertion methods, then pass it to
    ``ChaosRunner.measure()`` via the ``hypothesis=`` parameter, or call
    ``.check(result)`` manually on any :class:`~chaos_jungle.runner.MeasurementResult`.

    Parameters
    ----------
    name : str
        Human-readable name for this hypothesis, e.g.
        ``"system handles 200ms delay with < 50% latency increase"``.
    description : str, optional
        Longer free-text description stored with the hypothesis result.

    Examples
    --------
    ::

        from chaos_jungle import Hypothesis

        h = (
            Hypothesis("degraded network does not break the pipeline")
            .max_delta_pct("duration_s", 100)
            .max_fault_value("error_rate", 0.1)
            .no_regression("completion_rate", tolerance=0.05)
        )

        result = runner.measure(workload, n_baseline=5, n_fault=5, hypothesis=h)
        if not result.hypothesis_result.passed:
            print(result.hypothesis_result.summary())
    """

    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description
        self._assertions: list[tuple] = []

    # ── Assertion builders ────────────────────────────────────────────────────

    def max_delta_pct(self, metric: str, pct: float) -> "Hypothesis":
        """Fault metric may increase by at most ``pct`` % relative to baseline.

        Example: ``.max_delta_pct("duration_s", 50)`` — latency may grow by at
        most 50% over the no-fault baseline.
        """
        self._assertions.append(("max_delta_pct", metric, pct))
        return self

    def max_fault_value(self, metric: str, value: float) -> "Hypothesis":
        """Fault metric must be <= ``value`` in absolute terms.

        Example: ``.max_fault_value("error_rate", 0.05)`` — error rate must
        stay below 5% even under the fault.
        """
        self._assertions.append(("max_fault_value", metric, value))
        return self

    def min_fault_value(self, metric: str, value: float) -> "Hypothesis":
        """Fault metric must be >= ``value`` in absolute terms.

        Example: ``.min_fault_value("completion_rate", 0.90)`` — at least 90%
        of workload calls must complete under the fault.
        """
        self._assertions.append(("min_fault_value", metric, value))
        return self

    def no_regression(self, metric: str, tolerance: float = 0.0) -> "Hypothesis":
        """Fault metric must not drop below ``baseline - tolerance``.

        Useful for metrics where lower is worse (throughput, completion rate,
        faithfulness). A ``tolerance`` of ``0.05`` allows up to a 0.05 absolute
        drop before the assertion fails.
        """
        self._assertions.append(("no_regression", metric, tolerance))
        return self

    def max_absolute_delta(self, metric: str, delta: float) -> "Hypothesis":
        """Absolute difference between fault and baseline must be <= ``delta``.

        Example: ``.max_absolute_delta("p99_latency_ms", 500)`` — p99 latency
        may not shift by more than 500 ms in either direction.
        """
        self._assertions.append(("max_absolute_delta", metric, delta))
        return self

    # ── Evaluation ────────────────────────────────────────────────────────────

    def check(self, result: "MeasurementResult") -> HypothesisResult:
        """Evaluate all assertions against *result* and return a HypothesisResult.

        Parameters
        ----------
        result : MeasurementResult
            The result of a ``ChaosRunner.measure()`` call.

        Returns
        -------
        HypothesisResult
            Contains per-assertion pass/fail details and an overall ``passed``
            flag.
        """
        assertion_results: list[AssertionResult] = []

        for entry in self._assertions:
            kind, metric = entry[0], entry[1]
            param: float = entry[2]

            b_val = result.baseline.get(metric)
            f_val = result.fault.get(metric)
            d_val = result.delta.get(metric)

            if not isinstance(b_val, (int, float)) or not isinstance(f_val, (int, float)):
                assertion_results.append(AssertionResult(
                    metric=metric, kind=kind, passed=False,
                    expected=str(param), actual="not measured",
                    reason=f"metric '{metric}' not found in results",
                ))
                continue

            if kind == "max_delta_pct":
                if b_val == 0:
                    passed = f_val == 0
                    actual_pct = 0.0 if passed else float("inf")
                else:
                    actual_pct = abs(d_val / b_val) * 100 if d_val is not None else 0.0
                    passed = actual_pct <= param
                assertion_results.append(AssertionResult(
                    metric=metric, kind=kind, passed=passed,
                    expected=f"delta<={param}%",
                    actual=f"delta={actual_pct:.1f}%",
                    reason=f"delta={actual_pct:.1f}% (limit={param}%)",
                ))

            elif kind == "max_fault_value":
                passed = f_val <= param
                assertion_results.append(AssertionResult(
                    metric=metric, kind=kind, passed=passed,
                    expected=f"<={param}", actual=str(round(f_val, 6)),
                    reason=f"fault={round(f_val, 4)} (limit={param})",
                ))

            elif kind == "min_fault_value":
                passed = f_val >= param
                assertion_results.append(AssertionResult(
                    metric=metric, kind=kind, passed=passed,
                    expected=f">={param}", actual=str(round(f_val, 6)),
                    reason=f"fault={round(f_val, 4)} (min={param})",
                ))

            elif kind == "no_regression":
                threshold = b_val - param
                passed = f_val >= threshold
                assertion_results.append(AssertionResult(
                    metric=metric, kind=kind, passed=passed,
                    expected=f">={round(threshold, 4)}",
                    actual=str(round(f_val, 6)),
                    reason=f"fault={round(f_val, 4)} baseline={round(b_val, 4)} tolerance={param}",
                ))

            elif kind == "max_absolute_delta":
                actual_delta = abs(d_val) if d_val is not None else 0.0
                passed = actual_delta <= param
                assertion_results.append(AssertionResult(
                    metric=metric, kind=kind, passed=passed,
                    expected=f"|delta|<={param}",
                    actual=f"|delta|={round(actual_delta, 4)}",
                    reason=f"|fault-baseline|={round(actual_delta, 4)} (limit={param})",
                ))

        overall = all(a.passed for a in assertion_results)
        return HypothesisResult(
            name=self.name,
            passed=overall,
            assertions=assertion_results,
        )

    def __repr__(self) -> str:
        return f"Hypothesis({self.name!r}, assertions={len(self._assertions)})"
