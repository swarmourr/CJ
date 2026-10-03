# CJ Publication Evaluation Output Schema

Schema version: `cj-eval-publication-v1.0.1`

Status: frozen for the next multi-task pilot.

This schema separates:

- Task correctness: `task_success_verdict`, `success`, `tests_passed`, `tests_total`.
- Executor/scorer health: `executor_success_verdict`, `scorer_success_verdict`.
- Agent operation: `operational_success`, `continued_operation`.
- Detection: `agent_detected_fault`, `runner_observed_failure`, `failure_detection_source`.
- Silent failure: `silent_failure`.
- Agent telemetry versus CJ proxy telemetry: `agent_reported_llm_calls`, `proxy_intercepted_calls`.
- CJ fault effect: `proxy_calls_affected`, lifecycle fields, and `cj_evidence.csv`.
- Latency fidelity: `observed_injected_delay_s` evidence aggregated into `fault_fidelity_summary.csv`
  with `latency_fidelity_source=cj_proxy_sleep_evidence`.

Patch `v1.0.1` clarifies two pilot-discovered semantics:

- A fault is `triggered` only when CJ evidence shows the configured fault was
  applied, not merely because the proxy intercepted an unrelated call.
- `condition_summary.csv` exposes both `total_degradation_direct_minus_fault`
  and `fault_specific_degradation_control_minus_fault`; the legacy
  `degradation` column aliases the fault-specific `CJ-control - CJ-fault`
  effect for paper figures.

Every newly persisted run record must include `output_schema_version`.
Regenerated derived outputs also include this version so old JSONL records can be
identified as legacy or normalized to the current output contract.
