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

## Lifecycle And Reachability

Lifecycle fields are independent observations, not aliases:

- `configured`: the run requested the fault;
- `activated`: CJ verified that the injector was active;
- `triggered`: authoritative CJ evidence shows the target operation was matched;
- `manifested`: the requested effect was objectively observed;
- `reverted`: cleanup was executed successfully;
- `recovered`: post-cleanup verification confirmed restoration.

`validity` is `valid`, `untriggered`, `invalid`, or `inconclusive`. For an
untriggered run, `trigger_reachability_status`, `untriggered_reason`, and
diagnostic detail distinguish an unreachable workflow stage from a selector
mismatch or interception failure. Resilience estimates include only valid fault
runs; all other classes remain in validity and coverage summaries.

## Pairing And Eligibility

An exact triplet contains one `direct_baseline`, one `cj_control`, and one
`cj_fault` record sharing `pair_id`. The identifier includes task, framework,
topology, model, seed/repetition, fault, severity, and relevant configuration so
different experiments cannot collide.

Task-effect summaries require successful scoring and a valid fault. Conditional
robustness and catastrophic/silent-failure rates additionally require a
baseline-successful pair. Missing, invalid, inconclusive, untriggered, and
unscored pairs are counted explicitly rather than converted to zero success.

## Generated Files

| File | Purpose |
| --- | --- |
| `runs.jsonl` | Append-only task-level source records |
| `task_results.csv` | Flattened selected-study records |
| `cj_evidence.csv` | Bounded, redacted proxy/lifecycle evidence with hashes and lengths |
| `condition_summary.csv` | Stratified direct/control/fault descriptive outcomes |
| `scientific_summary.csv` | Principal validity-gated result table |
| `validity_summary.csv` | Lifecycle and validity-class rates |
| `trigger_coverage_summary.csv` | Reachability, untriggered reasons, and conditional fidelity |
| `fault_fidelity_summary.csv` | Configured versus observed fault severity |
| `cj_overhead_summary.csv` | Paired direct versus CJ-control overhead |
| `agent_resilience_summary.csv` | Detection, continuation, repair, graceful and silent failures |
| `multi_agent_process_summary.csv` | Propagation, affected roles, handoffs, revisions, and containment |
| `tool_stage_summary.csv` | Tool-stage reachability and failure-stage diagnosis |
| `observability_alignment_summary.csv` | Run/trace/span/role/step evidence coverage |
| `infrastructure_paper_summary.csv` | Container-scoped activation, measurements, confinement, and recovery |
| `inferential_summary.csv` | Paired tests, task-clustered intervals, effects, Holm correction, and status |
| `paper_alignment_summary.csv` | Claim-to-evidence map for related-work methodology |
| `study_manifest.json` | Selected-study matrix, versions, image digest, configuration, and timestamps |

All derived files are filtered to one `study_id`. Historical, smoke, pilot, and
main-study records are not mixed automatically.

## Additive Extended-study Records

E7--E9 use the same frozen schema version and live in separate result roots;
they do not append to canonical `runs.jsonl` files.

| Study | Raw/derived files | Additional identity |
| --- | --- | --- |
| E7 distributed coordination | `runs.jsonl`, `distributed_coordination_summary.json`, `distributed_coordination_runs.csv` | `study_id`, `run_id`, stable target role/type/id, group skew and rollback |
| E8 cross-layer chaos | `runs.jsonl`, `cj_evidence.sqlite`, `cross_layer_summary.json`, `e8_recovery_audit.json` | `study_id`, `factorial_id`, `run_id`, matched five-condition block |
| E9 performance/restoration | `runs.jsonl`, `performance_restoration_summary.json` | `study_id`, `run_id`, load condition and restoration cycle |

Group-level fields keep `group_valid`, `partial_activation`,
`rollback_success`, and `verified_recovery` separate. E8 aggregate counters are
computed from the complete proxy database; only serialized call previews are
bounded. E8's `task_success` means operational probe success and must not be
pooled with EvalPlus `pass@1`.

The separate real E8 runner writes five-condition factorial blocks:
`direct_baseline`, `cj_control`, `llm_only`, `infrastructure_only`, and
`compound`. These records contain ordinary scored-agent fields (`success`,
`scorer_status`, `tests_passed`, and `tests_total`) and may enter task-effect
analysis only when the full block is present, the CJ control intercepted but
affected zero calls, and all three fault conditions have `validity=valid`.
Strict test-count analyses additionally require every condition to execute
EvalPlus tests. Binary task-success analyses may include an explicit
`no_generated_code` outcome as failure while keeping `tests_total=0` and the
scorer status visible. Its `cross_layer_summary.json`, `task_results.csv`, and
`inferential_summary.csv` are never pooled with the operational-probe E8 files.

## Secret And Evidence Handling

API keys and authorization headers are forbidden in JSONL, CSV, logs, command
arguments, and generated artifacts. Full model responses are not copied into
evidence tables; store bounded redacted previews, lengths, hashes, mutation
metadata, status codes, and timing evidence instead. Aggregate counters are
computed from complete CJ database evidence, not from capped serialized previews.
