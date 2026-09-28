"""Canonical ExperimentPlan — the single intermediate representation (IR)
that every CJ authoring interface compiles to.

All of the following produce an equivalent ``ExperimentPlan``:

* Explicit Python:
    from chaos_jungle.plan import ExperimentPlan, ScenarioSpec, TargetSpec
    plan = ExperimentPlan(scenario=ScenarioSpec(...), target=TargetSpec(...), ...)

* Scenario + Target compile helper:
    plan = ExperimentPlan.from_scenario(scenario, target, duration=30)

* YAML suite entry:
    plan = ExperimentPlan.from_yaml_entry(entry_dict)

* Decorator / context manager:
    plan = runner.active_plan  # set on ChaosRunner at start()

Design goals
------------
* Validated — ``plan.validate()`` raises on invalid field combinations.
* Serializable — ``plan.to_dict()`` produces a JSON-safe dict.
* Hashable — ``plan.plan_hash`` is SHA-256 of the canonical dict (stable
  across Python runs, ignores plan_id / timestamps).
* Versioned — ``schema_version`` must be bumped when the IR changes.
* Storable — write as ``resolved_plan.json`` next to run results.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any


SCHEMA_VERSION = "1.0"

# ---------------------------------------------------------------------------
# Sub-specs
# ---------------------------------------------------------------------------

@dataclass
class FaultSpec:
    """One fault within a scenario."""
    fault_class: str           # e.g. "LLMLatency"
    parameters: dict = field(default_factory=dict)
    layer: str = "llm"         # llm | network | process | storage | state | resource | gpu | skill


@dataclass
class ScenarioSpec:
    """What faults to inject and how to handle conflicts."""
    name: str
    faults: list[FaultSpec] = field(default_factory=list)
    conflict_policy: str = "raise"   # raise | warn | force


@dataclass
class TargetSpec:
    """Where the faults are injected."""
    kind: str = "local"              # local | ssh | http
    host: str = ""
    user: str = ""
    port: int = 0
    url: str = ""                    # full URL for http targets


@dataclass
class TriggerSpec:
    """When to start injection."""
    mode: str = "immediate"          # immediate | on_event | scheduled
    event_type: str = ""
    delay_s: float = 0.0


@dataclass
class HypothesisSpec:
    """The falsifiable claim this experiment tests."""
    statement: str = ""
    metric: str = ""
    condition: str = ""              # e.g. "success_rate < 0.8"
    threshold: float = 0.0
    direction: str = "below"        # above | below


@dataclass
class ObservationSpec:
    """What to measure and how to collect evidence."""
    metrics: list[str] = field(default_factory=lambda: ["latency_ms", "success_rate"])
    evidence_method: str = "proxy_interception"   # proxy_interception | syscall | log
    capture_events: bool = True
    streaming_secondary: bool = True    # mark streaming results as secondary


@dataclass
class SafetySpec:
    """Hard limits that abort the experiment."""
    max_duration_s: float = 300.0
    emergency_stop_on_failure: bool = True
    max_injections: int = 1
    budget_limit_usd: float = 1.0


@dataclass
class CleanupSpec:
    """State restoration and recovery verification."""
    restore_state: bool = True
    verify_recovery: bool = True
    timeout_s: float = 30.0
    on_failure: str = "abort"        # abort | warn | ignore


# ---------------------------------------------------------------------------
# FaultLifecycleRecord — the canonical evidence format (per-request)
# ---------------------------------------------------------------------------

@dataclass
class FaultLifecycleRecord:
    """Full causal-chain evidence for one fault on one LLM/tool call.

    Lifecycle states (in order):
      configured  → Fault exists in the scenario.
      activated   → Injection mechanism is operational (proxy running).
      target_matched → The matching event occurred (right call, right endpoint).
      triggered   → The injector attempted its action.
      applied     → The injector successfully performed its action.
      manifested  → The target experienced the intended effect.
      recovered   → The target returned to its clean state.

    Primary experiment results should include only runs where
    ``manifested=True``.
    """
    fault_id: str = ""              # unique per-call ID, e.g. "latency_call_3"
    fault_type: str = ""            # proxy fault name, e.g. "latency"
    fault_class: str = ""           # CJ class name, e.g. "LLMLatency"
    layer: str = "llm"

    # Target description
    target: dict = field(default_factory=dict)
    # e.g. {"provider": "openai", "operation": "chat.completions", "call_index": 2}

    # Lifecycle booleans
    configured: bool = False
    activated: bool = False
    target_matched: bool = False
    triggered: bool = False
    applied: bool = False
    manifested: bool = False
    recovered: bool | None = None   # None = not yet assessed

    # Structured evidence
    evidence: dict = field(default_factory=dict)
    # e.g. {"method": "proxy_interception",
    #        "expected": "timeout after 3000 ms",
    #        "observed": "TimeoutError after 3007 ms"}

    # Canonical comparison (for mutation faults)
    original_value: Any = None
    mutated_value: Any = None
    delivered_value: Any = None

    # Timestamps (ISO-8601)
    activated_at: str = ""
    triggered_at: str = ""
    applied_at: str = ""
    recovered_at: str = ""

    def to_dict(self) -> dict:
        return {
            "fault_id": self.fault_id,
            "fault_type": self.fault_type,
            "fault_class": self.fault_class,
            "layer": self.layer,
            "target": self.target,
            "configured": self.configured,
            "activated": self.activated,
            "target_matched": self.target_matched,
            "triggered": self.triggered,
            "applied": self.applied,
            "manifested": self.manifested,
            "recovered": self.recovered,
            "evidence": self.evidence,
            "original_value": self.original_value,
            "mutated_value": self.mutated_value,
            "delivered_value": self.delivered_value,
            "activated_at": self.activated_at,
            "triggered_at": self.triggered_at,
            "applied_at": self.applied_at,
            "recovered_at": self.recovered_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FaultLifecycleRecord":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# ExperimentPlan — the canonical IR
# ---------------------------------------------------------------------------

@dataclass
class ExperimentPlan:
    """Canonical intermediate representation for one CJ experiment.

    Every authoring interface (Python, YAML, decorator, context manager)
    must produce an equivalent ``ExperimentPlan`` for the same logical
    experiment.  The plan is:

    * Validated before execution.
    * Serialised alongside results as ``resolved_plan.json``.
    * Hashed (``plan_hash``) for reproducibility tracking.
    """

    scenario: ScenarioSpec
    target: TargetSpec = field(default_factory=TargetSpec)
    trigger: TriggerSpec = field(default_factory=TriggerSpec)
    hypothesis: HypothesisSpec = field(default_factory=HypothesisSpec)
    observations: ObservationSpec = field(default_factory=ObservationSpec)
    safety: SafetySpec = field(default_factory=SafetySpec)
    cleanup: CleanupSpec = field(default_factory=CleanupSpec)

    # Metadata
    schema_version: str = SCHEMA_VERSION
    plan_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    experiment_id: str = ""
    description: str = ""
    duration_s: float | None = None

    # Provenance (populated at serialisation time)
    plan_hash: str = ""
    cj_version: str = ""
    git_commit: str = ""
    source: str = ""     # "python" | "yaml" | "decorator" | "context_manager"

    # ── Validation ────────────────────────────────────────────────────────

    def validate(self) -> "ExperimentPlan":
        """Validate the plan; raises ``ValueError`` with a descriptive message."""
        errors: list[str] = []

        if not self.scenario.name:
            errors.append("scenario.name must not be empty")

        for i, f in enumerate(self.scenario.faults):
            if not f.fault_class:
                errors.append(f"scenario.faults[{i}].fault_class must not be empty")
            if f.layer not in {"llm", "network", "process", "storage", "state",
                               "resource", "gpu", "skill", "gateway"}:
                errors.append(
                    f"scenario.faults[{i}].layer={f.layer!r} is not a recognised layer"
                )

        if self.target.kind not in {"local", "ssh", "http"}:
            errors.append(f"target.kind={self.target.kind!r} must be local | ssh | http")

        if self.target.kind == "ssh" and not self.target.host:
            errors.append("target.host is required for ssh targets")

        if self.target.kind == "http" and not self.target.url:
            errors.append("target.url is required for http targets")

        if self.trigger.mode not in {"immediate", "on_event", "scheduled"}:
            errors.append(f"trigger.mode={self.trigger.mode!r} must be immediate | on_event | scheduled")

        if self.safety.max_duration_s <= 0:
            errors.append("safety.max_duration_s must be > 0")

        if self.safety.max_injections <= 0:
            errors.append("safety.max_injections must be > 0")

        if errors:
            raise ValueError("ExperimentPlan validation failed:\n" + "\n".join(f"  • {e}" for e in errors))

        return self

    # ── Serialisation ─────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Return a JSON-safe dict (does not include non-stable fields in hash)."""
        d = {
            "schema_version": self.schema_version,
            "plan_id": self.plan_id,
            "experiment_id": self.experiment_id,
            "description": self.description,
            "duration_s": self.duration_s,
            "source": self.source,
            "scenario": {
                "name": self.scenario.name,
                "faults": [
                    {"fault_class": f.fault_class, "parameters": f.parameters, "layer": f.layer}
                    for f in self.scenario.faults
                ],
                "conflict_policy": self.scenario.conflict_policy,
            },
            "target": {
                "kind": self.target.kind,
                "host": self.target.host,
                "user": self.target.user,
                "port": self.target.port,
                "url": self.target.url,
            },
            "trigger": {
                "mode": self.trigger.mode,
                "event_type": self.trigger.event_type,
                "delay_s": self.trigger.delay_s,
            },
            "hypothesis": {
                "statement": self.hypothesis.statement,
                "metric": self.hypothesis.metric,
                "condition": self.hypothesis.condition,
                "threshold": self.hypothesis.threshold,
                "direction": self.hypothesis.direction,
            },
            "observations": {
                "metrics": self.observations.metrics,
                "evidence_method": self.observations.evidence_method,
                "capture_events": self.observations.capture_events,
                "streaming_secondary": self.observations.streaming_secondary,
            },
            "safety": {
                "max_duration_s": self.safety.max_duration_s,
                "emergency_stop_on_failure": self.safety.emergency_stop_on_failure,
                "max_injections": self.safety.max_injections,
                "budget_limit_usd": self.safety.budget_limit_usd,
            },
            "cleanup": {
                "restore_state": self.cleanup.restore_state,
                "verify_recovery": self.cleanup.verify_recovery,
                "timeout_s": self.cleanup.timeout_s,
                "on_failure": self.cleanup.on_failure,
            },
            "plan_hash": self.plan_hash,
            "cj_version": self.cj_version,
            "git_commit": self.git_commit,
        }
        return d

    def _stable_dict(self) -> dict:
        """Dict used for hashing — excludes plan_id, plan_hash, timestamps."""
        d = self.to_dict()
        for key in ("plan_id", "plan_hash", "cj_version", "git_commit"):
            d.pop(key, None)
        return d

    def compute_hash(self) -> str:
        """Compute and store the SHA-256 plan hash; return it."""
        canonical = json.dumps(self._stable_dict(), sort_keys=True, separators=(",", ":"))
        self.plan_hash = hashlib.sha256(canonical.encode()).hexdigest()
        return self.plan_hash

    def save(self, path: str) -> None:
        """Write the plan as ``resolved_plan.json`` at *path*."""
        import os
        self.compute_hash()
        self._fill_provenance()
        with open(path, "w") as fh:
            json.dump(self.to_dict(), fh, indent=2)

    def _fill_provenance(self) -> None:
        try:
            import chaos_jungle
            self.cj_version = getattr(chaos_jungle, "__version__", "unknown")
        except Exception:
            pass
        if not self.git_commit:
            try:
                import subprocess, pathlib
                r = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(pathlib.Path(__file__).parent),
                    capture_output=True, text=True, timeout=3,
                )
                self.git_commit = r.stdout.strip()[:12]
            except Exception:
                self.git_commit = "unknown"

    @classmethod
    def from_dict(cls, d: dict) -> "ExperimentPlan":
        scenario_d = d.get("scenario", {})
        faults = [
            FaultSpec(
                fault_class=f["fault_class"],
                parameters=f.get("parameters", {}),
                layer=f.get("layer", "llm"),
            )
            for f in scenario_d.get("faults", [])
        ]
        scenario = ScenarioSpec(
            name=scenario_d.get("name", ""),
            faults=faults,
            conflict_policy=scenario_d.get("conflict_policy", "raise"),
        )
        target_d = d.get("target", {})
        target = TargetSpec(
            kind=target_d.get("kind", "local"),
            host=target_d.get("host", ""),
            user=target_d.get("user", ""),
            port=target_d.get("port", 0),
            url=target_d.get("url", ""),
        )
        trigger_d = d.get("trigger", {})
        trigger = TriggerSpec(
            mode=trigger_d.get("mode", "immediate"),
            event_type=trigger_d.get("event_type", ""),
            delay_s=trigger_d.get("delay_s", 0.0),
        )
        hyp_d = d.get("hypothesis", {})
        hypothesis = HypothesisSpec(
            statement=hyp_d.get("statement", ""),
            metric=hyp_d.get("metric", ""),
            condition=hyp_d.get("condition", ""),
            threshold=hyp_d.get("threshold", 0.0),
            direction=hyp_d.get("direction", "below"),
        )
        obs_d = d.get("observations", {})
        observations = ObservationSpec(
            metrics=obs_d.get("metrics", ["latency_ms", "success_rate"]),
            evidence_method=obs_d.get("evidence_method", "proxy_interception"),
            capture_events=obs_d.get("capture_events", True),
            streaming_secondary=obs_d.get("streaming_secondary", True),
        )
        safety_d = d.get("safety", {})
        safety = SafetySpec(
            max_duration_s=safety_d.get("max_duration_s", 300.0),
            emergency_stop_on_failure=safety_d.get("emergency_stop_on_failure", True),
            max_injections=safety_d.get("max_injections", 1),
            budget_limit_usd=safety_d.get("budget_limit_usd", 1.0),
        )
        cleanup_d = d.get("cleanup", {})
        cleanup = CleanupSpec(
            restore_state=cleanup_d.get("restore_state", True),
            verify_recovery=cleanup_d.get("verify_recovery", True),
            timeout_s=cleanup_d.get("timeout_s", 30.0),
            on_failure=cleanup_d.get("on_failure", "abort"),
        )
        return cls(
            scenario=scenario,
            target=target,
            trigger=trigger,
            hypothesis=hypothesis,
            observations=observations,
            safety=safety,
            cleanup=cleanup,
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            plan_id=d.get("plan_id", str(uuid.uuid4())),
            experiment_id=d.get("experiment_id", ""),
            description=d.get("description", ""),
            duration_s=d.get("duration_s"),
            plan_hash=d.get("plan_hash", ""),
            cj_version=d.get("cj_version", ""),
            git_commit=d.get("git_commit", ""),
            source=d.get("source", ""),
        )

    # ── Factory helpers ───────────────────────────────────────────────────

    @classmethod
    def from_scenario(
        cls,
        scenario: "Scenario",  # chaos_jungle.core.scenario.Scenario
        target: "Target | None" = None,
        duration: "float | str | None" = None,
        source: str = "python",
    ) -> "ExperimentPlan":
        """Build a plan from a ``Scenario`` + ``Target`` pair.

        This is the compile path for both explicit Python code and the
        context-manager / decorator interfaces.
        """
        from chaos_jungle.core._duration import parse_duration

        faults = [
            FaultSpec(
                fault_class=f.__class__.__name__,
                parameters=f._parameters() if hasattr(f, "_parameters") else {},
                layer=getattr(f, "category", "llm"),
            )
            for f in scenario.faults
        ]
        scenario_spec = ScenarioSpec(name=scenario.name, faults=faults)

        target_spec = TargetSpec()
        if target is not None:
            from chaos_jungle.targets.local import LocalTarget
            from chaos_jungle.targets.ssh import SSHTarget
            from chaos_jungle.targets.http import HTTPTarget
            if isinstance(target, SSHTarget):
                target_spec = TargetSpec(
                    kind="ssh",
                    host=getattr(target, "host", ""),
                    user=getattr(target, "user", ""),
                    port=getattr(target, "port", 22),
                )
            elif isinstance(target, HTTPTarget):
                target_spec = TargetSpec(
                    kind="http",
                    url=getattr(target, "base_url", ""),
                )
            else:
                target_spec = TargetSpec(kind="local")

        duration_s = None
        if duration is not None:
            try:
                duration_s = float(parse_duration(duration))
            except Exception:
                duration_s = float(duration) if isinstance(duration, (int, float)) else None

        plan = cls(
            scenario=scenario_spec,
            target=target_spec,
            safety=SafetySpec(max_duration_s=duration_s or 300.0),
            duration_s=duration_s,
            source=source,
        )
        plan.compute_hash()
        return plan

    @classmethod
    def from_yaml_entry(
        cls,
        entry: dict,
        suite_defaults: "dict | None" = None,
        source: str = "yaml",
    ) -> "ExperimentPlan":
        """Build a plan from one YAML experiment entry dict.

        *entry* is one item from the ``experiments`` list in a suite YAML.
        *suite_defaults* carries top-level keys (duration, conflict, etc.).
        """
        from chaos_jungle.config import ConfigLoader

        defaults = suite_defaults or {}
        name = entry.get("name", "")
        target_str = entry.get("target", "local")
        duration = entry.get("duration") or defaults.get("duration")
        conflict = entry.get("conflict") or defaults.get("conflict", "raise")

        faults = []
        for fault_dict in entry.get("faults", []):
            fault_obj = ConfigLoader.build_fault(dict(fault_dict))
            faults.append(FaultSpec(
                fault_class=fault_obj.__class__.__name__,
                parameters=fault_obj._parameters() if hasattr(fault_obj, "_parameters") else {},
                layer=getattr(fault_obj, "category", "llm"),
            ))

        from chaos_jungle.targets.local import LocalTarget
        target_obj = ConfigLoader.build_target(target_str)

        from chaos_jungle.core.scenario import Scenario
        scenario_obj = Scenario(name, [])  # faults already extracted above

        scenario_spec = ScenarioSpec(name=name, faults=faults, conflict_policy=conflict)

        target_spec = TargetSpec()
        from chaos_jungle.targets.ssh import SSHTarget
        from chaos_jungle.targets.http import HTTPTarget
        if isinstance(target_obj, SSHTarget):
            target_spec = TargetSpec(
                kind="ssh",
                host=getattr(target_obj, "host", ""),
                user=getattr(target_obj, "user", ""),
                port=getattr(target_obj, "port", 22),
            )
        elif isinstance(target_obj, HTTPTarget):
            target_spec = TargetSpec(kind="http", url=getattr(target_obj, "base_url", ""))
        else:
            target_spec = TargetSpec(kind="local")

        from chaos_jungle.core._duration import parse_duration
        duration_s = None
        if duration is not None:
            try:
                duration_s = float(parse_duration(str(duration)))
            except Exception:
                pass

        plan = cls(
            scenario=scenario_spec,
            target=target_spec,
            safety=SafetySpec(max_duration_s=duration_s or 300.0),
            duration_s=duration_s,
            source=source,
        )
        plan.compute_hash()
        return plan
