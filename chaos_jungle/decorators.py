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


def chaos_measure(
    *faults: Fault,
    target: Target | None = None,
    scenario_name: str = "",
    capture_output: bool = False,
    auto_install: bool | str = False,
    conflict: str = "warn",
) -> Callable:
    """Decorator that runs a function under chaos and auto-records its results.

    If the decorated function returns a ``dict``, it is automatically stored
    via :meth:`~chaos_jungle.runner.ChaosRunner.record_result` so the metrics
    appear in the dashboard without any extra code.

    Parameters
    ----------
    *faults : Fault
        One or more fault instances to inject.
    target : Target, optional
        Where to inject the faults. Defaults to :class:`~chaos_jungle.targets.local.LocalTarget`.
    scenario_name : str, optional
        Name stored in the database. Defaults to the function name.
    capture_output : bool, optional
        If ``True``, stdout printed inside the function is captured and
        returned in ``summary["captured_output"]`` as well as printed
        normally. Default ``False``.
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

        * ``fn_result``        — the raw return value of the function
        * ``captured_output``  — stdout text (only when ``capture_output=True``)

    Examples
    --------
    >>> @chaos_measure(NetworkDelay("100ms"), scenario_name="E1")
    ... def run_experiment():
    ...     run_pipeline()
    ...     return {
    ...         "files_transferred": 120,
    ...         "retries": 3,
    ...         "throughput_mbps": 42.1,
    ...     }
    ...
    >>> summary = run_experiment()
    >>> print(summary["duration_s"], "s of chaos")
    >>> print(summary["fn_result"])   # the dict above
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            name = scenario_name or fn.__name__
            runner = ChaosRunner(
                Scenario(name, list(faults)),
                target=target or LocalTarget(),
                auto_install=auto_install,
                conflict=conflict,
            )
            runner.start()
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
                runner.stop()
            if isinstance(result, dict):
                runner.record_result(result)
            summary = runner.summary()
            summary["fn_result"] = result
            if captured is not None:
                summary["captured_output"] = captured
            return summary
        return wrapper
    return decorator
