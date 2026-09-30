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
python -m evaluation.run --generate-outputs --results-dir results/
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
#   results/full/condition_summary.csv
#   results/full/validity_summary.csv
#   results/full/group_summary.csv
#   results/full/figures/degradation.pdf     (requires matplotlib)
#   results/full/figures/validity.pdf        (requires matplotlib)
#   results/full/latex/table_resilience.tex
```

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
