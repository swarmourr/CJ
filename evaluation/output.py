"""Output: JSONL loader, CSV writer, LaTeX table generator.

Reads ``runs.jsonl`` and produces:
  - task_results.csv
  - condition_summary.csv
  - validity_summary.csv
  - group_summary.csv (placeholder when no group data)
  - paper tables as LaTeX snippets
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
import os
import re
from typing import Any

from evaluation.analysis.metrics import AggregationMetrics, compute_metrics
from evaluation.analysis.validity import classify_records
from evaluation.schema import OUTPUT_SCHEMA_STATUS, OUTPUT_SCHEMA_VERSION


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool_success(record: dict) -> int:
    return 1 if _as_float(record.get("success"), 0.0) >= 0.5 else 0


def _as_bool_field(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no", ""}:
            return False
    return default


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    n = len(vals)
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def _ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def _json_or(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _condition_index(records: list[dict]) -> dict[str, dict[str, dict]]:
    by_pair: dict[str, dict[str, dict]] = {}
    for record in records:
        pair_id = record.get("pair_id")
        condition = record.get("condition")
        if not pair_id or not condition:
            continue
        by_pair.setdefault(str(pair_id), {})[str(condition)] = record
    return by_pair


def _fault_records(records: list[dict]) -> list[dict]:
    return [
        r for r in records
        if r.get("condition") == "cj_fault"
        or (r.get("phase") == "fault" and r.get("fault_type") not in ("", "none", "passthrough"))
    ]


def _severity_label(record: dict) -> str:
    params = record.get("fault_parameters") if isinstance(record.get("fault_parameters"), dict) else {}
    for key in ("severity", "delay_s", "timeout_s", "max_tokens", "mode", "n"):
        if key in params:
            return f"{key}={params[key]}"
    if params:
        return json.dumps(params, sort_keys=True)
    return ""


def _science_key(record: dict) -> tuple:
    return (
        record.get("agent_level", ""),
        record.get("agent_system", ""),
        record.get("topology", ""),
        record.get("benchmark", ""),
        record.get("model", ""),
        record.get("fault_type", "none"),
        _severity_label(record),
    )


def _ci_from_triplet_diffs(
    triplets: list[tuple[dict, dict, dict]],
    diff_fn,
    *,
    iterations: int = 2000,
    seed: int = 0,
) -> dict[str, Any]:
    clusters: dict[str, list[float]] = {}
    for base, control, fault in triplets:
        task_id = str(fault.get("task_id") or base.get("task_id") or fault.get("pair_id"))
        clusters.setdefault(task_id, []).append(float(diff_fn(base, control, fault)))
    task_ids = sorted(clusters)
    values = [v for vals in clusters.values() for v in vals]
    if not values:
        return {"mean": None, "ci_low": None, "ci_high": None, "n_tasks": 0}
    observed = sum(values) / len(values)
    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(iterations):
        picked = [rng.choice(task_ids) for _ in task_ids]
        vals = [v for task_id in picked for v in clusters[task_id]]
        samples.append(sum(vals) / len(vals))
    samples.sort()
    low_idx = max(0, int(0.025 * iterations) - 1)
    high_idx = min(iterations - 1, int(0.975 * iterations) - 1)
    return {
        "mean": observed,
        "ci_low": samples[low_idx],
        "ci_high": samples[high_idx],
        "n_tasks": len(task_ids),
    }


def _lifecycle(record: dict) -> dict:
    lifecycle = record.get("lifecycle")
    return lifecycle if isinstance(lifecycle, dict) else {}


def _evidence_entries(record: dict) -> list[dict]:
    evidence = _lifecycle(record).get("evidence")
    if not isinstance(evidence, dict):
        return []
    raw_items = evidence.get("fault_evidence") or []
    entries: list[dict] = []
    if isinstance(raw_items, (str, dict)):
        raw_items = [raw_items]
    for item in raw_items:
        parsed = _json_or(item, {})
        if isinstance(parsed, dict):
            # Old proxy evidence can be a fault->quantity mapping.
            if {"fault_id", "fault_type", "target", "evidence"} & set(parsed):
                entries.append(parsed)
            else:
                for fault_type, ev in parsed.items():
                    if isinstance(ev, dict):
                        entries.append({"fault_type": fault_type, "evidence": ev})
                    else:
                        entries.append({"fault_type": fault_type, "observed": ev})
        elif isinstance(parsed, list):
            entries.extend(x for x in parsed if isinstance(x, dict))
    return entries


_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|authorization|bearer)\s*[:=]\s*['\"]?[^,'\"\s}]+"),
]


def _redacted_preview(text: str, *, preview_chars: int = 160) -> str:
    preview = text[:preview_chars]
    for pattern in _SECRET_PATTERNS:
        preview = pattern.sub("[REDACTED]", preview)
    return preview


def _text_summary(value: Any, *, preview_chars: int = 160) -> dict[str, Any]:
    text = "" if value is None else str(value)
    return {
        "sha256": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
        "length": len(text),
        "preview": _redacted_preview(text, preview_chars=preview_chars),
    }


def _sanitize_cj_value(value: Any, *, key: str = "") -> Any:
    """Preserve CJ evidence while replacing bulky/sensitive text with summaries."""
    key_l = key.lower()
    text_like_key = (
        key_l in {
            "prompt_text", "response_text", "full_messages_json",
            "original_value", "mutated_value", "delivered_value",
            "original_canonical", "mutated_canonical",
        }
        or key_l.endswith("_canonical")
        or key_l.endswith("_text")
    )
    if isinstance(value, str):
        parsed = _json_or(value, None)
        if isinstance(parsed, (dict, list)) and not text_like_key:
            return _sanitize_cj_value(parsed, key=key)
        if text_like_key or len(value) > 300:
            return _text_summary(value)
        return _redacted_preview(value, preview_chars=300)
    if isinstance(value, dict):
        return {
            str(k): _sanitize_cj_value(v, key=str(k))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_cj_value(item, key=key) for item in value]
    return value


def _sanitize_cj_evidence(evidence: Any) -> Any:
    return _sanitize_cj_value(evidence)


def _json_cell(value: Any) -> str:
    if value in (None, ""):
        return ""
    return json.dumps(value, sort_keys=True, default=str)


def _sanitized_json_cell(value: Any) -> str:
    if value in (None, ""):
        return ""
    return _json_cell(_sanitize_cj_value(value))


def _text_summary_cell(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, dict) and {"sha256", "length", "preview"} <= set(value):
        safe = dict(value)
        safe["preview"] = _redacted_preview(str(safe.get("preview", "")))
        return _json_cell(safe)
    return _json_cell(_text_summary(value))


def _proxy_calls(record: dict) -> list[dict]:
    evidence = _lifecycle(record).get("evidence")
    if not isinstance(evidence, dict):
        return []
    calls = evidence.get("proxy_calls") or []
    if isinstance(calls, dict):
        calls = [calls]
    if isinstance(calls, str):
        parsed = _json_or(calls, [])
        calls = parsed if isinstance(parsed, list) else []
    return [call for call in calls if isinstance(call, dict)]


def _matching_proxy_call(entry: dict | None, calls: list[dict], fallback_index: int) -> dict:
    if not calls:
        return {}
    call_index = None
    if isinstance(entry, dict):
        target = entry.get("target") if isinstance(entry.get("target"), dict) else {}
        call_index = target.get("call_index")
    if call_index is not None:
        for call in calls:
            if str(call.get("call_index")) == str(call_index):
                return call
    if fallback_index < len(calls):
        return calls[fallback_index]
    return {}


def _proxy_call_columns(call: dict) -> dict[str, Any]:
    triggered = _json_or(call.get("triggered_faults_json", "[]"), [])
    configured = _json_or(call.get("configured_faults_json", "[]"), [])
    evidence = _json_or(call.get("fault_evidence_json", "{}"), {})
    return {
        "proxy_call_id": call.get("id", ""),
        "proxy_phase": call.get("phase", ""),
        "proxy_call_index": call.get("call_index", ""),
        "proxy_run_id": call.get("run_id", ""),
        "proxy_agent_role": call.get("agent_role", ""),
        "proxy_step": call.get("step", ""),
        "proxy_timestamp": call.get("timestamp", ""),
        "proxy_model": call.get("model", ""),
        "proxy_latency_s": call.get("latency_s", ""),
        "proxy_http_status": call.get("http_status", ""),
        "proxy_fault_name": call.get("fault_name", ""),
        "proxy_was_blocked": call.get("was_blocked", ""),
        "proxy_was_modified": call.get("was_modified", ""),
        "proxy_fault_triggered": call.get("fault_triggered", ""),
        "proxy_fault_offset_s": call.get("fault_offset_s", ""),
        "proxy_request_size_bytes": call.get("request_size_bytes", ""),
        "proxy_response_size_bytes": call.get("response_size_bytes", ""),
        "proxy_response_length_chars": call.get("response_length_chars", ""),
        "proxy_prompt_tokens": call.get("prompt_tokens", ""),
        "proxy_completion_tokens": call.get("completion_tokens", ""),
        "proxy_total_tokens": call.get("total_tokens", ""),
        "proxy_max_tokens_requested": call.get("max_tokens_requested", ""),
        "proxy_message_count": call.get("message_count", ""),
        "proxy_tool_count": call.get("tool_count", ""),
        "proxy_response_tool_calls": call.get("response_tool_calls", ""),
        "proxy_is_retry": call.get("is_retry", ""),
        "proxy_agent_addr": call.get("agent_addr", ""),
        "proxy_configured_faults_json": _json_cell(_sanitize_cj_value(configured)),
        "proxy_triggered_faults_json": _json_cell(_sanitize_cj_value(triggered)),
        "proxy_fault_evidence_json": _json_cell(_sanitize_cj_value(evidence)),
        "proxy_prompt_text_summary_json": _text_summary_cell(call.get("prompt_text", "")),
        "proxy_response_text_summary_json": _text_summary_cell(call.get("response_text", "")),
        "proxy_full_messages_summary_json": _text_summary_cell(call.get("full_messages_json", "")),
    }


def _blank_proxy_call_columns() -> dict[str, Any]:
    return {key: "" for key in _proxy_call_columns({})}


def _affected_call_count(record: dict) -> int:
    if record.get("condition") == "cj_control" or record.get("fault_type") in {
        "", "none", "passthrough",
    }:
        return 0
    entries = _evidence_entries(record)
    if entries:
        return sum(
            1 for entry in entries
            if entry.get("triggered", True) is not False
            and entry.get("applied", True) is not False
        )
    evidence = _lifecycle(record).get("evidence")
    if isinstance(evidence, dict):
        triggered = evidence.get("triggered_faults") or []
        count = 0
        for item in triggered:
            parsed = _json_or(item, [])
            if isinstance(parsed, list) and parsed:
                count += 1
        if count:
            return count
    return int(_lifecycle(record).get("proxy_call_count") or 0)


def _call_accounting(record: dict) -> dict[str, int]:
    """Separate agent-reported calls from CJ proxy-observed calls."""
    agent_reported = int(_as_float(record.get("llm_calls"), 0.0))
    intercepted = int(_lifecycle(record).get("proxy_call_count") or 0)
    affected = _affected_call_count(record)
    return {
        "agent_reported_llm_calls": agent_reported,
        "proxy_intercepted_calls": intercepted,
        "llm_calls_attempted": max(agent_reported, intercepted),
        "llm_calls_completed": agent_reported,
        "proxy_calls_intercepted": intercepted,
        "proxy_calls_affected": affected,
    }


def _success_verdicts(record: dict) -> dict[str, str]:
    """Keep executor, scorer, and task correctness success separate."""
    executor_status = str(record.get("executor_status", "ok")).lower()
    executor_exit_code = int(_as_float(record.get("executor_exit_code"), 0.0))
    executor_ok = (
        record.get("executor_timed_out") is not True
        and executor_exit_code == 0
        and executor_status in {"", "ok"}
        and not record.get("executor_error")
    )

    scorer_status = str(record.get("scorer_status", "")).lower()
    tests_total = int(_as_float(record.get("tests_total"), 0.0))
    if scorer_status in {"", "not_available", "missing", "skipped"}:
        scorer_verdict = "not_available"
    elif scorer_status == "ok" and tests_total > 0:
        scorer_verdict = "success"
    else:
        scorer_verdict = "failure"

    return {
        "executor_success_verdict": "success" if executor_ok else "failure",
        "scorer_success_verdict": scorer_verdict,
        "task_success_verdict": "success" if _as_bool_success(record) else "failure",
    }


def _run_failure_verdicts(record: dict) -> dict[str, Any]:
    success = _success_verdicts(record)
    agent_detected = _agent_detected_fault(record)
    runner_observed, runner_source = _runner_observed_failure(record)
    reported_error = _as_float(record.get("reported_error"), 0.0) >= 0.5
    operational_success = _agent_operational_success(record)
    if agent_detected and runner_observed:
        source = f"agent_and_{runner_source}"
    elif agent_detected:
        source = "agent"
    elif runner_observed:
        source = runner_source
    elif reported_error:
        source = "reported_error"
    else:
        source = "none"
    return {
        **success,
        "operational_success": operational_success,
        "continued_operation": operational_success,
        "agent_detected_fault": agent_detected,
        "runner_observed_failure": runner_observed,
        "failure_detection_source": source,
        "silent_failure": bool(
            success["task_success_verdict"] == "failure"
            and operational_success
            and not agent_detected
        ),
    }


def _cj_evidence_summary(record: dict) -> dict[str, Any]:
    lifecycle = _lifecycle(record)
    evidence = lifecycle.get("evidence") if isinstance(lifecycle.get("evidence"), dict) else {}
    accounting = _call_accounting(record)
    proxy_calls = _proxy_calls(record)
    return {
        "cj_session_id": lifecycle.get("session_id", ""),
        "cj_evidence_source": lifecycle.get("evidence_source", record.get("lifecycle", {}).get("evidence_source", "") if isinstance(record.get("lifecycle"), dict) else ""),
        "cj_lifecycle_configured": lifecycle.get("configured", ""),
        "cj_lifecycle_activated": lifecycle.get("activated", ""),
        "cj_lifecycle_triggered": lifecycle.get("triggered", ""),
        "cj_lifecycle_manifested": lifecycle.get("manifested", ""),
        "cj_lifecycle_reverted": lifecycle.get("reverted", ""),
        "cj_lifecycle_recovered": lifecycle.get("recovered", ""),
        "cj_lifecycle_verdict": lifecycle.get("verdict", ""),
        "cj_proxy_call_count": lifecycle.get("proxy_call_count", ""),
        "agent_reported_llm_calls": accounting["agent_reported_llm_calls"],
        "proxy_intercepted_calls": accounting["proxy_intercepted_calls"],
        "cj_proxy_calls_intercepted": accounting["proxy_calls_intercepted"],
        "cj_proxy_calls_affected": accounting["proxy_calls_affected"],
        "cj_configured_faults_json": _sanitized_json_cell(evidence.get("configured_faults", [])),
        "cj_triggered_faults_json": _sanitized_json_cell(evidence.get("triggered_faults", [])),
        "cj_fault_evidence_json": _sanitized_json_cell(evidence.get("fault_evidence", [])),
        "cj_faults_json": _sanitized_json_cell(evidence.get("faults", [])),
        "cj_proxy_calls_json": _sanitized_json_cell(proxy_calls),
    }


def _validity_counts(records: list[dict]) -> dict[str, Any]:
    configured = [r for r in records if _lifecycle(r).get("configured") is True]
    activated = [r for r in configured if _lifecycle(r).get("activated") is True]
    triggered = [r for r in activated if _lifecycle(r).get("triggered") is True]
    manifested = [r for r in triggered if _lifecycle(r).get("manifested") is True]
    reverted = [r for r in activated if _lifecycle(r).get("reverted") is True]
    recovered = [r for r in reverted if _lifecycle(r).get("recovered") is True]
    return {
        "configured_count": len(configured),
        "activated_count": len(activated),
        "triggered_count": len(triggered),
        "manifested_count": len(manifested),
        "reverted_count": len(reverted),
        "recovered_count": len(recovered),
        "activation_rate": _ratio(len(activated), len(configured)),
        "trigger_rate": _ratio(len(triggered), len(activated)),
        "manifestation_rate": _ratio(len(manifested), len(triggered)),
        "reversion_rate": _ratio(len(reverted), len(activated)),
        "verified_recovery_rate": _ratio(len(recovered), len(reverted)),
        "invalid_experiment_rate": _ratio(sum(1 for r in configured if r.get("validity") == "invalid"), len(configured)),
        "inconclusive_rate": _ratio(sum(1 for r in configured if r.get("validity") == "inconclusive"), len(configured)),
        "untriggered_rate": _ratio(sum(1 for r in configured if r.get("validity") == "untriggered"), len(configured)),
    }


def _inference_status(*, unique_tasks: int, valid_pairs: int) -> str:
    if unique_tasks < 2:
        return "insufficient_unique_tasks"
    if valid_pairs < 2:
        return "insufficient_pairs"
    return "ok"


def _selectivity_metrics(records: list[dict]) -> dict[str, Any]:
    affected = 0
    targeted_affected = 0
    targeted_calls = 0
    has_target = False
    for record in records:
        role = record.get("fault_target_role")
        for entry in _evidence_entries(record):
            target = entry.get("target") if isinstance(entry.get("target"), dict) else {}
            evidence = entry.get("evidence") if isinstance(entry.get("evidence"), dict) else {}
            request_meta = evidence.get("request_meta") if isinstance(evidence.get("request_meta"), dict) else {}
            agent_role = target.get("agent_role") or request_meta.get("agent_role")
            if role:
                target_matched = agent_role == role
                has_target = True
            elif "target_matched" in entry:
                target_matched = entry.get("target_matched") is True
                has_target = True
            else:
                target_matched = False
            if target_matched:
                targeted_calls += 1
            if entry.get("applied", entry.get("triggered", True)):
                affected += 1
                if target_matched:
                    targeted_affected += 1
    if not has_target:
        return {"selectivity_precision": None, "target_coverage": None}
    return {
        "selectivity_precision": _ratio(targeted_affected, affected),
        "target_coverage": _ratio(targeted_affected, targeted_calls),
    }


def load_jsonl(path: str) -> list[dict]:
    """Load all RunRecord dicts from a JSONL file."""
    if not os.path.exists(path):
        return []
    records: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def write_task_results_csv(records: list[dict], path: str) -> None:
    """Write one row per RunRecord to task_results.csv."""
    if not records:
        print(f"[output] No records — skipping {path}")
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    cols = [
        "study_id", "run_id", "output_schema_version",
        "campaign_id", "pair_id", "condition",
        "agent_level", "topology", "fault_target_role",
        "timestamp", "cj_commit", "cj_source_version",
        "cj_source_state", "cj_source_dirty",
        "agent_system", "framework", "requested_framework", "framework_native",
        "multi_agent_impl", "benchmark", "benchmark_subset", "task_id",
        "task_difficulty", "task_complexity",
        "model", "model_digest", "endpoint_type", "config_hash",
        "seed", "fault_type", "phase", "validity",
        "executor_success_verdict", "scorer_success_verdict", "task_success_verdict",
        "operational_success", "continued_operation",
        "agent_detected_fault", "runner_observed_failure",
        "failure_detection_source", "silent_failure",
        "success", "duration_s", "reported_error", "retries",
        "llm_calls", "tool_calls", "turns",
        "agent_reported_llm_calls", "proxy_intercepted_calls",
        "llm_calls_attempted", "llm_calls_completed",
        "proxy_calls_intercepted", "proxy_calls_affected",
        "prompt_tokens", "completion_tokens", "total_tokens", "cost_usd",
        "tests_passed", "tests_total", "termination_reason",
        "lifecycle.triggered", "lifecycle.manifested", "lifecycle.reverted", "lifecycle.recovered",
        "lifecycle.activated", "lifecycle.verdict",
        "framework_version", "python_version",
        "docker_image", "docker_image_digest",
        "cj_session_id", "cj_evidence_source",
        "cj_lifecycle_configured", "cj_lifecycle_activated",
        "cj_lifecycle_triggered", "cj_lifecycle_manifested",
        "cj_lifecycle_reverted", "cj_lifecycle_recovered",
        "cj_lifecycle_verdict", "cj_proxy_call_count",
        "cj_proxy_calls_intercepted", "cj_proxy_calls_affected",
        "cj_configured_faults_json", "cj_triggered_faults_json",
        "cj_fault_evidence_json", "cj_faults_json", "cj_proxy_calls_json",
    ]

    def _flatten(r: dict) -> dict:
        out = dict(r)
        out["output_schema_version"] = r.get("output_schema_version") or OUTPUT_SCHEMA_VERSION
        lc = r.get("lifecycle") or {}
        if isinstance(lc, dict):
            out["lifecycle.triggered"]  = lc.get("triggered", "")
            out["lifecycle.manifested"] = lc.get("manifested", "")
            out["lifecycle.reverted"]   = lc.get("reverted", "")
            out["lifecycle.recovered"]  = lc.get("recovered", "")
            out["lifecycle.activated"]  = lc.get("activated", "")
            out["lifecycle.verdict"]    = lc.get("verdict", "")
        out.update(_run_failure_verdicts(r))
        out.update(_call_accounting(r))
        out.update(_cj_evidence_summary(r))
        return out

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(_flatten(r) for r in records)


def write_condition_summary_csv(
    records: list[dict],
    path: str,
) -> None:
    """Write aggregated metrics per (agent_system, benchmark, fault_type).

    Baselines (fault_type="none") are paired with each fault condition that
    shares the same (agent_system, benchmark) so that degradation is computable.
    """
    from itertools import groupby

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    # Separate baselines from fault records
    baselines  = [r for r in records if r.get("condition") == "direct_baseline"]
    fault_recs = [r for r in records if r.get("phase") == "fault"]

    # Index baselines by pair_id (exact pairing) and by (agent_system, benchmark)
    # for fallback when pair_id is absent (records from older schema).
    baseline_by_pair:  dict[str, dict] = {}
    baseline_by_ab:    dict[tuple, list[dict]] = {}
    for r in baselines:
        pid = r.get("pair_id", "")
        if pid:
            baseline_by_pair[pid] = r
        k = (r.get("agent_system", ""), r.get("benchmark", ""))
        baseline_by_ab.setdefault(k, []).append(r)

    def fault_key(r: dict):
        return (r.get("agent_system", ""), r.get("benchmark", ""), r.get("fault_type", "none"))

    rows: list[dict] = []

    # One row per (agent_system, benchmark, fault_type).
    # For each fault record, find its paired baseline via pair_id (preferred) or
    # via (agent_system, benchmark) match (fallback for records without pair_id).
    sorted_faults = sorted(fault_recs, key=fault_key)
    for (sys, bench, fault), group_recs in groupby(sorted_faults, key=fault_key):
        group_list = list(group_recs)

        paired_baselines: list[dict] = []
        for fr in group_list:
            pid = fr.get("pair_id", "")
            if pid and pid in baseline_by_pair:
                paired_baselines.append(baseline_by_pair[pid])
            # else: no exact match; baseline_by_ab fallback applied below

        # Fall back to all baselines for this (sys, bench) when pair_id is absent
        if not paired_baselines:
            paired_baselines = baseline_by_ab.get((sys, bench), [])

        combined = paired_baselines + group_list
        m   = compute_metrics(combined)
        row = {"agent_system": sys, "benchmark": bench, "fault_type": fault}
        row.update(m.to_dict())
        rows.append(row)

    # Baseline-only summary rows
    for (sys, bench), b_recs in sorted(baseline_by_ab.items()):
        m   = compute_metrics(b_recs)
        row = {"agent_system": sys, "benchmark": bench, "fault_type": "none"}
        row.update(m.to_dict())
        rows.append(row)

    if not rows:
        print(f"[output] No condition data — skipping {path}")
        return

    cols = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_cj_evidence_csv(records: list[dict], path: str) -> None:
    """Write CJ-provided lifecycle/proxy evidence per experimental execution.

    This file intentionally preserves CJ evidence as evidence, separate from
    agent-reported telemetry.  Derived summaries should prefer this table for
    injection validity and fidelity questions.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    rows: list[dict[str, Any]] = []
    for record in records:
        summary = _cj_evidence_summary(record)
        lifecycle = _lifecycle(record)
        evidence = lifecycle.get("evidence") if isinstance(lifecycle.get("evidence"), dict) else {}
        entries = _evidence_entries(record)
        proxy_calls = _proxy_calls(record)
        base = {
            "study_id": record.get("study_id", ""),
            "campaign_id": record.get("campaign_id", ""),
            "pair_id": record.get("pair_id", ""),
            "run_id": record.get("run_id", ""),
            "condition": record.get("condition", ""),
            "agent_level": record.get("agent_level", ""),
            "agent_system": record.get("agent_system", ""),
            "topology": record.get("topology", ""),
            "benchmark": record.get("benchmark", ""),
            "task_id": record.get("task_id", ""),
            "model": record.get("model", ""),
            "fault_type": record.get("fault_type", ""),
            "validity": record.get("validity", ""),
            **summary,
            "cj_lifecycle_timestamps_json": _sanitized_json_cell(lifecycle.get("timestamps", {})),
            "cj_lifecycle_details": lifecycle.get("details", ""),
            "cj_raw_evidence_json": _sanitized_json_cell(evidence),
        }
        row_count = max(len(entries), len(proxy_calls), 1)
        for idx in range(row_count):
            entry = entries[idx] if idx < len(entries) else {}
            target = entry.get("target") if isinstance(entry.get("target"), dict) else {}
            ev = entry.get("evidence") if isinstance(entry.get("evidence"), dict) else {}
            call = _matching_proxy_call(entry, proxy_calls, idx)
            rows.append({
                **base,
                **(_proxy_call_columns(call) if call else _blank_proxy_call_columns()),
                "evidence_index": idx if entry else "",
                "evidence_fault_id": entry.get("fault_id", ""),
                "evidence_fault_type": entry.get("fault_type", ""),
                "evidence_fault_class": entry.get("fault_class", ""),
                "evidence_layer": entry.get("layer", ""),
                "evidence_target_json": _sanitized_json_cell(target),
                "evidence_configured": entry.get("configured", ""),
                "evidence_activated": entry.get("activated", ""),
                "evidence_target_matched": entry.get("target_matched", ""),
                "evidence_triggered": entry.get("triggered", ""),
                "evidence_applied": entry.get("applied", ""),
                "evidence_manifested": entry.get("manifested", ""),
                "evidence_observed_json": _sanitized_json_cell(ev),
                "evidence_original_value": _text_summary_cell(entry.get("original_value")),
                "evidence_mutated_value": _text_summary_cell(entry.get("mutated_value")),
                "evidence_delivered_value": _text_summary_cell(entry.get("delivered_value")),
            })

    fields = list(rows[0].keys()) if rows else [
        "study_id", "campaign_id", "pair_id", "run_id", "condition",
        "agent_level", "agent_system", "topology", "benchmark", "task_id",
        "model", "fault_type", "validity", "cj_session_id",
        "cj_evidence_source", "cj_lifecycle_configured",
        "cj_lifecycle_activated", "cj_lifecycle_triggered",
        "cj_lifecycle_manifested", "cj_lifecycle_reverted",
        "cj_lifecycle_recovered", "cj_lifecycle_verdict",
        "cj_proxy_call_count", "cj_proxy_calls_intercepted",
        "cj_proxy_calls_affected", "agent_reported_llm_calls",
        "proxy_intercepted_calls", "cj_configured_faults_json",
        "cj_triggered_faults_json", "cj_fault_evidence_json",
        "cj_faults_json", "cj_proxy_calls_json", "cj_lifecycle_timestamps_json",
        "cj_lifecycle_details", "cj_raw_evidence_json", "evidence_index",
        "evidence_fault_id", "evidence_fault_type", "evidence_fault_class",
        "evidence_layer", "evidence_target_json", "evidence_configured",
        "evidence_activated", "evidence_target_matched",
        "evidence_triggered", "evidence_applied", "evidence_manifested",
        "evidence_observed_json", "evidence_original_value",
        "evidence_mutated_value", "evidence_delivered_value",
        *_blank_proxy_call_columns().keys(),
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _json_dump_cell(value: Any) -> str:
    if value in (None, ""):
        return ""
    return json.dumps(value, sort_keys=True, default=str)


def write_cj_overhead_summary_csv(records: list[dict], path: str) -> None:
    """Compare direct_baseline with cj_control by exact pair_id.

    Rows are stratified by system/model/topology/benchmark.  The primary
    duration overhead is ``(T_C - T_B) / T_B``; negative medians are retained as
    measurements but should be interpreted as noise, not evidence that CJ makes
    workloads faster.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    by_pair = _condition_index(records)
    grouped: dict[tuple, list[tuple[dict, dict]]] = {}
    for pair in by_pair.values():
        base = pair.get("direct_baseline")
        ctrl = pair.get("cj_control")
        if not base or not ctrl:
            continue
        key = (
            ctrl.get("agent_level", ""),
            ctrl.get("framework") or ctrl.get("agent_system", ""),
            ctrl.get("topology", ""),
            ctrl.get("benchmark", ""),
            ctrl.get("model", ""),
        )
        grouped.setdefault(key, []).append((base, ctrl))
    rows = []
    for (level, framework, topology, benchmark, model), pairs in sorted(grouped.items()):
        triplets = [(base, ctrl, ctrl) for base, ctrl in pairs]
        unique_tasks = len({base.get("task_id") or ctrl.get("task_id") or "" for base, ctrl in pairs})
        inference_status = _inference_status(unique_tasks=unique_tasks, valid_pairs=len(pairs))
        duration_rel = [
            (_as_float(ctrl.get("duration_s")) - _as_float(base.get("duration_s"))) / _as_float(base.get("duration_s"))
            for base, ctrl in pairs
            if _as_float(base.get("duration_s")) > 0
        ]
        ci = (
            _ci_from_triplet_diffs(
                triplets,
                lambda base, control, _fault: (
                    (_as_float(control.get("duration_s")) - _as_float(base.get("duration_s")))
                    / _as_float(base.get("duration_s"))
                ) if _as_float(base.get("duration_s")) > 0 else 0.0,
            )
            if inference_status == "ok"
            else {"ci_low": None, "ci_high": None}
        )
        rows.append({
            "agent_level": level,
            "framework": framework,
            "topology": topology,
            "benchmark": benchmark,
            "model": model,
            "n_pairs": len(pairs),
            "unique_tasks": unique_tasks,
            "inference_status": inference_status,
            "median_duration_overhead_ratio": _median(duration_rel),
            "mean_duration_overhead_ratio": _mean(duration_rel),
            "duration_overhead_ci_low": ci.get("ci_low"),
            "duration_overhead_ci_high": ci.get("ci_high"),
            "median_duration_overhead_s": _median([
                _as_float(ctrl.get("duration_s")) - _as_float(base.get("duration_s"))
                for base, ctrl in pairs
            ]),
            "correctness_overhead": _mean([
                _as_float(base.get("success")) - _as_float(ctrl.get("success"))
                for base, ctrl in pairs
            ]),
            "llm_call_overhead": _mean([
                _as_float(ctrl.get("llm_calls")) - _as_float(base.get("llm_calls"))
                for base, ctrl in pairs
            ]),
            "tool_call_overhead": _mean([
                _as_float(ctrl.get("tool_calls")) - _as_float(base.get("tool_calls"))
                for base, ctrl in pairs
            ]),
            "token_overhead": _mean([
                _as_float(ctrl.get("total_tokens")) - _as_float(base.get("total_tokens"))
                for base, ctrl in pairs
            ]),
            "cost_overhead_usd": _mean([
                _as_float(ctrl.get("cost_usd")) - _as_float(base.get("cost_usd"))
                for base, ctrl in pairs
            ]),
            "cpu_usage_overhead": _mean([
                _as_float(ctrl.get("cpu_usage")) - _as_float(base.get("cpu_usage"))
                for base, ctrl in pairs
                if "cpu_usage" in ctrl or "cpu_usage" in base
            ]),
            "peak_memory_overhead": _mean([
                _as_float(ctrl.get("peak_memory_bytes")) - _as_float(base.get("peak_memory_bytes"))
                for base, ctrl in pairs
                if "peak_memory_bytes" in ctrl or "peak_memory_bytes" in base
            ]),
            "network_bytes_overhead": _mean([
                _as_float(ctrl.get("network_bytes")) - _as_float(base.get("network_bytes"))
                for base, ctrl in pairs
                if "network_bytes" in ctrl or "network_bytes" in base
            ]),
        })
    fields = list(rows[0].keys()) if rows else [
        "agent_level", "framework", "topology", "benchmark", "model", "n_pairs",
        "unique_tasks", "inference_status", "median_duration_overhead_ratio",
        "mean_duration_overhead_ratio", "duration_overhead_ci_low", "duration_overhead_ci_high",
        "median_duration_overhead_s", "correctness_overhead",
        "llm_call_overhead", "tool_call_overhead", "token_overhead",
        "cost_overhead_usd", "cpu_usage_overhead", "peak_memory_overhead",
        "network_bytes_overhead",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def write_scientific_summary_csv(records: list[dict], path: str) -> None:
    """Write the principal scientific result table.

    Fault-effect estimates use only complete triplets whose fault record is
    classified ``valid``.  Invalid, inconclusive, and untriggered experiments
    are counted separately in the data-quality fields.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    by_pair = _condition_index(records)
    grouped: dict[tuple, list[tuple[dict | None, dict | None, dict]]] = {}
    for fault in _fault_records(records):
        pair = by_pair.get(str(fault.get("pair_id")), {})
        grouped.setdefault(_science_key(fault), []).append((
            pair.get("direct_baseline"),
            pair.get("cj_control"),
            fault,
        ))

    rows: list[dict[str, Any]] = []
    for key, triplet_like in sorted(grouped.items()):
        level, system, topology, benchmark, model, fault_type, severity = key
        complete = [(b, c, f) for b, c, f in triplet_like if b and c]
        valid = [(b, c, f) for b, c, f in complete if f.get("validity") == "valid"]
        baseline_eligible = [(b, c, f) for b, c, f in valid if _as_bool_success(b)]
        control_eligible = [(b, c, f) for b, c, f in valid if _as_bool_success(c)]
        conditional_success = [f for _b, _c, f in baseline_eligible if _as_bool_success(f)]
        unique_tasks = len({(f.get("task_id") or "") for _b, _c, f in complete})
        inference_status = _inference_status(
            unique_tasks=unique_tasks,
            valid_pairs=len(valid),
        )
        fault_deg_ci = (
            _ci_from_triplet_diffs(
                control_eligible,
                lambda _b, c, f: _as_float(c.get("success")) - _as_float(f.get("success")),
            )
            if inference_status == "ok" and control_eligible
            else {"mean": None, "ci_low": None, "ci_high": None, "n_tasks": unique_tasks}
        )
        counts = _validity_counts([f for _b, _c, f in triplet_like])
        selectivity = _selectivity_metrics([f for _b, _c, f in triplet_like])
        overhead_ratios = [
            (_as_float(c.get("duration_s")) - _as_float(b.get("duration_s"))) / _as_float(b.get("duration_s"))
            for b, c, _f in complete
            if _as_float(b.get("duration_s")) > 0
        ]
        rows.append({
            "agent_level": level,
            "output_schema_version": OUTPUT_SCHEMA_VERSION,
            "system": system,
            "model": model,
            "fault": fault_type,
            "severity": severity,
            "topology": topology,
            "benchmark": benchmark,
            "tasks": unique_tasks,
            "unique_tasks": unique_tasks,
            "complete_triplets": len(complete),
            "missing_pair_count": len(triplet_like) - len(complete),
            "valid_pairs": len(valid),
            "baseline_eligible_pairs": len(baseline_eligible),
            "control_eligible_pairs": len(control_eligible),
            "inference_status": inference_status,
            "evaluation_scope": (
                "agent_resilience"
                if baseline_eligible and control_eligible
                else "fault_injector_validation"
            ),
            "baseline_pass_at_1": _mean([_as_float(b.get("success")) for b, _c, _f in valid]),
            "control_pass_at_1": _mean([_as_float(c.get("success")) for _b, c, _f in valid]),
            "fault_pass_at_1": _mean([_as_float(f.get("success")) for _b, _c, f in valid]),
            "total_degradation": _mean([
                _as_float(b.get("success")) - _as_float(f.get("success"))
                for b, _c, f in baseline_eligible
            ]),
            "fault_specific_degradation": _mean([
                _as_float(c.get("success")) - _as_float(f.get("success"))
                for _b, c, f in control_eligible
            ]),
            "cj_correctness_overhead": _mean([
                _as_float(b.get("success")) - _as_float(c.get("success"))
                for b, c, _f in valid
            ]),
            "conditional_robustness": _ratio(len(conditional_success), len(baseline_eligible)),
            "catastrophic_failure_rate": _ratio(len(baseline_eligible) - len(conditional_success), len(baseline_eligible)),
            "fault_degradation_ci_low": fault_deg_ci.get("ci_low"),
            "fault_degradation_ci_high": fault_deg_ci.get("ci_high"),
            "trigger_rate": counts["trigger_rate"],
            "manifestation_rate": counts["manifestation_rate"],
            "recovery_rate": counts["verified_recovery_rate"],
            "cj_overhead": _median(overhead_ratios),
            "activation_rate": counts["activation_rate"],
            "invalid_experiment_rate": counts["invalid_experiment_rate"],
            "inconclusive_rate": counts["inconclusive_rate"],
            "untriggered_rate": counts["untriggered_rate"],
            "excluded_invalid_count": sum(1 for _b, _c, f in triplet_like if f.get("validity") == "invalid"),
            "excluded_inconclusive_count": sum(1 for _b, _c, f in triplet_like if f.get("validity") == "inconclusive"),
            "excluded_untriggered_count": sum(1 for _b, _c, f in triplet_like if f.get("validity") == "untriggered"),
            "selectivity_precision": selectivity["selectivity_precision"],
            "target_coverage": selectivity["target_coverage"],
            "framework_versions": json.dumps(_first_nonempty([f.get("framework_versions") for _b, _c, f in triplet_like]), sort_keys=True),
            "cj_commit": _first_nonempty([f.get("cj_commit") for _b, _c, f in triplet_like]),
            "cj_source_version": _first_nonempty([f.get("cj_source_version") for _b, _c, f in triplet_like]),
            "cj_source_state": _first_nonempty([f.get("cj_source_state") for _b, _c, f in triplet_like]),
            "docker_image_digest": _first_nonempty([f.get("docker_image_digest") or f.get("image_digest") for _b, _c, f in triplet_like]),
            "fault_parameters": json.dumps(_first_nonempty([f.get("fault_parameters") for _b, _c, f in triplet_like]) or {}, sort_keys=True),
        })

    fields = list(rows[0].keys()) if rows else [
        "agent_level", "output_schema_version", "system", "model", "fault", "severity", "topology",
        "benchmark", "tasks", "unique_tasks", "complete_triplets",
        "missing_pair_count", "valid_pairs", "baseline_eligible_pairs",
        "control_eligible_pairs", "inference_status", "evaluation_scope",
        "baseline_pass_at_1", "control_pass_at_1", "fault_pass_at_1",
        "total_degradation", "fault_specific_degradation",
        "cj_correctness_overhead", "conditional_robustness",
        "catastrophic_failure_rate", "fault_degradation_ci_low",
        "fault_degradation_ci_high", "trigger_rate", "manifestation_rate",
        "recovery_rate", "cj_overhead", "activation_rate",
        "invalid_experiment_rate", "inconclusive_rate", "untriggered_rate",
        "excluded_invalid_count", "excluded_inconclusive_count",
        "excluded_untriggered_count", "selectivity_precision", "target_coverage",
        "framework_versions", "cj_commit", "cj_source_version",
        "cj_source_state", "docker_image_digest", "fault_parameters",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _first_nonempty(values: list[Any]) -> Any:
    for value in values:
        if value not in (None, "", {}, []):
            return value
    return ""


def write_fault_fidelity_summary_csv(records: list[dict], path: str) -> None:
    """Write fault-fidelity metrics from CJ evidence and paired controls."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    by_pair = _condition_index(records)
    grouped: dict[tuple, list[tuple[dict | None, dict]]] = {}
    for fault in _fault_records(records):
        pair = by_pair.get(str(fault.get("pair_id")), {})
        grouped.setdefault(_science_key(fault), []).append((pair.get("cj_control"), fault))

    rows: list[dict[str, Any]] = []
    for key, pairs in sorted(grouped.items()):
        level, system, topology, benchmark, model, fault_type, severity = key
        fault_records = [f for _c, f in pairs]
        affected_calls = sum(_affected_call_count(f) for f in fault_records)
        intercepted_calls = sum(int(_lifecycle(f).get("proxy_call_count") or 0) for f in fault_records)
        params = _first_nonempty([f.get("fault_parameters") for f in fault_records])
        params = params if isinstance(params, dict) else {}
        configured_delay = _as_float(params.get("delay_s"), 0.0)
        configured_timeout = _as_float(params.get("timeout_s"), 0.0)
        observed_latency_samples = _fault_evidence_numbers(
            fault_records,
            "observed_injected_delay_s",
            "observed_delay_s",
            "injected_delay_s",
            fault_names={"latency", "llm_latency"},
        )
        configured_latency_samples = _fault_evidence_numbers(
            fault_records,
            "configured_delay_s",
            "delay_s",
            fault_names={"latency", "llm_latency"},
        )
        if fault_type == "llm_latency":
            expected_latency = (
                sum(configured_latency_samples)
                if configured_latency_samples
                else affected_calls * configured_delay
            )
            observed_latency = (
                sum(observed_latency_samples)
                if observed_latency_samples
                else None
            )
            latency_fidelity_source = (
                "cj_proxy_sleep_evidence"
                if observed_latency_samples
                else "missing_proxy_sleep_evidence"
            )
        else:
            expected_latency = None
            observed_latency = None
            latency_fidelity_source = ""
        expected_timeout = affected_calls * configured_timeout if fault_type == "llm_timeout" else None
        timeout_proxy_latencies = _proxy_latencies_for_fault(fault_records, {"timeout", "llm_timeout"}, http_status=504)
        observed_timeout = sum(timeout_proxy_latencies) if fault_type == "llm_timeout" and timeout_proxy_latencies else None
        timeout_fidelity_source = (
            "cj_proxy_latency_s"
            if observed_timeout is not None
            else "missing_proxy_timestamps" if fault_type == "llm_timeout" else ""
        )
        evidence_values = _observed_evidence_values(fault_records)
        original_token_limit = _first_observed(
            evidence_values,
            "original_token_limit", "original_max_tokens", "original_max_completion_tokens",
        )
        modified_token_limit = (
            _first_observed(
                evidence_values,
                "modified_token_limit", "modified_max_tokens",
                "max_tokens", "max_completion_tokens",
            )
            or (params.get("max_tokens") if "max_tokens" in params else "")
        )
        original_response_length = _first_observed(
            evidence_values,
            "original_response_length", "original_length", "original_len",
        )
        truncated_response_length = _first_observed(
            evidence_values,
            "truncated_response_length", "mutated_response_length",
            "mutated_length", "mutated_len", "truncated_length",
        )
        original_response_bytes = _first_observed(
            evidence_values,
            "original_response_bytes", "original_bytes",
        )
        mutated_response_bytes = _first_observed(
            evidence_values,
            "mutated_response_bytes", "truncated_response_bytes",
            "mutated_bytes",
        )
        truncation_ratio = _first_observed(evidence_values, "truncation_ratio")
        tool_call_target = _first_observed(
            evidence_values,
            "tool_name", "tool_call_target", "operation", "target",
        )
        completion_token_reduction = _mean([
            _as_float(c.get("completion_tokens")) - _as_float(f.get("completion_tokens"))
            for c, f in pairs
            if c is not None
        ])
        selectivity = _selectivity_metrics(fault_records)
        affected_call_fraction = _ratio(affected_calls, intercepted_calls)
        rows.append({
            "agent_level": level,
            "output_schema_version": OUTPUT_SCHEMA_VERSION,
            "system": system,
            "topology": topology,
            "benchmark": benchmark,
            "model": model,
            "fault": fault_type,
            "severity": severity,
            "configured_severity": json.dumps(params, sort_keys=True),
            "intercepted_calls": intercepted_calls,
            "affected_calls": affected_calls,
            "observed_added_latency_s": observed_latency,
            "expected_added_latency_s": expected_latency,
            "latency_error_s": (
                abs(float(observed_latency) - float(expected_latency))
                if observed_latency is not None and expected_latency is not None
                else None
            ),
            "latency_fidelity_source": latency_fidelity_source,
            "observed_added_latency_per_affected_call_s": (
                observed_latency / len(observed_latency_samples)
                if observed_latency is not None and observed_latency_samples
                else observed_latency / affected_calls
                if observed_latency is not None and affected_calls
                else None
            ),
            "observed_timeout_duration_s": observed_timeout,
            "expected_timeout_duration_s": expected_timeout,
            "timeout_error_s": (
                abs(float(observed_timeout) - float(expected_timeout))
                if observed_timeout is not None and expected_timeout is not None
                else None
            ),
            "timeout_fidelity_source": timeout_fidelity_source,
            "original_token_limit": original_token_limit,
            "modified_token_limit": modified_token_limit,
            "completion_token_reduction": completion_token_reduction,
            "original_response_length": original_response_length,
            "truncated_response_length": truncated_response_length,
            "original_response_bytes": original_response_bytes,
            "mutated_response_bytes": mutated_response_bytes,
            "truncation_ratio": truncation_ratio,
            "json_parse_failure_confirmed": (
                True if fault_type == "malformed_response"
                and (
                    "invalid_json" in evidence_values.get("mode", set())
                    or any((_lifecycle(f).get("manifested") is True) for f in fault_records)
                )
                else ""
            ),
            "tool_call_target": tool_call_target,
            "affected_call_fraction": affected_call_fraction,
            "affected_call_precision": selectivity["selectivity_precision"],
            "selectivity_precision": selectivity["selectivity_precision"],
            "target_coverage": selectivity["target_coverage"],
            "observed_http_statuses": json.dumps(sorted(evidence_values.get("http_status", []))),
            "observed_modes": json.dumps(sorted(evidence_values.get("mode", []))),
            "observed_truncated_token_counts": json.dumps(sorted(evidence_values.get("truncated_tokens", []))),
            "injection_point_timing": _injection_point_timing(fault_records),
            "activation_verification_time_s": "",
            "recovery_verification_time_s": "",
        })

    fields = list(rows[0].keys()) if rows else [
        "agent_level", "system", "topology", "benchmark", "model", "fault",
        "severity", "configured_severity", "intercepted_calls", "affected_calls",
        "observed_added_latency_s", "expected_added_latency_s", "latency_error_s",
        "latency_fidelity_source", "observed_added_latency_per_affected_call_s",
        "observed_timeout_duration_s", "expected_timeout_duration_s",
        "timeout_error_s", "original_token_limit", "timeout_fidelity_source",
        "modified_token_limit", "completion_token_reduction",
        "original_response_length", "truncated_response_length",
        "original_response_bytes", "mutated_response_bytes",
        "truncation_ratio", "json_parse_failure_confirmed", "tool_call_target",
        "affected_call_fraction", "affected_call_precision",
        "selectivity_precision", "target_coverage", "observed_http_statuses",
        "observed_modes", "observed_truncated_token_counts",
        "injection_point_timing", "activation_verification_time_s",
        "recovery_verification_time_s",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _observed_evidence_values(records: list[dict]) -> dict[str, set[str]]:
    values: dict[str, set[str]] = {
        "http_status": set(),
        "mode": set(),
        "truncated_tokens": set(),
        "original_token_limit": set(),
        "original_max_tokens": set(),
        "original_max_completion_tokens": set(),
        "modified_token_limit": set(),
        "modified_max_tokens": set(),
        "max_tokens": set(),
        "max_completion_tokens": set(),
        "original_response_length": set(),
        "original_response_bytes": set(),
        "original_bytes": set(),
        "original_length": set(),
        "original_len": set(),
        "truncated_response_length": set(),
        "truncated_response_bytes": set(),
        "mutated_response_length": set(),
        "mutated_response_bytes": set(),
        "mutated_bytes": set(),
        "mutated_length": set(),
        "mutated_len": set(),
        "truncated_length": set(),
        "truncation_ratio": set(),
        "tool_name": set(),
        "tool_call_target": set(),
        "operation": set(),
        "target": set(),
    }
    for record in records:
        for entry in _evidence_entries(record):
            evidence = entry.get("evidence") if isinstance(entry.get("evidence"), dict) else entry
            for key in values:
                if key in evidence:
                    values[key].add(str(evidence[key]))
            target = entry.get("target")
            if isinstance(target, dict):
                for key in ("operation", "tool_name", "target"):
                    if target.get(key):
                        values[key].add(str(target[key]))
    return values


def _proxy_latencies_for_fault(
    records: list[dict],
    fault_names: set[str],
    *,
    http_status: int | None = None,
) -> list[float]:
    latencies: list[float] = []
    for record in records:
        for call in _proxy_calls(record):
            status_matches = (
                http_status is None
                or str(call.get("http_status", "")) == str(http_status)
            )
            triggered = _json_or(call.get("triggered_faults_json", "[]"), [])
            triggered_names = {str(item) for item in triggered} if isinstance(triggered, list) else set()
            call_fault = str(call.get("fault_name") or "")
            fault_matches = call_fault in fault_names or bool(triggered_names & fault_names)
            latency = call.get("latency_s")
            if status_matches and fault_matches and latency not in (None, ""):
                latencies.append(_as_float(latency))
    return latencies


def _fault_evidence_numbers(
    records: list[dict],
    *keys: str,
    fault_names: set[str] | None = None,
) -> list[float]:
    values: list[float] = []
    names = fault_names or set()
    for record in records:
        for entry in _evidence_entries(record):
            if names and str(entry.get("fault_type", "")) not in names:
                continue
            evidence = entry.get("evidence") if isinstance(entry.get("evidence"), dict) else entry
            for key in keys:
                if key in evidence and evidence[key] not in (None, ""):
                    values.append(_as_float(evidence[key]))
                    break
    return values


def _injection_point_timing(records: list[dict]) -> str:
    for record in records:
        for entry in _evidence_entries(record):
            evidence = entry.get("evidence") if isinstance(entry.get("evidence"), dict) else entry
            start = evidence.get("injection_start_fault_offset_s")
            end = evidence.get("injection_end_fault_offset_s")
            if start not in (None, "") or end not in (None, ""):
                return json.dumps(
                    {
                        "start_fault_offset_s": start,
                        "end_fault_offset_s": end,
                    },
                    sort_keys=True,
                )
        for call in _proxy_calls(record):
            offset = call.get("fault_offset_s")
            if offset not in (None, ""):
                return json.dumps({"recorded_fault_offset_s": offset}, sort_keys=True)
    return ""


def _first_observed(values: dict[str, set[str]], *keys: str) -> str:
    for key in keys:
        entries = sorted(values.get(key, set()))
        if entries:
            return entries[0]
    return ""


def write_agent_resilience_summary_csv(records: list[dict], path: str) -> None:
    """Write agent-recovery metrics, separate from CJ cleanup metrics."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    by_pair = _condition_index(records)
    grouped: dict[tuple, list[tuple[dict, dict, dict]]] = {}
    for fault in _fault_records(records):
        pair = by_pair.get(str(fault.get("pair_id")), {})
        base = pair.get("direct_baseline")
        control = pair.get("cj_control")
        if base and control and fault.get("validity") == "valid":
            grouped.setdefault(_science_key(fault), []).append((base, control, fault))

    rows: list[dict[str, Any]] = []
    for key, triplets in sorted(grouped.items()):
        level, system, topology, benchmark, model, fault_type, severity = key
        baseline_success_faults = [(b, c, f) for b, c, f in triplets if _as_bool_success(b)]
        fault_failures_after_baseline_success = [
            (b, c, f) for b, c, f in baseline_success_faults if not _as_bool_success(f)
        ]
        detected = [f for _b, _c, f in triplets if _record_agent_detected_fault(f)]
        continued = [f for _b, _c, f in triplets if _record_continued_operation(f)]
        repair = [f for _b, _c, f in triplets if _agent_attempted_repair(f)]
        successful_repair = [f for _b, _c, f in triplets if _agent_attempted_repair(f) and _as_bool_success(f)]
        graceful = [f for _b, _c, f in triplets if _agent_graceful_failure(f)]
        silent = [
            f for _b, _c, f in fault_failures_after_baseline_success
            if _record_silent_failure(f)
        ]
        rows.append({
            "agent_level": level,
            "output_schema_version": OUTPUT_SCHEMA_VERSION,
            "system": system,
            "topology": topology,
            "benchmark": benchmark,
            "model": model,
            "fault": fault_type,
            "severity": severity,
            "valid_pairs": len(triplets),
            "baseline_eligible_pairs": len(baseline_success_faults),
            "fault_detection_rate": _ratio(len(detected), len(triplets)),
            "continued_operation_rate": _ratio(len(continued), len(triplets)),
            "retry_repair_rate": _ratio(len(repair), len(triplets)),
            "successful_repair_rate": _ratio(len(successful_repair), len(repair)),
            "graceful_failure_rate": _ratio(len(graceful), len(triplets)),
            "silent_failure_rate": _ratio(len(silent), len(fault_failures_after_baseline_success)),
            "recovery_latency_s": "",
            "extra_llm_calls": _mean([
                _as_float(f.get("llm_calls")) - _as_float(c.get("llm_calls"))
                for _b, c, f in triplets
            ]),
            "extra_tool_calls": _mean([
                _as_float(f.get("tool_calls")) - _as_float(c.get("tool_calls"))
                for _b, c, f in triplets
            ]),
            "extra_turns": _mean([
                _as_float(f.get("turns")) - _as_float(c.get("turns"))
                for _b, c, f in triplets
            ]),
            "extra_tokens": _mean([
                _as_float(f.get("total_tokens")) - _as_float(c.get("total_tokens"))
                for _b, c, f in triplets
            ]),
        })

    fields = list(rows[0].keys()) if rows else [
        "agent_level", "system", "topology", "benchmark", "model", "fault",
        "severity", "valid_pairs", "baseline_eligible_pairs",
        "fault_detection_rate", "continued_operation_rate", "retry_repair_rate",
        "successful_repair_rate", "graceful_failure_rate", "silent_failure_rate",
        "recovery_latency_s", "extra_llm_calls", "extra_tool_calls",
        "extra_turns", "extra_tokens",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _agent_detected_fault(record: dict) -> bool:
    reason = str(record.get("termination_reason", "")).lower()
    if reason in {"graceful_failure", "controlled_failure", "reported_failure"}:
        return True
    trace = record.get("execution_trace") or (record.get("result") or {}).get("execution_trace", [])
    if isinstance(trace, list):
        return any(
            isinstance(event, dict)
            and event.get("event_type") in {"error_detection", "attempted_recovery"}
            for event in trace
        )
    return False


def _record_agent_detected_fault(record: dict) -> bool:
    if "agent_detected_fault" in record:
        return _as_bool_field(record.get("agent_detected_fault"))
    return _agent_detected_fault(record)


def _record_continued_operation(record: dict) -> bool:
    if "continued_operation" in record:
        return _as_bool_field(record.get("continued_operation"))
    return _agent_operational_success(record)


def _record_silent_failure(record: dict) -> bool:
    if "silent_failure" in record:
        return _as_bool_field(record.get("silent_failure"))
    return (
        not _as_bool_success(record)
        and _agent_operational_success(record)
        and not _agent_detected_fault(record)
    )


def _agent_reported_error(record: dict) -> bool:
    if _as_float(record.get("reported_error"), 0.0) >= 0.5:
        return True
    if record.get("exception") or record.get("executor_error"):
        return True
    status = str(record.get("executor_status", "")).lower()
    if status not in {"", "ok"}:
        return True
    reason = str(record.get("termination_reason", "")).lower()
    explicit_failure_reasons = {
        "api_error", "agent_exception", "container_killed",
        "controlled_failure", "error", "exception", "executor_error",
        "graceful_failure", "killed", "reported_failure", "timeout",
    }
    return reason in explicit_failure_reasons or "error" in reason or "exception" in reason


def _agent_reported_or_detected_error(record: dict) -> bool:
    return _agent_reported_error(record) or _agent_detected_fault(record)


def _runner_observed_failure(record: dict) -> tuple[bool, str]:
    if record.get("executor_timed_out") is True:
        return True, "runner_timeout"
    if record.get("exception"):
        return True, "runner_exception"
    if record.get("executor_error"):
        return True, "executor_error"
    status = str(record.get("executor_status", "")).lower()
    if status not in {"", "ok"}:
        return True, "executor_error"
    reason = str(record.get("termination_reason", "")).lower()
    if reason == "timeout":
        return True, "runner_timeout"
    if reason in {
        "api_error", "agent_exception", "container_killed",
        "error", "exception", "executor_error", "killed",
    }:
        return True, "runner_exception"
    if "exception" in reason:
        return True, "runner_exception"
    if "error" in reason:
        return True, "executor_error"
    return False, "none"


def _agent_operational_success(record: dict) -> bool:
    if _as_float(record.get("reported_error"), 0.0) >= 0.5:
        return False
    return str(record.get("termination_reason", "")).lower() == "completed"


def _agent_continued(record: dict) -> bool:
    if record.get("executor_timed_out") is True:
        return False
    if record.get("exception"):
        return False
    status = str(record.get("executor_status", "")).lower()
    reason = str(record.get("termination_reason", "")).lower()
    if status not in {"", "ok"}:
        return False
    blocked_reasons = {
        "timeout", "killed", "container_killed", "agent_exception",
        "executor_error", "error", "exception",
    }
    return reason not in blocked_reasons and "exception" not in reason


def _agent_graceful_failure(record: dict) -> bool:
    if _as_bool_success(record):
        return False
    if record.get("exception"):
        return False
    status = str(record.get("executor_status", "")).lower()
    reason = str(record.get("termination_reason", "")).lower()
    if status not in {"", "ok"}:
        return False
    return reason in {"graceful_failure", "controlled_failure", "reported_failure"}


def _agent_attempted_repair(record: dict) -> bool:
    if _as_float(record.get("retries"), 0.0) > 0:
        return True
    trace = record.get("execution_trace") or (record.get("result") or {}).get("execution_trace", [])
    if isinstance(trace, list):
        return any(
            isinstance(event, dict)
            and event.get("event_type") in {"attempted_recovery", "revision_request"}
            for event in trace
        )
    return False


def write_data_quality_summary_csv(records: list[dict], path: str) -> None:
    """Write reproducibility and data-quality counts per scientific stratum."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    by_pair = _condition_index(records)
    grouped: dict[tuple, list[tuple[dict | None, dict | None, dict]]] = {}
    for fault in _fault_records(records):
        pair = by_pair.get(str(fault.get("pair_id")), {})
        grouped.setdefault(_science_key(fault), []).append((
            pair.get("direct_baseline"),
            pair.get("cj_control"),
            fault,
        ))

    rows: list[dict[str, Any]] = []
    for key, triplet_like in sorted(grouped.items()):
        level, system, topology, benchmark, model, fault_type, severity = key
        complete = [(b, c, f) for b, c, f in triplet_like if b and c]
        valid = [(b, c, f) for b, c, f in complete if f.get("validity") == "valid"]
        baseline_eligible = [(b, c, f) for b, c, f in valid if _as_bool_success(b)]
        fault_records = [f for _b, _c, f in triplet_like]
        raw_condition_records = sum(
            (1 if b else 0) + (1 if c else 0) + 1
            for b, c, _f in triplet_like
        )
        rows.append({
            "agent_level": level,
            "output_schema_version": OUTPUT_SCHEMA_VERSION,
            "system": system,
            "topology": topology,
            "benchmark": benchmark,
            "model": model,
            "fault": fault_type,
            "severity": severity,
            "records": len(fault_records),
            "raw_fault_records": len(fault_records),
            "raw_condition_records": raw_condition_records,
            "expected_condition_records_for_complete_triplets": len(complete) * 3,
            "tasks": len([f.get("task_id") for f in fault_records]),
            "unique_tasks": len({f.get("task_id") for f in fault_records}),
            "repetitions": len({(f.get("task_id"), f.get("seed")) for f in fault_records}),
            "complete_triplets": len(complete),
            "missing_pair_count": len(triplet_like) - len(complete),
            "baseline_eligible_pair_count": len(baseline_eligible),
            "valid_fault_pair_count": len(valid),
            "excluded_invalid_count": sum(1 for f in fault_records if f.get("validity") == "invalid"),
            "excluded_inconclusive_count": sum(1 for f in fault_records if f.get("validity") == "inconclusive"),
            "excluded_untriggered_count": sum(1 for f in fault_records if f.get("validity") == "untriggered"),
            "model_versions": model,
            "framework_versions": json.dumps(_first_nonempty([f.get("framework_versions") for f in fault_records]), sort_keys=True),
            "cj_commit": _first_nonempty([f.get("cj_commit") for f in fault_records]),
            "cj_source_version": _first_nonempty([f.get("cj_source_version") for f in fault_records]),
            "cj_source_state": _first_nonempty([f.get("cj_source_state") for f in fault_records]),
            "docker_image_digest": _first_nonempty([f.get("docker_image_digest") or f.get("image_digest") for f in fault_records]),
            "fault_parameters": json.dumps(_first_nonempty([f.get("fault_parameters") for f in fault_records]) or {}, sort_keys=True),
            "resource_limits": json.dumps(_first_nonempty([f.get("container_resource_limits") for f in fault_records]) or {}, sort_keys=True),
        })

    fields = list(rows[0].keys()) if rows else [
        "agent_level", "output_schema_version", "system", "topology", "benchmark",
        "model", "fault", "severity", "records", "raw_fault_records", "raw_condition_records",
        "expected_condition_records_for_complete_triplets", "tasks", "unique_tasks",
        "repetitions", "complete_triplets", "missing_pair_count", "baseline_eligible_pair_count",
        "valid_fault_pair_count", "excluded_invalid_count",
        "excluded_inconclusive_count", "excluded_untriggered_count",
        "model_versions", "framework_versions", "cj_commit",
        "cj_source_version", "cj_source_state", "docker_image_digest",
        "fault_parameters", "resource_limits",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_multi_agent_process_summary_csv(records: list[dict], path: str) -> None:
    """Summarize process metrics from multi-agent execution traces."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    rows = []
    for r in records:
        if r.get("agent_level") != "multi_agent":
            continue
        result = r.get("result") if isinstance(r.get("result"), dict) else r
        trace = result.get("execution_trace") or []
        roles = {
            e.get("agent_role")
            for e in trace
            if isinstance(e, dict) and e.get("agent_role")
        }
        revisions = sum(1 for e in trace if isinstance(e, dict) and e.get("event_type") == "revision_request")
        handoffs = sum(1 for e in trace if isinstance(e, dict) and e.get("event_type") == "handoff")
        detections = sum(1 for e in trace if isinstance(e, dict) and e.get("event_type") == "error_detection")
        repairs = sum(1 for e in trace if isinstance(e, dict) and e.get("event_type") == "attempted_recovery")
        targeted_role = r.get("fault_target_role", "")
        affected_roles = {
            e.get("agent_role")
            for e in trace
            if isinstance(e, dict)
            and e.get("agent_role")
            and e.get("event_type") in {"error_detection", "attempted_recovery", "error", "fault_observed"}
        }
        propagation_roles = {
            role for role in affected_roles
            if targeted_role and role != targeted_role
        }
        rows.append({
            "study_id": r.get("study_id", ""),
            "pair_id": r.get("pair_id", ""),
            "run_id": r.get("run_id", ""),
            "condition": r.get("condition", ""),
            "framework": r.get("framework") or r.get("agent_system", ""),
            "topology": r.get("topology", ""),
            "task_id": r.get("task_id", ""),
            "message_count": len(trace),
            "affected_roles": len(roles),
            "affected_role_count": len(affected_roles),
            "affected_role_percentage": _ratio(len(affected_roles), len(roles)),
            "fault_propagation_probability": 1.0 if propagation_roles else 0.0 if targeted_role else "",
            "propagation_depth": len(propagation_roles) if targeted_role else "",
            "time_to_propagation_s": "",
            "successful_handoff_count": handoffs,
            "failed_handoff_count": sum(
                1 for e in trace
                if isinstance(e, dict) and e.get("event_type") == "handoff_failed"
            ),
            "message_loss_count": sum(1 for e in trace if isinstance(e, dict) and e.get("event_type") == "message_loss"),
            "message_corruption_count": sum(1 for e in trace if isinstance(e, dict) and e.get("event_type") == "message_corruption"),
            "message_duplication_count": sum(1 for e in trace if isinstance(e, dict) and e.get("event_type") == "message_duplication"),
            "detection_role": _first_nonempty([
                e.get("agent_role")
                for e in trace
                if isinstance(e, dict) and e.get("event_type") == "error_detection"
            ]),
            "repairing_role": _first_nonempty([
                e.get("agent_role")
                for e in trace
                if isinstance(e, dict) and e.get("event_type") == "attempted_recovery"
            ]),
            "revision_rounds": revisions,
            "coordination_overhead": "",
            "topology_conditional_robustness": "",
            "containment_rate": (
                1.0 if targeted_role and not propagation_roles
                else 0.0 if targeted_role
                else ""
            ),
            "fault_detection_events": detections,
            "attempted_recovery_events": repairs,
            "final_pass1": result.get("success", r.get("success", "")),
        })
    fields = list(rows[0].keys()) if rows else [
        "study_id", "pair_id", "run_id", "condition", "framework", "topology",
        "task_id", "message_count", "affected_roles", "affected_role_count",
        "affected_role_percentage", "fault_propagation_probability",
        "propagation_depth", "time_to_propagation_s", "successful_handoff_count",
        "failed_handoff_count", "message_loss_count", "message_corruption_count",
        "message_duplication_count", "detection_role", "repairing_role",
        "revision_rounds", "coordination_overhead",
        "topology_conditional_robustness", "containment_rate",
        "fault_detection_events", "attempted_recovery_events",
        "final_pass1",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def write_study_manifest(records: list[dict], path: str, study_id: str | None) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    manifest = {
        "study_id": study_id or (records[0].get("study_id") if records else ""),
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "output_schema_status": OUTPUT_SCHEMA_STATUS,
        "record_count": len(records),
        "conditions": sorted({r.get("condition", "") for r in records if r.get("condition")}),
        "campaign_ids": sorted({r.get("campaign_id", "") for r in records if r.get("campaign_id")}),
        "agent_levels": sorted({r.get("agent_level", "") for r in records if r.get("agent_level")}),
        "frameworks": sorted({(r.get("framework") or r.get("agent_system", "")) for r in records}),
        "benchmarks": sorted({r.get("benchmark", "") for r in records if r.get("benchmark")}),
        "task_ids": sorted({r.get("task_id", "") for r in records if r.get("task_id")}),
        "models": sorted({r.get("model", "") for r in records if r.get("model")}),
        "seeds": sorted({r.get("seed") for r in records if r.get("seed") is not None}),
        "faults": sorted({r.get("fault_type", "") for r in records if r.get("fault_type")}),
        "cj_commits": sorted({r.get("cj_commit", "") for r in records if r.get("cj_commit")}),
        "cj_source_versions": sorted({
            r.get("cj_source_version", "") for r in records if r.get("cj_source_version")
        }),
        "cj_source_states": sorted({
            r.get("cj_source_state", "") for r in records if r.get("cj_source_state")
        }),
        "docker_images": sorted({r.get("docker_image", "") for r in records if r.get("docker_image")}),
        "docker_image_digests": sorted({
            r.get("docker_image_digest") or r.get("image_digest", "")
            for r in records
            if r.get("docker_image_digest") or r.get("image_digest")
        }),
        "resource_limits": _unique_json_values([
            r.get("container_resource_limits") for r in records
            if r.get("container_resource_limits")
        ]),
        "framework_versions": _unique_json_values([
            r.get("framework_versions") for r in records
            if r.get("framework_versions")
        ]),
        "fault_parameters": _unique_json_values([
            r.get("fault_parameters") for r in records
            if r.get("fault_parameters")
        ]),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)


def _unique_json_values(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    out: list[Any] = []
    for value in values:
        key = json.dumps(value, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def write_validity_summary_csv(records: list[dict], path: str) -> None:
    """Write validity breakdown per (agent_system, benchmark, fault_type)."""
    from itertools import groupby

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def key(r):
        return (r.get("agent_system",""), r.get("benchmark",""), r.get("fault_type","none"))

    sorted_recs = sorted(records, key=key)
    rows: list[dict] = []
    for (sys, bench, fault), group_recs in groupby(sorted_recs, key=key):
        recs = list(group_recs)
        filt = classify_records(recs)
        fault_recs = filt.all_fault
        counts = _validity_counts(fault_recs)
        selectivity = _selectivity_metrics(fault_recs)
        rows.append({
            "agent_system":        sys,
            "benchmark":           bench,
            "fault_type":          fault,
            "n_baseline":          len(filt.baseline),
            "n_configured":         counts["configured_count"],
            "n_activated":          counts["activated_count"],
            "n_triggered":          counts["triggered_count"],
            "n_manifested":         counts["manifested_count"],
            "n_reverted":           counts["reverted_count"],
            "n_recovered":          counts["recovered_count"],
            "n_valid":             len(filt.valid),
            "n_invalid":           len(filt.invalid),
            "n_inconclusive":      len(filt.inconclusive),
            "n_untriggered":       len(filt.untriggered),
            "activation_rate":      counts["activation_rate"],
            "trigger_rate":         counts["trigger_rate"],
            "manifestation_rate":   counts["manifestation_rate"],
            "reversion_rate":       counts["reversion_rate"],
            "verified_recovery_rate": counts["verified_recovery_rate"],
            "invalid_experiment_rate": counts["invalid_experiment_rate"],
            "inconclusive_rate":    counts["inconclusive_rate"],
            "untriggered_rate":     counts["untriggered_rate"],
            "selectivity_precision": selectivity["selectivity_precision"],
            "target_coverage":      selectivity["target_coverage"],
        })

    if not rows:
        print(f"[output] No validity data — skipping {path}")
        return

    cols = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_group_summary_csv(records: list[dict], path: str) -> None:
    """Write group injection summary (from records with group evidence)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    group_recs = [
        r for r in records
        if isinstance(r.get("lifecycle"), dict)
        and r["lifecycle"].get("group_verdict") is not None
    ]
    if not group_recs:
        with open(path, "w", encoding="utf-8") as f:
            f.write("# No group injection records in this campaign\n")
        return
    # Summarize group evidence fields
    cols = ["run_id", "task_id", "fault_type", "group_verdict",
            "activation_skew_ms", "synchronization_valid"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in group_recs:
            lc = r.get("lifecycle", {})
            w.writerow({
                "run_id":               r.get("run_id",""),
                "task_id":              r.get("task_id",""),
                "fault_type":           r.get("fault_type",""),
                "group_verdict":        lc.get("group_verdict",""),
                "activation_skew_ms":   lc.get("activation_skew_ms",""),
                "synchronization_valid":lc.get("synchronization_valid",""),
            })


def write_latex_tables(
    condition_summary_path: str,
    output_dir: str = "results/latex",
) -> None:
    """Generate LaTeX table snippets from condition_summary.csv."""
    os.makedirs(output_dir, exist_ok=True)
    if not os.path.exists(condition_summary_path):
        with open(os.path.join(output_dir, "tables_notice.txt"), "w") as f:
            f.write("NO DATA: condition_summary.csv not found\n")
        return

    rows = []
    with open(condition_summary_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        with open(os.path.join(output_dir, "tables_notice.txt"), "w") as f:
            f.write("NO DATA: condition_summary.csv is empty\n")
        return

    latex = [
        r"\begin{table}[h]",
        r"\centering",
        r"\caption{Agent System Resilience under Fault Injection}",
        r"\label{tab:resilience}",
        r"\begin{tabular}{llllrrr}",
        r"\hline",
        r"System & Benchmark & Fault & $n$ & pass@1$_\mathrm{base}$ & "
        r"pass@1$_\mathrm{fault}$ & Degrad. \\",
        r"\hline",
    ]
    for row in rows:
        if row.get("fault_type") == "none":
            continue
        try:
            base  = f"{float(row['pass_at_1_baseline']):.3f}" if row.get('pass_at_1_baseline') not in (None,'','None') else "--"
            fault = f"{float(row['pass_at_1_fault']):.3f}"    if row.get('pass_at_1_fault') not in (None,'','None') else "--"
            degrad= f"{float(row['degradation']):+.3f}"       if row.get('degradation') not in (None,'','None') else "--"
        except (ValueError, TypeError):
            base = fault = degrad = "--"
        n = row.get("n_fault_valid", "--")
        latex.append(
            f"{row.get('agent_system','')} & {row.get('benchmark','')} & "
            f"{row.get('fault_type','')} & {n} & {base} & {fault} & {degrad} \\\\"
        )
    latex += [r"\hline", r"\end{tabular}", r"\end{table}"]

    out = os.path.join(output_dir, "table_resilience.tex")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(latex) + "\n")
    print(f"[output] LaTeX table → {out}")


def write_stats_summary_csv(records: list[dict], path: str) -> None:
    """Write per-condition statistical summary with CIs and Cohen's d_z."""
    from evaluation.analysis.statistics import compute_condition_stats
    from evaluation.analysis.validity import classify_records
    from itertools import groupby

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def fault_key(r: dict):
        return (r.get("agent_system", ""), r.get("benchmark", ""), r.get("fault_type", "none"))

    all_fault_recs = [r for r in records if r.get("phase") == "fault"]
    all_base_recs  = [r for r in records if r.get("condition") == "direct_baseline"]

    rows: list[dict] = []
    for (sys, bench, fault), group_recs in groupby(
        sorted(all_fault_recs, key=fault_key), key=fault_key
    ):
        group_list = list(group_recs)
        filt = classify_records(group_list)
        # Paired baselines for this condition
        pair_ids = {r.get("pair_id") for r in group_list if r.get("pair_id")}
        if pair_ids:
            base_recs = [r for r in all_base_recs
                         if r.get("agent_system") == sys
                         and r.get("benchmark") == bench
                         and r.get("pair_id") in pair_ids]
        else:
            base_recs = [r for r in all_base_recs
                         if r.get("agent_system") == sys and r.get("benchmark") == bench]

        stats_list = compute_condition_stats(
            baseline_records=base_recs,
            fault_records=filt.valid,
            condition_name=fault,
        )
        for s in stats_list:
            row = {"agent_system": sys, "benchmark": bench}
            row.update(s.to_dict())
            rows.append(row)

    if not rows:
        print(f"[output] No stats data — skipping {path}")
        return

    cols = ["agent_system", "benchmark"] + list(rows[0].keys())[2:]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"[output] Stats summary → {path}")


def write_inferential_summary_csv(records: list[dict], path: str) -> None:
    """Write paired, task-aware inferential tests for valid fault records."""
    from evaluation.analysis.statistics import (
        holm_correction,
        mcnemar_exact,
        paired_permutation_test,
        task_clustered_bootstrap_ci,
    )
    from evaluation.analysis.validity import classify_records
    from itertools import groupby

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    direct = [r for r in records if r.get("condition") == "direct_baseline"]
    faults = [r for r in records if r.get("phase") == "fault"]

    def key(r: dict):
        return (
            r.get("agent_level", ""),
            r.get("agent_system", ""),
            r.get("topology", ""),
            r.get("benchmark", ""),
            r.get("fault_type", "none"),
        )

    rows: list[dict[str, Any]] = []
    for (level, system, topology, bench, fault), group_recs in groupby(
        sorted(faults, key=key), key=key
    ):
        valid_faults = classify_records(list(group_recs)).valid
        pair_ids = {r.get("pair_id") for r in valid_faults if r.get("pair_id")}
        base = [
            r for r in direct
            if r.get("pair_id") in pair_ids
            and r.get("agent_system") == system
            and r.get("benchmark") == bench
        ]
        if not valid_faults or not base:
            continue
        unique_tasks = len({
            r.get("task_id", "")
            for r in valid_faults + base
            if r.get("task_id")
        })
        inference_status = _inference_status(
            unique_tasks=unique_tasks,
            valid_pairs=len(pair_ids),
        )
        if inference_status != "ok":
            rows.append({
                "agent_level": level,
                "agent_system": system,
                "topology": topology,
                "benchmark": bench,
                "fault_type": fault,
                "metric": "all",
                "test": "not_run",
                "inference_status": inference_status,
                "n_pairs": len(pair_ids),
                "n_tasks": unique_tasks,
                "effect": None,
                "ci_low": None,
                "ci_high": None,
                "p_value": None,
                "missing_pairs": 0,
                "baseline_only_success": "",
                "condition_only_success": "",
            })
            continue

        mcn = mcnemar_exact(base, valid_faults, metric="success")
        boot = task_clustered_bootstrap_ci(base, valid_faults, metric="success")
        rows.append({
            "agent_level": level,
            "agent_system": system,
            "topology": topology,
            "benchmark": bench,
            "fault_type": fault,
            "metric": "success",
            "test": "mcnemar_exact",
            "inference_status": "ok",
            "n_pairs": mcn.get("n_pairs"),
            "n_tasks": boot.get("n_tasks"),
            "effect": boot.get("mean_diff"),
            "ci_low": boot.get("ci_low"),
            "ci_high": boot.get("ci_high"),
            "p_value": mcn.get("p_value"),
            "missing_pairs": mcn.get("missing_pairs"),
            "baseline_only_success": mcn.get("baseline_only_success"),
            "condition_only_success": mcn.get("condition_only_success"),
        })

        for metric in ("duration_s", "llm_calls", "tool_calls", "turns", "total_tokens"):
            perm = paired_permutation_test(base, valid_faults, metric=metric)
            boot = task_clustered_bootstrap_ci(base, valid_faults, metric=metric)
            rows.append({
                "agent_level": level,
                "agent_system": system,
                "topology": topology,
                "benchmark": bench,
                "fault_type": fault,
                "metric": metric,
                "test": "paired_permutation",
                "inference_status": "ok",
                "n_pairs": perm.get("n_pairs"),
                "n_tasks": boot.get("n_tasks"),
                "effect": boot.get("mean_diff"),
                "ci_low": boot.get("ci_low"),
                "ci_high": boot.get("ci_high"),
                "p_value": perm.get("p_value"),
                "missing_pairs": perm.get("missing_pairs"),
                "baseline_only_success": "",
                "condition_only_success": "",
            })

    p_adjusted = holm_correction([
        float(r["p_value"]) if r.get("p_value") is not None else None
        for r in rows
    ])
    for row, p_holm in zip(rows, p_adjusted):
        row["p_value_holm"] = p_holm

    fields = [
        "agent_level", "agent_system", "topology", "benchmark", "fault_type",
        "metric", "test", "inference_status", "n_pairs", "n_tasks", "effect",
        "ci_low", "ci_high", "p_value", "p_value_holm", "missing_pairs",
        "baseline_only_success", "condition_only_success",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    print(f"[output] Inferential summary → {path}")


def generate_all_outputs(
    results_dir: str = "results",
    figures_dir: str | None = None,
    latex_dir:   str | None = None,
    campaign_id: str | None = None,
    study_id: str | None = None,
) -> None:
    """Load runs.jsonl and generate all output files.

    Parameters
    ----------
    study_id : str, optional
        When provided, only records from this exact study are processed. Legacy
        records without a study_id are excluded.
    campaign_id : str, optional
        When provided, only records from this campaign are processed.
        Prevents mixing data from old and new runs in the same JSONL file.
    """
    from evaluation.analysis.plots import plot_degradation, plot_validity_summary
    from itertools import groupby

    jsonl_path = os.path.join(results_dir, "runs.jsonl")
    all_records = load_jsonl(jsonl_path)

    if not all_records:
        print(f"[output] No records found in {jsonl_path}. Run an experiment first.")
        return

    if study_id:
        records = [r for r in all_records if r.get("study_id") == study_id]
        print(f"[output] Loaded {len(records)}/{len(all_records)} records "
              f"for study {study_id} from {jsonl_path}")
    elif campaign_id:
        records = [r for r in all_records if r.get("campaign_id") == campaign_id]
        print(f"[output] Loaded {len(records)}/{len(all_records)} records "
              f"for campaign {campaign_id[:8]} from {jsonl_path}")
    else:
        records = all_records
        print(f"[output] Loaded {len(records)} records from {jsonl_path}")

    write_task_results_csv(records, os.path.join(results_dir, "task_results.csv"))
    write_cj_evidence_csv(records, os.path.join(results_dir, "cj_evidence.csv"))
    write_condition_summary_csv(records, os.path.join(results_dir, "condition_summary.csv"))
    write_scientific_summary_csv(records, os.path.join(results_dir, "scientific_summary.csv"))
    write_validity_summary_csv(records, os.path.join(results_dir, "validity_summary.csv"))
    write_fault_fidelity_summary_csv(records, os.path.join(results_dir, "fault_fidelity_summary.csv"))
    write_group_summary_csv(records, os.path.join(results_dir, "group_summary.csv"))
    write_cj_overhead_summary_csv(records, os.path.join(results_dir, "cj_overhead_summary.csv"))
    write_agent_resilience_summary_csv(records, os.path.join(results_dir, "agent_resilience_summary.csv"))
    write_data_quality_summary_csv(records, os.path.join(results_dir, "data_quality_summary.csv"))
    write_multi_agent_process_summary_csv(records, os.path.join(results_dir, "multi_agent_process_summary.csv"))
    write_stats_summary_csv(records, os.path.join(results_dir, "stats_summary.csv"))
    write_inferential_summary_csv(records, os.path.join(results_dir, "inferential_summary.csv"))
    write_study_manifest(records, os.path.join(results_dir, "study_manifest.json"), study_id)

    fig_dir = figures_dir or os.path.join(results_dir, "figures")
    plot_degradation(
        _load_condition_rows(os.path.join(results_dir, "condition_summary.csv")),
        os.path.join(fig_dir, "degradation.pdf"),
    )
    plot_validity_summary(records, os.path.join(fig_dir, "validity.pdf"))

    latex_out = latex_dir or os.path.join(results_dir, "latex")
    write_latex_tables(os.path.join(results_dir, "condition_summary.csv"), latex_out)


def _load_condition_rows(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))
