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
        self.name = name
        self.faults = faults

    def __repr__(self) -> str:
        fault_names = [f.__class__.__name__ for f in self.faults]
        return f"Scenario(name={self.name!r}, faults={fault_names})"
