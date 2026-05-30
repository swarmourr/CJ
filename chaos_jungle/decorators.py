"""Decorator and context manager interfaces for chaos-jungle."""

from __future__ import annotations
import functools
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
