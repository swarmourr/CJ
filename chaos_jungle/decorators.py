"""Decorator and context manager interfaces for chaos-jungle."""

from __future__ import annotations
import functools
import io
import sys
from contextlib import contextmanager
from typing import Callable

from chaos_jungle.faults.base import Fault
from chaos_jungle.runner import ChaosRunner
from chaos_jungle.scenario import Scenario
from chaos_jungle.targets.base import Target
from chaos_jungle.targets.local import LocalTarget


def chaos(
    *faults: Fault,
    target: Target | None = None,
    scenario_name: str = "",
    conflict: str = "raise",
) -> Callable:
    """Decorator that wraps a function with chaos injection.

    Faults are started before the function runs and stopped (with
    revert) after it returns — even if the function raises an exception.

    Parameters
    ----------
    *faults : Fault
        One or more fault instances to inject.
    target : Target, optional
        Where to inject the faults. Defaults to :class:`~chaos_jungle.targets.local.LocalTarget`.
    scenario_name : str, optional
        Name stored in the database. Defaults to the function name.
    conflict : str
        Guardrail conflict mode: ``"raise"`` (default), ``"warn"``, or
        ``"force"``. See :func:`~chaos_jungle.guardrails.apply_guardrails`.

    Examples
    --------
    >>> @chaos(NetworkDelay("100ms"), StorageCorrupt("*.pdb", "/data"))
    ... def my_experiment():
    ...     run_pipeline()
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            name = scenario_name or fn.__name__
            runner = ChaosRunner(
                Scenario(name, list(faults)),
                target=target or LocalTarget(),
                conflict=conflict,
            )
            runner.start()
            try:
                return fn(*args, **kwargs)
            finally:
                runner.stop()
        return wrapper
    return decorator


@contextmanager
def chaos_session(
    *faults: Fault,
    target: Target | None = None,
    scenario_name: str = "chaos-session",
    conflict: str = "raise",
):
    """Context manager that injects chaos for the duration of the block.

    Parameters
    ----------
    *faults : Fault
        One or more fault instances to inject.
    target : Target, optional
        Where to inject. Defaults to :class:`~chaos_jungle.targets.local.LocalTarget`.
    scenario_name : str, optional
        Name stored in the database.
    conflict : str
        Guardrail conflict mode: ``"raise"`` (default), ``"warn"``, or
        ``"force"``. See :func:`~chaos_jungle.guardrails.apply_guardrails`.

    Yields
    ------
    ChaosRunner
        The active runner (use to export results inside the block).

    Examples
    --------
    >>> with chaos_session(NetworkDelay("100ms"), scenario_name="E3") as session:
    ...     run_pipeline()
    ...     print(session.export("json"))
    """
    runner = ChaosRunner(
        Scenario(scenario_name, list(faults)),
        target=target or LocalTarget(),
        conflict=conflict,
    )
    runner.start()
    try:
        yield runner
    finally:
        runner.stop()


def _collect_metrics(metrics_list, target) -> dict:
    """Run all metrics collectors and return a merged flat dict."""
    result = {}
    for m in metrics_list:
        try:
            values = m.collect(target)
            if isinstance(values, dict):
                for k, v in values.items():
                    result[f"{m.name}_{k}"] = v
        except Exception as exc:
            result[f"{m.name}_error"] = str(exc)
    return result


def chaos_measure(
    *faults: Fault,
    target: Target | None = None,
    scenario_name: str = "",
    metrics: list | None = None,
    capture_output: bool = False,
    auto_install: bool | str = False,
    conflict: str = "warn",
) -> Callable:
    """Decorator that runs a function under chaos, collects metrics, and
    auto-records all results.

    **Metric collection** happens in two phases:

    1. **Baseline** — before ``runner.start()``, each metric in
       ``metrics`` is collected. Keys are prefixed ``baseline_``.
    2. **Chaos** — after the function returns (before ``runner.stop()``),
       metrics are collected again. Keys are prefixed ``chaos_``.

    Both sets are merged with any dict returned by the function and
    stored via :meth:`~chaos_jungle.runner.ChaosRunner.record_result`.

    Parameters
    ----------
    *faults : Fault
        One or more fault instances to inject.
    target : Target, optional
        Where to inject the faults. Defaults to
        :class:`~chaos_jungle.targets.local.LocalTarget`.
    scenario_name : str, optional
        Name stored in the database. Defaults to the function name.
    metrics : list of :class:`~chaos_jungle.metrics.base.Metric`, optional
        Metric collectors to run at baseline and under chaos. Any
        subclass of :class:`~chaos_jungle.metrics.base.Metric` works here,
        including the built-ins
        :class:`~chaos_jungle.metrics.PingLatency`,
        :class:`~chaos_jungle.metrics.CommandMetric`,
        :class:`~chaos_jungle.metrics.FileIntegrity`, and
        :class:`~chaos_jungle.metrics.ThroughputMetric`.
    capture_output : bool, optional
        If ``True``, stdout printed inside the function is captured,
        printed normally, and returned in
        ``summary["captured_output"]``. Default ``False``.
    auto_install : bool or str, optional
        Passed to :class:`~chaos_jungle.runner.ChaosRunner`. Use
        ``"prompt"`` to ask before installing missing deps.
    conflict : str
        Guardrail conflict mode: ``"raise"``, ``"warn"`` (default), or
        ``"force"``.

    Returns
    -------
    Callable
        The wrapper returns a dict with all keys from
        :meth:`~chaos_jungle.runner.ChaosRunner.summary` plus:

        * ``fn_result``        — raw return value of the function
        * ``metrics``          — ``{"baseline": {...}, "chaos": {...}}``
        * ``captured_output``  — stdout text (only when ``capture_output=True``)

    Examples
    --------
    >>> from chaos_jungle.metrics import PingLatency, CommandMetric
    >>>
    >>> @chaos_measure(
    ...     NetworkDelay("100ms"),
    ...     metrics=[
    ...         PingLatency("127.0.0.1", count=5),
    ...         CommandMetric(
    ...             "ss -tn | grep ESTAB | wc -l",
    ...             parse=lambda out: {"open_connections": int(out.strip())},
    ...             name="tcp",
    ...         ),
    ...     ],
    ...     scenario_name="E1",
    ... )
    ... def run_experiment():
    ...     run_pipeline()
    ...     return {"retries": 3}
    ...
    >>> summary = run_experiment()
    >>> print(summary["metrics"]["baseline"])
    >>> # {"ping_avg_ms": 0.2, "ping_min_ms": 0.1, ..., "tcp_open_connections": 12}
    >>> print(summary["metrics"]["chaos"])
    >>> # {"ping_avg_ms": 108.6, ..., "tcp_open_connections": 8}
    """
    _metrics = metrics or []

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            name = scenario_name or fn.__name__
            tgt = target or LocalTarget()
            runner = ChaosRunner(
                Scenario(name, list(faults)),
                target=tgt,
                auto_install=auto_install,
                conflict=conflict,
            )

            # ── 1. Baseline metrics (before chaos) ────────────────
            if _metrics:
                tgt.connect()
            baseline_data = _collect_metrics(_metrics, tgt) if _metrics else {}

            # ── 2. Start chaos ────────────────────────────────────
            runner.start()

            # ── 3. Run function ───────────────────────────────────
            captured: str | None = None
            _buf: io.StringIO | None = None
            old_stdout = None
            if capture_output:
                _buf = io.StringIO()
                old_stdout = sys.stdout
                sys.stdout = _buf
            try:
                result = fn(*args, **kwargs)
            finally:
                if capture_output and _buf is not None:
                    captured = _buf.getvalue()
                    sys.stdout = old_stdout
                    print(captured, end="")

            # ── 4. Chaos metrics (after function, before stop) ────
            chaos_data = _collect_metrics(_metrics, tgt) if _metrics else {}

            # ── 5. Stop chaos ─────────────────────────────────────
            runner.stop()

            # ── 6. Merge and record all results ───────────────────
            merged: dict = {}
            # flatten baseline_ / chaos_ prefixed keys into one dict for DB
            for k, v in baseline_data.items():
                merged[f"baseline_{k}"] = v
            for k, v in chaos_data.items():
                merged[f"chaos_{k}"] = v
            if isinstance(result, dict):
                for k, v in result.items():
                    merged[f"fn_{k}"] = v

            if merged:
                runner.record_result(merged)

            summary = runner.summary()
            summary["fn_result"] = result
            summary["metrics"] = {
                "baseline": baseline_data,
                "chaos": chaos_data,
            }
            if captured is not None:
                summary["captured_output"] = captured
            return summary
        return wrapper
    return decorator
