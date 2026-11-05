"""Evidence-backed exposure verdicts, separate from session cleanup status.

This module deliberately does not infer task success from fault delivery. A
fault's activation check proves that the mechanism is ready; a valid exposure
also needs an observed trigger and an observed effect.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


EXPOSURE_SCHEMA_VERSION = 1
VERDICTS = frozenset({"valid", "untriggered", "invalid", "inconclusive"})


@dataclass(frozen=True)
class ExposureVerdict:
    fault_id: int | str
    verdict: str
    reason: str
    activated: bool | None
    triggered: bool | None
    manifested: bool | None
    recovered: bool | None = None
    policy_compliant: bool | None = None
    evidence_source: str = "none"
    observed_calls: int = 0
    matched_calls: int = 0
    affected_calls: int = 0
    untriggered_reason: str = ""
    evidence_summary: dict = field(default_factory=dict)
    proxy_call_ids_preview: list[int] = field(default_factory=list)
    schema_version: int = EXPOSURE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _proxy_entries(calls: list[dict], fault_type: str) -> list[dict]:
    entries: list[dict] = []
    for call in calls:
        try:
            parsed = json.loads(call.get("fault_evidence_json") or "[]")
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, list):
            entries.extend(
                entry for entry in parsed
                if isinstance(entry, dict) and entry.get("fault_type") == fault_type
            )
    return entries


def classify_fault_exposure(
    fault: dict,
    *,
    proxy_calls: list[dict] | None = None,
    observation: dict | None = None,
    policy_compliant: bool | None = None,
    ambiguous_proxy_type: bool = False,
) -> ExposureVerdict:
    """Classify one fault using full proxy records or a target-side observation.

    ``observation`` may contain ``triggered``, ``manifested``,
    ``event_reached``, and ``untriggered_reason``. An absent value is unknown,
    never silently interpreted as false. Explicit observations take precedence
    over proxy inference, allowing workload-specific reachability checks.
    """
    fault_id = fault.get("id", "")
    status = str(fault.get("status") or "")
    activated = (
        False if status == "injection_failed" else
        True if fault.get("verified_active") else None
    )
    recovered = (
        False if status == "revert_failed" else
        True if fault.get("verified_recovered") else None
    )
    params = _json_object(fault.get("parameters"))
    fault_type = str(params.get("fault") or "")
    calls = proxy_calls or []
    entries = _proxy_entries(calls, fault_type) if fault_type else []
    observed_calls = len(calls) if fault_type else 0
    matched_calls = sum(entry.get("target_matched") is True for entry in entries)
    affected_calls = sum(entry.get("triggered") is True for entry in entries)
    observation = observation or {}
    explicit = any(observation.get(key) is not None for key in
                   ("triggered", "manifested", "event_reached"))
    source = "target_observation" if explicit else "proxy" if fault_type else "none"

    triggered = observation.get("triggered") if "triggered" in observation else None
    manifested = observation.get("manifested") if "manifested" in observation else None
    if triggered is None and entries:
        triggered = any(entry.get("triggered") is True for entry in entries)
    if manifested is None and entries and triggered is True:
        values = [entry.get("manifested") for entry in entries if entry.get("triggered") is True]
        manifested = True if True in values else False if values and all(v is False for v in values) else None
    if manifested is None and triggered is False:
        manifested = False

    untriggered_reason = str(observation.get("untriggered_reason") or "")
    if not untriggered_reason and triggered is False and entries:
        if matched_calls:
            untriggered_reason = "selection_not_fired"
        elif any(entry.get("selector_matched") is True for entry in entries):
            untriggered_reason = "operation_not_reached"
        elif all(entry.get("selector_matched") is False for entry in entries):
            untriggered_reason = "selected_request_not_observed"
        else:
            untriggered_reason = "target_not_observed"

    if policy_compliant is False:
        verdict, reason = "invalid", "safety policy failed"
    elif activated is False:
        verdict, reason = "invalid", "fault activation failed"
    elif activated is None:
        verdict, reason = "inconclusive", "fault activation was not verified"
    elif policy_compliant is None:
        verdict, reason = "inconclusive", "safety policy compliance was not established"
    elif ambiguous_proxy_type and not explicit:
        verdict, reason = "inconclusive", "multiple faults share one proxy type; call attribution is ambiguous"
    elif explicit and not observation.get("evidence"):
        verdict, reason = "inconclusive", "target observation lacks supporting evidence"
    elif triggered is True and manifested is True:
        verdict, reason = "valid", "trigger and effect observed"
    elif triggered is True and manifested is False:
        verdict, reason = "invalid", "target reached but requested effect was not observed"
    elif triggered is False:
        coverage = explicit and observation.get("event_reached") is False
        if entries or coverage:
            verdict, reason = "untriggered", untriggered_reason or "selected event was not reached"
        else:
            verdict, reason = "inconclusive", "no evidence that the selected event was observable"
    else:
        verdict, reason = "inconclusive", "trigger or manifestation evidence is missing"

    return ExposureVerdict(
        fault_id=fault_id, verdict=verdict, reason=reason,
        activated=activated, triggered=triggered, manifested=manifested,
        recovered=recovered, policy_compliant=policy_compliant,
        evidence_source=source, observed_calls=observed_calls,
        matched_calls=matched_calls, affected_calls=affected_calls,
        untriggered_reason=untriggered_reason,
        evidence_summary=observation.get("evidence") or {},
        proxy_call_ids_preview=[call["id"] for call in calls[:20]
                                if isinstance(call.get("id"), int)],
    )


def aggregate_exposure(verdicts: list[ExposureVerdict]) -> str:
    """A group/session is valid only if every member has verified exposure."""
    values = {item.verdict for item in verdicts}
    if not values or "inconclusive" in values:
        return "inconclusive" if "invalid" not in values else "invalid"
    if "invalid" in values:
        return "invalid"
    if "untriggered" in values:
        return "untriggered"
    return "valid"
