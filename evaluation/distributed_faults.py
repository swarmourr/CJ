"""Fault helpers for distributed reference multi-agent experiments."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


COMMUNICATION_FAULTS = {
    "message_delay",
    "message_drop",
    "message_duplication",
    "message_corruption",
    "message_misrouting",
}

ROLE_LOCAL_FAULTS = {
    "role_container_kill",
    "role_cpu_pressure",
    "role_memory_pressure",
}

SUPPORTED_DISTRIBUTED_FAULTS = COMMUNICATION_FAULTS | ROLE_LOCAL_FAULTS


@dataclass
class DistributedFaultPlan:
    """A deterministic fault plan for one distributed execution."""

    fault_type: str = "none"
    target_role: str = ""
    from_role: str = ""
    to_role: str = ""
    severity: str = "medium"
    parameters: dict[str, Any] = field(default_factory=dict)
    applied: bool = False
    evidence: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> "DistributedFaultPlan":
        raw = dict(config or {})
        fault_type = str(raw.get("fault_type") or raw.get("type") or "none")
        if fault_type in {"", "none"}:
            return cls()
        if fault_type not in SUPPORTED_DISTRIBUTED_FAULTS:
            raise ValueError(f"unsupported distributed fault: {fault_type!r}")
        severity = str(raw.get("severity") or "medium")
        params = dict(raw.get("parameters") or {})
        params.update({k: v for k, v in raw.items() if k not in {
            "fault_type", "type", "target_role", "from_role", "to_role",
            "severity", "parameters",
        }})
        return cls(
            fault_type=fault_type,
            target_role=str(raw.get("target_role") or params.get("target_role") or ""),
            from_role=str(raw.get("from_role") or params.get("from_role") or ""),
            to_role=str(raw.get("to_role") or params.get("to_role") or ""),
            severity=severity,
            parameters=params,
        )

    def is_active(self) -> bool:
        return self.fault_type not in {"", "none"}

    def is_communication(self) -> bool:
        return self.fault_type in COMMUNICATION_FAULTS

    def is_role_local(self) -> bool:
        return self.fault_type in ROLE_LOCAL_FAULTS

    def matches_handoff(self, from_role: str, to_role: str) -> bool:
        if not self.is_communication() or self.applied:
            return False
        if self.from_role and self.from_role != from_role:
            return False
        if self.to_role and self.to_role != to_role:
            return False
        return True

    def matches_role(self, role: str) -> bool:
        if not self.is_role_local() or self.applied:
            return False
        return bool(self.target_role) and self.target_role == role

    def as_record_parameters(self) -> dict[str, Any]:
        return {
            "fault_type": self.fault_type,
            "fault_layer": self.layer(),
            "target_role": self.target_role,
            "from_role": self.from_role,
            "to_role": self.to_role,
            "severity": self.severity,
            **self.parameters,
        }

    def layer(self) -> str:
        if self.is_communication():
            return "communication"
        if self.is_role_local():
            return "distributed_role_infrastructure"
        return ""

    def record_evidence(self, event: dict[str, Any]) -> None:
        self.evidence.append(event)


def severity_delay_s(severity: str, parameters: dict[str, Any]) -> float:
    if "delay_s" in parameters:
        return float(parameters["delay_s"])
    return {
        "low": 0.25,
        "medium": 1.0,
        "high": 3.0,
    }.get(severity, 1.0)


def role_pressure_duration_s(severity: str, parameters: dict[str, Any]) -> float:
    if "duration_s" in parameters:
        return float(parameters["duration_s"])
    return {
        "low": 2.0,
        "medium": 5.0,
        "high": 10.0,
    }.get(severity, 5.0)


def role_memory_bytes(severity: str, parameters: dict[str, Any]) -> str:
    if "memory_bytes" in parameters:
        return str(parameters["memory_bytes"])
    return {
        "low": "64M",
        "medium": "256M",
        "high": "512M",
    }.get(severity, "256M")


def apply_communication_fault(
    *,
    plan: DistributedFaultPlan,
    content: str,
    from_role: str,
    to_role: str,
) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    """Return deliveries and trace events for one host-mediated handoff.

    Deliveries are ``(recipient_role, content)`` tuples.  An empty delivery list
    means the expected handoff was actually dropped/misrouted.
    """
    if not plan.matches_handoff(from_role, to_role):
        return [(to_role, content)], []

    events: list[dict[str, Any]] = []
    plan.applied = True
    now = time.time()
    base_payload = {
        "fault_type": plan.fault_type,
        "from_role": from_role,
        "to_role": to_role,
        "severity": plan.severity,
    }

    if plan.fault_type == "message_delay":
        delay_s = severity_delay_s(plan.severity, plan.parameters)
        t0 = time.monotonic()
        time.sleep(delay_s)
        observed = time.monotonic() - t0
        event = {
            "event_type": "message_delay",
            "agent_role": to_role,
            "timestamp": now,
            "payload": {
                **base_payload,
                "configured_delay_s": delay_s,
                "observed_delay_s": observed,
            },
        }
        events.append(event)
        plan.record_evidence(event)
        return [(to_role, content)], events

    if plan.fault_type == "message_drop":
        event = {
            "event_type": "message_loss",
            "agent_role": to_role,
            "timestamp": now,
            "payload": base_payload,
        }
        events.append(event)
        plan.record_evidence(event)
        return [], events

    if plan.fault_type == "message_duplication":
        count = int(plan.parameters.get("duplicate_count", 2))
        count = max(2, min(count, 5))
        event = {
            "event_type": "message_duplication",
            "agent_role": to_role,
            "timestamp": now,
            "payload": {**base_payload, "duplicate_count": count},
        }
        events.append(event)
        plan.record_evidence(event)
        return [(to_role, content) for _ in range(count)], events

    if plan.fault_type == "message_corruption":
        replacement = str(
            plan.parameters.get("replacement")
            or "[CORRUPTED MESSAGE: content unavailable]"
        )
        event = {
            "event_type": "message_corruption",
            "agent_role": to_role,
            "timestamp": now,
            "payload": {
                **base_payload,
                "original_length": len(content),
                "mutated_length": len(replacement),
            },
        }
        events.append(event)
        plan.record_evidence(event)
        return [(to_role, replacement)], events

    if plan.fault_type == "message_misrouting":
        misroute_to = str(plan.parameters.get("misroute_to") or plan.target_role or "reviewer")
        event = {
            "event_type": "message_misrouting",
            "agent_role": misroute_to,
            "timestamp": now,
            "payload": {**base_payload, "misroute_to": misroute_to},
        }
        events.append(event)
        plan.record_evidence(event)
        if misroute_to == to_role:
            return [(to_role, content)], events
        return [(misroute_to, content)], events

    return [(to_role, content)], events
