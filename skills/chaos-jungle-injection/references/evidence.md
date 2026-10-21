# Evidence And Reporting

## Minimum Lifecycle Record

Record each stage independently with timestamp, source, and diagnostic details:

| Stage | Minimum evidence |
| --- | --- |
| Configured | Fault class, parameters, selector, target identity |
| Activated | CJ verification result or authoritative target state |
| Triggered | Matching request/event/operation reached the injection point |
| Manifested | Objective observed effect matching requested severity |
| Reverted | Cleanup action and result |
| Recovered | Post-cleanup verification within defined tolerance |

Classify a run as `valid`, `untriggered`, `invalid`, or `inconclusive`. Only valid fault runs support fault-effect claims.

## Evidence By Layer

- LLM/response/tool: CJ proxy database, matched request, selected fault, mutation/blocking record, response status, and request/response summaries.
- Latency: monotonic timing around injected sleep per affected call; do not infer fidelity from total run duration.
- Network: `tc` state plus measured latency/loss through the affected namespace.
- CPU: active stress PID plus observed container CPU utilization.
- Memory: active stress PID plus observed container memory pressure.
- Disk: disposable-volume free space and a write failure such as `ENOSPC` when exhaustion is claimed.
- Process: PID existence before injection and termination evidence afterward.
- Container: Docker state transition for the exact experiment container.
- State: before/after checksum, field-level mutation, application read, and restoration validation.

## Separate Outcomes

Do not collapse these into one success value:

- task success;
- operational success;
- continued operation;
- agent detection;
- runner-observed failure;
- CJ recovery.

An agent exception can coexist with a correct saved artifact. A task can fail silently while the process completes normally. CJ cleanup is not agent recovery.

## Report Honestly

Include commands actually run, skipped or unsupported combinations, target identity, versions, and limitations. Redact secrets and full sensitive payloads; store hashes, lengths, bounded previews, and structured summaries instead.
