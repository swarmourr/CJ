"""Scenario — a named group of faults."""

from __future__ import annotations
from chaos_jungle.faults.base import Fault


class Scenario:
    """A named collection of faults to inject together.

    A scenario is a pure data container. It has no knowledge of
    targets, workloads, or the database.

    Parameters
    ----------
    name : str
        Human-readable name used in the database and CLI output.
    faults : list[Fault]
        Faults to inject when this scenario is started.

    Examples
    --------
    >>> from chaos_jungle.faults.network import NetworkDelay, NetworkLoss
    >>> scenario = Scenario("net-chaos", faults=[
    ...     NetworkDelay("100ms", jitter="10ms"),
    ...     NetworkLoss("5%"),
    ... ])
    """

    def __init__(self, name: str, faults: list[Fault]) -> None:
        if not name or not str(name).strip():
            raise ValueError(
                "Scenario requires a non-empty 'name'.\n"
                "  Example: Scenario('my-experiment', faults=[NetworkDelay('100ms')])"
            )
        if not isinstance(faults, (list, tuple)):
            raise TypeError(
                f"Scenario 'faults' must be a list of Fault instances, got {type(faults).__name__}.\n"
                "  Example: Scenario('test', faults=[NetworkDelay('100ms')])"
            )
        for i, f in enumerate(faults):
            if not isinstance(f, Fault):
                raise TypeError(
                    f"Scenario 'faults[{i}]' must be a Fault instance, got {type(f).__name__}.\n"
                    "  Example: faults=[NetworkDelay('100ms'), NetworkLoss('5%')]"
                )
        self.name = str(name).strip()
        self.faults = list(faults)

    def __repr__(self) -> str:
        fault_names = [f.__class__.__name__ for f in self.faults]
        return f"Scenario(name={self.name!r}, faults={fault_names})"
