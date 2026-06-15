"""Resilient controller bridge for chaos-jungle skill-file faults.

The installed version of _LocalSkillFault subclasses do not implement
revert(), which keeps them abstract and uninstantiable.  This module:

  1. Bridges the gap by subclassing each fault and mapping revert() → stop().
  2. Fixes the DB path to ~/.chaos-jungle/ (writable on all machines).
  3. Provides build_scenario() that silently skips any fault that cannot be
     instantiated, so one bad fault does not abort the whole experiment.
  4. Provides run_experiment() as a single-call entry point.
"""

from __future__ import annotations

import os
from typing import Callable

from chaos_jungle import Scenario, ChaosRunner
from chaos_jungle.targets import LocalTarget
from chaos_jungle.faults.skill_file import (
    _LocalSkillFault,
    SkillFileInstructionCorrupt as _SkillFileInstructionCorrupt,
    SkillFileUnavailable as _SkillFileUnavailable,
    SkillFileConflict as _SkillFileConflict,
    SkillFileBadOutput as _SkillFileBadOutput,
    SkillFileVersionSkew as _SkillFileVersionSkew,
    SkillFileMemoryStale as _SkillFileMemoryStale,
    SkillFilePermissionDenied as _SkillFilePermissionDenied,
)

# ---------------------------------------------------------------------------
# DB path — guaranteed writable
# ---------------------------------------------------------------------------

_DB_PATH = os.path.join(os.path.expanduser("~"), ".chaos-jungle", "chaos_jungle.db")
os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)


# ---------------------------------------------------------------------------
# Bridge: add revert() → stop() to every _LocalSkillFault subclass
# ---------------------------------------------------------------------------

def _add_revert(base_cls: type) -> type:
    """Return a concrete subclass of *base_cls* with revert() delegating to stop()."""
    class _Bridged(base_cls):  # type: ignore[valid-type]
        def revert(self, target) -> None:
            self.stop(target)
    _Bridged.__name__ = base_cls.__name__
    _Bridged.__qualname__ = base_cls.__qualname__
    return _Bridged


SkillFileInstructionCorrupt = _add_revert(_SkillFileInstructionCorrupt)
SkillFileUnavailable        = _add_revert(_SkillFileUnavailable)
SkillFileConflict           = _add_revert(_SkillFileConflict)
SkillFileBadOutput          = _add_revert(_SkillFileBadOutput)
SkillFileVersionSkew        = _add_revert(_SkillFileVersionSkew)
SkillFileMemoryStale        = _add_revert(_SkillFileMemoryStale)
SkillFilePermissionDenied   = _add_revert(_SkillFilePermissionDenied)


# ---------------------------------------------------------------------------
# Resilient scenario builder
# ---------------------------------------------------------------------------

def build_scenario(
    name: str,
    fault_factories: list[Callable],
) -> tuple[Scenario, list[tuple[str, str]]]:
    """Build a Scenario from *fault_factories*, skipping any that fail.

    Parameters
    ----------
    name:
        Scenario name passed to ChaosRunner.
    fault_factories:
        List of zero-argument callables, each returning a Fault instance.

    Returns
    -------
    scenario:
        Scenario containing only the successfully constructed faults.
    skipped:
        List of ``(factory_name, reason)`` for faults that were skipped.
    """
    faults: list = []
    skipped: list[tuple[str, str]] = []

    for factory in fault_factories:
        name_ = getattr(factory, "__name__", repr(factory))
        try:
            fault = factory()
            faults.append(fault)
        except TypeError as exc:
            skipped.append((name_, f"abstract / bad args: {exc}"))
        except Exception as exc:
            skipped.append((name_, str(exc)))

    if skipped:
        for fname, reason in skipped:
            print(f"[controller] skipped fault {fname!r}: {reason}")

    return Scenario(name, faults), skipped


# ---------------------------------------------------------------------------
# Single-call experiment entry point
# ---------------------------------------------------------------------------

def run_experiment(
    name: str,
    fault_factories: list[Callable],
    workload: Callable,
    **measure_kwargs,
):
    """Build scenario, run experiment, return MeasurementResult (or None).

    Parameters
    ----------
    name:
        Scenario / experiment name.
    fault_factories:
        List of zero-argument callables, each returning a Fault instance.
    workload:
        Callable passed to runner.measure().
    **measure_kwargs:
        Extra keyword arguments forwarded to runner.measure()
        (e.g. n_baseline=5, n_fault=5).

    Returns
    -------
    MeasurementResult or None
        None when no faults could be instantiated.
    """
    scenario, _skipped = build_scenario(name, fault_factories)

    if not scenario.faults:
        print(f"[controller] no faults loaded for scenario {name!r} — skipping run.")
        return None

    runner = ChaosRunner(scenario, LocalTarget(), db_path=_DB_PATH)
    return runner.measure(workload, **measure_kwargs)


__all__ = [
    "SkillFileInstructionCorrupt",
    "SkillFileUnavailable",
    "SkillFileConflict",
    "SkillFileBadOutput",
    "SkillFileVersionSkew",
    "SkillFileMemoryStale",
    "SkillFilePermissionDenied",
    "build_scenario",
    "run_experiment",
]
