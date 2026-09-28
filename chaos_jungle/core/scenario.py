"""Scenario — a named group of faults and/or injection groups."""

from __future__ import annotations
import uuid
from chaos_jungle.faults.base import Fault


class Scenario:
    """A named collection of faults / injection groups to inject together.

    A scenario is a pure data container. It has no knowledge of
    targets, workloads, or the database.

    Parameters
    ----------
    name : str
        Human-readable name used in the database and CLI output.
    faults : list[Fault]
        Individual faults to inject (all share the ChaosRunner's single
        target). For multi-target injection use ``groups`` instead.
    groups : list[InjectionGroup], optional
        Coordinated injection groups. Each group carries its own targets
        and runs via the two-phase prepare/commit protocol. Groups are
        started after the individual ``faults`` and stopped before them
        during rollback.

    Examples
    --------
    Single-target (existing API, unchanged)::

        scenario = Scenario("net-chaos", faults=[
            NetworkDelay("100ms"),
            NetworkLoss("5%"),
        ])

    Multi-target group injection::

        from chaos_jungle.inject.group import InjectionGroup
        from chaos_jungle.distributed import Injection
        scenario = Scenario(
            "distributed-agent-failure",
            faults=[],
            groups=[
                InjectionGroup(
                    name="cross-layer",
                    injections=[
                        Injection("llm", HTTPTarget("http://agent:8080"), LLMLatency(delay_s=1.0)),
                        Injection("net", SSHTarget("tool-node", user="ubuntu"), NetworkLoss("5%")),
                    ],
                )
            ],
        )
    """

    def __init__(
        self,
        name: str,
        faults: "list[Fault] | None" = None,
        groups: "list | None" = None,
    ) -> None:
        if not name or not str(name).strip():
            raise ValueError(
                "Scenario requires a non-empty 'name'.\n"
                "  Example: Scenario('my-experiment', faults=[NetworkDelay('100ms')])"
            )
        faults = list(faults or [])
        groups = list(groups or [])

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
        self.id = str(uuid.uuid4())
        self.name = str(name).strip()
        self.faults = faults
        self.groups = groups

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict (used by ScenarioRegistry)."""
        d: dict = {
            "id": self.id,
            "name": self.name,
            "faults": [
                {"kind": f.__class__.__name__, "params": f._parameters()}
                for f in self.faults
            ],
        }
        if self.groups:
            d["groups"] = [
                {"name": g.name, "group_id": g.group_id, "n_injections": len(g.injections)}
                for g in self.groups
            ]
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Scenario":
        """Reconstruct a Scenario from a serialized dict.

        Requires chaos-jungle to be installed on the machine calling this
        (uses the fault class registry to reconstruct fault objects).
        """
        import chaos_jungle.faults as _faults_mod

        faults = []
        for entry in data.get("faults", []):
            kind = entry["kind"]
            params = entry.get("params", {})
            fault_cls = getattr(_faults_mod, kind, None)
            if fault_cls is None:
                raise ValueError(
                    f"Unknown fault class {kind!r}. "
                    "Make sure the same version of chaos-jungle is installed on both machines."
                )
            faults.append(fault_cls(**params))

        scenario = cls.__new__(cls)
        scenario.id = data["id"]
        scenario.name = data["name"]
        scenario.faults = faults
        return scenario

    def __repr__(self) -> str:
        fault_names = [f.__class__.__name__ for f in self.faults]
        return f"Scenario(name={self.name!r}, faults={fault_names})"
