# CJ Evaluation Package

Reproducible evaluation of LLM agent system resilience under fault injection,
built on the Chaos Jungle (CJ) framework.

## Provenance

| Component | Version / Commit |
|---|---|
| CJ package | `586c6c0835c707ea5a95c37006b9e0d0a1316623` |
| AgentChaos reference | [floritange/AgentChaos](https://github.com/floritange/AgentChaos) — inspected for architecture descriptions; adapters are independent implementations |
| EvalPlus (HumanEval+, MBPP+) | 0.3.1, Apache-2.0 |

---

## Architecture

### Agent systems

| Name | CLI key | Architecture | Deviations |
|---|---|---|---|
| AutoGen-style | `autogen` | Two-agent UserProxy+Assistant conversation (Wu et al. 2023) | Uses direct API client instead of AutoGen's executor; no GroupChat |
| MAD-style | `mad` | Multi-Agent Debate with majority vote (Du et al. 2023) | Agents share one endpoint; judge replaced by majority vote |
| MapCoder-style | `mapcoder` | Five-stage pipeline: retrieval→planning→coding→verification→debugging (Islam et al. 2024) | Retrieval via LLM self-query instead of external vector store |

All adapters use the name `*-style` to be explicit that they are independent
implementations, not wrappers around the original libraries.

### Benchmarks

- **HumanEval+** — 164 coding tasks with augmented test suites (EvalPlus).
  Five tasks bundled for offline smoke runs.
- **MBPP+** — Sanitised MBPP with additional tests (EvalPlus).
  Five tasks bundled for offline smoke runs.

Correctness is determined by deterministic test execution in a sandboxed
subprocess. No LLM judge is used for primary coding results.

### CJ fault catalog

| Fault | CJ class | SoA comparable |
|---|---|---|
| `llm_timeout` | `LLMTimeout(timeout_s=5)` | Yes |
| `llm_rate_limit` | `LLMRateLimit(n=0)` | Yes |
| `llm_unavailable` | `LLMUnavailable()` | Yes |
| `response_truncation` | `LLMResponseCorrupt(mode="truncate")` | Yes |
| `malformed_response` | `LLMResponseCorrupt(mode="invalid_json")` | Yes |
| `false_response` | `LLMResponseCorrupt(mode="false_response")` | No |
| `tool_failure` | `ToolFault()` | No |
| `token_starvation` | `LLMTokenStarvation(max_tokens=10)` | No |
| `llm_latency` | `LLMLatency(delay_s=3.0)` | No |

---

## Installation

```bash
# Install CJ package (already done if you're in this repo)
pip install -e .

# Optional: install EvalPlus for development/full subsets
pip install evalplus==0.3.1

# Optional: install matplotlib for plots
pip install matplotlib
```

No extra agent-framework libraries are required. The adapters use a direct
OpenAI-compatible HTTP client.

---

## Model configuration

Set these environment variables before running:

```bash
export CJ_EVAL_BASE_URL="https://api.openai.com"   # or any OpenAI-compat URL
export CJ_EVAL_API_KEY="sk-..."
export CJ_EVAL_MODEL="gpt-4o-mini"
export CJ_EVAL_TEMPERATURE="0.0"
```

For a local Ollama server:
```bash
export CJ_EVAL_BASE_URL="http://localhost:11434"
export CJ_EVAL_API_KEY="dummy"
export CJ_EVAL_MODEL="qwen2.5"
```

For mock/smoke testing without any model:
```bash
# Use --dry-run flag — no CJ_EVAL_BASE_URL needed
```

**Never hardcode or commit credentials.**

---

## Running experiments

### Dry-run smoke test (no model required)

```bash
python -m evaluation.run \
  --dry-run \
  --system autogen \
  --benchmark humanevalplus \
  --fault llm_timeout \
  --tasks 3 --repeats 1 --seed 42 \
  --results-dir results/smoke
```

Expected output: 6 records (3 baseline + 3 fault), validity=inconclusive in dry-run.

### Smoke configuration (requires model)

```bash
python -m evaluation.run --config evaluation/configs/smoke.yaml
```

### Development configuration

```bash
python -m evaluation.run --config evaluation/configs/development.yaml
```

### Full campaign

**Estimate cost before running:**

---

## Publication CJ Evaluation Additions

The publication protocol separates individual-agent and multi-agent-system
records. Each task/fault group has exactly three paired conditions:

- `direct_baseline`
- `cj_control`
- `cj_fault`

Records must share `study_id`, `campaign_id`, and exact `pair_id` across the
three conditions. Derived outputs should be generated with a selected
`study_id`; legacy rows without that ID are excluded.

### Docker backend

Host-side orchestration uses `evaluation.docker_runner.DockerAgentRunner`.
It prepares one fresh container per execution, then runs:

```bash
python -m evaluation.container_entrypoint \
  --request /cj/input/request.json \
  --output /cj/output/result.json
```

Build or refresh the evaluation image with the helper script:

```bash
evaluation/docker/rebuild_image.sh
```

The script removes old local `cj-eval-agent:*` image tags, computes the current
Git commit, and builds `cj-eval-agent:<commit>`. If old tags are attached to
local containers, rerun with:

```bash
evaluation/docker/rebuild_image.sh --force-remove
```

Equivalent manual build command:

```bash
export CJ_COMMIT="$(git rev-parse HEAD)"
docker build \
  --build-arg CJ_COMMIT="$CJ_COMMIT" \
  -f evaluation/docker/Dockerfile \
  -t cj-eval-agent:"$CJ_COMMIT" .
```

The runner records the resolved image digest, Python/framework versions,
exit code, timeout status, stdout/stderr, structured `AgentRunResult`, and
container CPU/memory/user limits. Secrets are not accepted as Docker command
arguments; use `.env`/`--env-file` or provider-specific secret injection.
The Docker build writes the build-time commit into `/cj/work/COMMIT`; `.env`
and `.git` are excluded from the image context by `.dockerignore`.

### One publication triplet

Run one end-to-end paired triplet before launching any pilot:

```bash
python -m evaluation.run \
  --publication-study \
  --docker-image cj-eval-agent:"$CJ_COMMIT" \
  --system autogen-real \
  --agent-level individual \
  --topology single \
  --benchmark humanevalplus \
  --fault llm_latency \
  --tasks 1 \
  --repeats 1 \
  --seed 42 \
  --base-url http://127.0.0.1:8000/v1 \
  --model gpt-4o-mini \
  --env-file .env \
  --results-dir results/publication-smoke
```

This produces exactly three records per selected task: `direct_baseline`,
`cj_control`, and `cj_fault`. Derived outputs are generated once at the end
and filtered to the new `study_id`.

### Publication fault suite

Run the connected proxy-fault suite in one command:

```bash
python -m evaluation.run \
  --publication-study \
  --docker-image cj-eval-agent:"$CJ_COMMIT" \
  --system autogen-real \
  --agent-level individual \
  --topology single \
  --benchmark humanevalplus \
  --fault all \
  --tasks 1 \
  --repeats 1 \
  --seed 42 \
  --base-url http://127.0.0.1:8000/v1 \
  --container-base-url http://host.docker.internal:8000/v1 \
  --model gpt-4o-mini \
  --env-file .env \
  --results-dir results/publication-fault-suite
```

`--fault all` expands to every currently connected publication proxy fault:
`llm_timeout`, `llm_rate_limit`, `llm_unavailable`,
`response_truncation`, `malformed_response`, `false_response`, `tool_failure`,
`token_starvation`, and `llm_latency`. Use `--fault-suite llm_api`,
`--fault-suite response`, `--fault-suite semantic`, `--fault-suite tool`,
or a comma-separated list such as `--fault llm_latency,llm_unavailable` for
smaller sweeps. Each fault still gets its own exact paired direct/control/fault
triplet and separate `pair_id`.

### Paper scenario scripts

Ready-to-run paper scaffolding lives in `evaluation/scripts/`. Each script writes
to a named category folder under `results/paper/<category>/<run-name>-<UTC>/` and
stores the exact command and logs next to the generated outputs.

```bash
# Build the Docker image used by publication-study runs
evaluation/docker/rebuild_image.sh --force-remove

# Experiment scripts auto-build the current-commit image if it is missing.
# Disable that behavior only when you want fail-fast image checks:
CJ_EVAL_AUTO_BUILD_IMAGE=0 evaluation/scripts/run_injector_validation.sh

# All non-Docker/static validation tests
evaluation/scripts/run_static_validation.sh

# All tests wrapper; Docker tests are opt-in
evaluation/scripts/run_all_tests.sh
CJ_EVAL_RUN_DOCKER_TESTS=1 evaluation/scripts/run_all_tests.sh

# CJ injector validation over all connected proxy faults
evaluation/scripts/run_injector_validation.sh

# Individual-agent resilience pilot
evaluation/scripts/run_individual_resilience_pilot.sh

# Current framework-neutral multi-agent reference pilot
evaluation/scripts/run_multi_agent_reference_pilot.sh

# Small smoke wrapper across implemented paper categories
evaluation/scripts/run_all_paper_scenarios.sh
```

See `evaluation/PAPER_EXPERIMENTS.md` for the scenario matrix, category layout,
and architecture diagrams.

### Safe infrastructure faults

Network and resource faults must be applied with `DockerTarget` against the
experiment container. `evaluation.infrastructure_orchestrator.run_container_scoped_fault`
implements the safe order:

1. prepare idle container;
2. start the idle container;
3. connect `DockerTarget`;
4. activate fault inside the running container namespace;
5. verify activation;
6. execute workload in the same container;
7. revert and verify recovery;
8. clean up the container.
5. revert and verify recovery;
6. remove the container.

`ContainerKill` uses `DockerContainerControllerTarget`, a host-side controller
restricted to the exact experiment container ID. The Docker socket is never
mounted into the agent container.

### Multi-agent traces

`evaluation.multi_agent` defines the shared roles and schema:

- Planner
- Coder
- Reviewer/Tester

Topologies:

- `linear`: Planner -> Coder -> Reviewer -> Final
- `closed_loop`: reviewer can request at most two revisions

Every event includes `study_id`, `run_id`, `pair_id`, role, step index,
timestamp, trace ID, and span ID. Role-scoped proxy faults use internal
headers `X-CJ-Run-ID`, `X-CJ-Agent-Role`, and `X-CJ-Step`; the proxy consumes
them for selector matching and strips them before forwarding upstream.

### Output generation

Generate derived outputs for one selected study only:

```bash
python - <<'PY'
from evaluation.output import generate_all_outputs
generate_all_outputs("results/pilot", study_id="study-...")
PY
```

Outputs include task-level CSV, condition summary, lifecycle validity summary,
CJ overhead summary, multi-agent process summary, statistical summary, plots,
LaTeX tables, and `study_manifest.json`.

### Tests

Unit and proxy integration:

```bash
/opt/homebrew/bin/python3.12 -m pytest -q evaluation/tests/test_publication_eval.py
/opt/homebrew/bin/python3.12 -m pytest -q evaluation/tests/test_integration.py
```

Docker integration is marked and requires a local image. It does not pull from
the network automatically:

```bash
export CJ_EVAL_TEST_IMAGE=python:3.12-slim
/opt/homebrew/bin/python3.12 -m pytest -q -m docker evaluation/tests/test_docker_integration.py
```

### Cost and pilot sizing

Use `evaluation.costing` before any paid run:

```bash
python - <<'PY'
from evaluation.costing import PILOT_PROFILE, estimate_condition_count, estimate_cost_usd

executions = estimate_condition_count(
    tasks_per_benchmark=PILOT_PROFILE.tasks_per_benchmark,
    repetitions=PILOT_PROFILE.repetitions,
    frameworks=len(PILOT_PROFILE.frameworks),
    individual_faults=len(PILOT_PROFILE.individual_faults),
    multi_agent_faults=len(PILOT_PROFILE.multi_agent_faults),
    multi_agent_topologies=2,
)
print("executions:", executions)
print("estimated_usd:", estimate_cost_usd(
    executions=executions,
    avg_prompt_tokens=1500,
    avg_completion_tokens=800,
    llm_calls_per_execution=4,
    input_price_per_1k=0.00015,
    output_price_per_1k=0.0006,
))
PY
```

Do not launch the full paid campaign until the Docker image digest, credentials,
pilot confidence intervals, and safety limits are reviewed.

```bash
# Rough estimate: tasks × repeats × conditions × avg_tokens × price_per_token
# For gpt-4o-mini: ~164 tasks × 5 repeats × 18 conditions × 500 tokens ≈ $3-5
```

```bash
python -m evaluation.run --config evaluation/configs/full.yaml
```

### Individual fault experiment

```bash
python -m evaluation.run \
  --system mad \
  --benchmark mbppplus \
  --fault response_truncation \
  --tasks 20 --repeats 3 --seed 42 \
  --results-dir results/mad-truncation
```

### Regenerate outputs from existing results

```bash
python -m evaluation.run --generate-outputs --results-dir results/ --study-id study-...
```

---

## Output schema

All output files are in `results/` (configurable via `--results-dir`).

### `runs.jsonl`

One JSON line per execution. Required fields:

```json
{
  "run_id": "uuid",
  "timestamp": "ISO-8601",
  "cj_commit": "sha",
  "agent_system": "autogen-style",
  "benchmark": "humanevalplus",
  "task_id": "HumanEval/0",
  "model": "gpt-4o-mini",
  "endpoint_type": "openai",
  "seed": 42,
  "fault_type": "llm_timeout",
  "fault_parameters": {"timeout_s": 5.0},
  "target": "local",
  "phase": "baseline|fault",
  "success": 1.0,
  "duration_s": 1.2,
  "reported_error": 0.0,
  "retries": 0,
  "llm_calls": 3,
  "tool_calls": 1,
  "turns": 3,
  "prompt_tokens": 150,
  "completion_tokens": 300,
  "total_tokens": 450,
  "cost_usd": 0.0001,
  "tests_passed": 3,
  "tests_total": 3,
  "termination_reason": "success",
  "lifecycle": {"configured": true, "activated": true, "recovered": true, "verdict": "VALID"},
  "validity": "valid|invalid|inconclusive|untriggered",
  "oracle_outcome": {"silent_failure": false}
}
```

### `task_results.csv`

One row per RunRecord with key numeric fields.

### `condition_summary.csv`

Aggregated metrics per (agent_system, benchmark, fault_type):
pass@1_baseline, pass@1_fault, degradation, trigger_rate, manifestation_rate,
recovery_rate, robustness_score, silent_failure_rate, amplification factors.

### `validity_summary.csv`

Validity breakdown per condition: n_valid, n_invalid, n_inconclusive, n_untriggered.

### `group_summary.csv`

Group injection metrics (activation_skew_ms, synchronization_valid) when
InjectionGroup scenarios are used.

---

## Metric definitions

| Metric | Formula |
|---|---|
| `pass@1_baseline` | `successful_baseline / baseline_tasks` |
| `pass@1_fault` | `successful_manifested_fault / manifested_fault_tasks` |
| `degradation` | `pass@1_baseline − pass@1_fault` (positive = worse under fault) |
| `trigger_rate` | `triggered / attempted_injections` |
| `manifestation_rate` | `manifested / triggered` |
| `recovery_rate` | `recovered / manifested` |
| `robustness_score` | `fault_success_among_baseline_successes / baseline_successes` |
| `silent_failure_rate` | `silent_incorrect / manifested` |
| `llm_call_amplification` | `fault_llm_calls / baseline_llm_calls` |
| `degradation` sign | CJ internally computes `fault − baseline`; this package **reverses** the sign for degradation |

---

## Validity filtering

Primary resilience metrics (pass@1_fault, degradation, robustness) use only
**valid** fault records — executions where CJ confirmed:
1. Fault configured
2. Fault activated (verify_active passed)
3. Fault manifested (lifecycle evidence)

Records classified as `invalid`, `inconclusive`, or `untriggered` are reported
separately and never silently discarded. Use `validity_summary.csv` to track
injection success rates.

In **dry-run** mode all fault records are classified `inconclusive` because no
real CJ proxy is started — this is by design.

---

## Safety precautions

- No paid API calls are made in `--dry-run` mode.
- LLM proxy faults start on `localhost` only and are automatically reverted by `runner.stop()`.
- All generated code runs in a sandboxed subprocess with a hard timeout and an
  isolated temp directory. The subprocess does not inherit API keys.
- Never commit `CJ_EVAL_API_KEY` or model credentials.

---

## Estimated API cost (before a paid campaign)

| Config | Tasks | Repeats | Conditions | Est. tokens | Est. cost (gpt-4o-mini) |
|---|---|---|---|---|---|
| smoke | 3-5 | 1 | 4 | ~20K | <$0.01 |
| development | 20 | 3 | 12 | ~720K | ~$0.10 |
| full | 164 | 5 | 24 | ~20M | ~$3-5 |

Actual cost depends on model verbosity and fault behavior (timeout faults
may produce zero tokens). Always verify with a smoke run first.

---

## Regenerating tables and plots

```bash
# Generate all outputs from an existing runs.jsonl
python -m evaluation.run --generate-outputs --results-dir results/full

# Outputs:
#   results/full/task_results.csv
#   results/full/cj_evidence.csv
#   results/full/condition_summary.csv
#   results/full/scientific_summary.csv
#   results/full/validity_summary.csv
#   results/full/fault_fidelity_summary.csv
#   results/full/cj_overhead_summary.csv
#   results/full/agent_resilience_summary.csv
#   results/full/data_quality_summary.csv
#   results/full/multi_agent_process_summary.csv
#   results/full/inferential_summary.csv
#   results/full/group_summary.csv
#   results/full/figures/degradation.pdf     (requires matplotlib)
#   results/full/figures/validity.pdf        (requires matplotlib)
#   results/full/latex/table_resilience.tex
```

`scientific_summary.csv` is the principal result table. It keeps agent quality,
CJ validity, fault effect, CJ overhead, and data quality separate: complete
triplets, valid pairs, baseline-eligible pairs, baseline/control/fault pass@1,
conditional robustness, `C-F` fault-specific degradation, bootstrap confidence
intervals, trigger/recovery rates, and CJ overhead are reported as separate
columns. `fault_fidelity_summary.csv` reports whether the injected fault matched
the requested severity, such as configured versus observed latency.
`cj_evidence.csv` is the normalized CJ-native evidence table: lifecycle verdicts,
session IDs, proxy-call counts, configured/triggered fault JSON, and per-call
fault evidence are preserved there before any derived metric is computed.

---

## Running tests

```bash
# All CJ + evaluation tests
python -m pytest tests/ evaluation/tests/ -v

# Evaluation tests only
python -m pytest evaluation/tests/ -v
```

---

## Known limitations

### `tool_failure` fault cannot trigger
The three agent adapters (AutoGen-style, MAD-style, MapCoder-style) communicate
with the LLM via direct HTTP calls.  No `role="tool"` messages are sent because
no tool-calling API feature is used.  The `ToolFault` CJ class intercepts
tool-role messages; it will never activate for these adapters.

**Impact:** `tool_failure` experiments produce `validity=untriggered` records.
Exclude `tool_failure` from degradation analysis or implement tool-use in the
agent adapters before running it.

### Sandbox security scope
`sandbox_exec` isolates code execution in a temporary directory with a
wall-clock timeout and an env that excludes API keys.  It does NOT provide
OS-level process/filesystem isolation (e.g. `nsjail`, `bubblewrap`, `seccomp`).
Generated code can read host files and make network requests.

**Impact:** Suitable for trusted benchmark tasks (HumanEval+, MBPP+) whose test
code is known-safe.  Do not run arbitrary untrusted code from a real model
without additional OS-level sandboxing in production.

### Test coverage with dry-run only
The 94 unit tests use a fake model server and `--dry-run` mode.  No test
verifies real CJ proxy interception or actual fault manifestation.  Run
`smoke.yaml` against a real model+endpoint as an integration smoke test before
a paid campaign.

## Unresolved compatibility issues

1. **evalplus not installed** — `subset="smoke"` works offline with bundled tasks.
   `subset="development"` and `"full"` require `pip install evalplus==0.3.1`.
2. **matplotlib not installed** — plots are skipped with a notice. Install with
   `pip install matplotlib` for paper figures.
3. **Real model required for non-dry-run** — set `CJ_EVAL_BASE_URL` before running.
4. **LLM proxy faults require LocalTarget** — SSH and GPU faults from the CJ catalog
   are not included in this initial evaluation; they require remote infrastructure.

---

## Remaining steps before full campaign

1. Set model credentials and verify with a 3-task smoke run against the real model.
2. Run `development.yaml` to collect pilot variance estimates.
3. Use pilot variance to justify sample size for `full.yaml` (target n≥30 per condition).
4. Install `evalplus` for full HumanEval+ (164 tasks) and MBPP+ (374 tasks).
5. Install `matplotlib` for paper-ready figures.
6. Add network, CPU, memory, and compound fault experiments once infrastructure is ready.
