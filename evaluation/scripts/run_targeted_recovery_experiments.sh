#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF'
Usage:
  evaluation/scripts/run_targeted_recovery_experiments.sh [options]

Run only the experiments needed to replace known invalid/untriggered evidence:
  1. Native LangGraph multi-agent: both benchmarks and both topologies.
  2. Controlled tool-required workload: both coding benchmarks.

Existing campaign results are never modified. Results are written under a new
experiment-run folder with independent checkpoints.

Options:
  --env-file PATH     Model secrets/configuration file. Default: .env
  --model NAME        Model name. Default: value loaded from the env file
  --tasks N           Tasks per benchmark. Default: 12
  --repeats N         Repetitions per task. Default: 3
  --results-run NAME  Folder name under results/
  --rebuild-image     Rebuild the evaluation image before validation
  --help              Show this help
EOF
}

rebuild=0
tasks=12
repeats=3

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) export CJ_EVAL_ENV_FILE="$2"; shift 2 ;;
    --model) export CJ_EVAL_MODEL="$2"; shift 2 ;;
    --tasks) tasks="$2"; shift 2 ;;
    --repeats) repeats="$2"; shift 2 ;;
    --results-run) export CJ_EVAL_EXPERIMENT_RUN="$2"; shift 2 ;;
    --rebuild-image) rebuild=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "[cj-eval] unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

cd "${CJ_EVAL_REPO_ROOT}"
cj_eval_model_defaults
export CJ_EVAL_RUN_STAMP="${CJ_EVAL_RUN_STAMP:-$(cj_eval_timestamp)}"
export CJ_EVAL_EXPERIMENT_RUN="${CJ_EVAL_EXPERIMENT_RUN:-targeted-recovery-${CJ_EVAL_RUN_STAMP}}"
export CJ_EVAL_EXPERIMENT_CREATED_AT="${CJ_EVAL_EXPERIMENT_CREATED_AT:-$(cj_eval_timestamp)}"

if [[ "${rebuild}" -eq 1 ]]; then
  evaluation/docker/rebuild_image.sh --force-remove
fi

image="$(cj_eval_default_image)"
cj_eval_require_image "${image}"
export CJ_EVAL_DOCKER_IMAGE="${image}"
cj_eval_init_experiment_run

cat <<EOF
[cj-eval] targeted recovery experiments
[cj-eval] results : $(cj_eval_results_root)
[cj-eval] image   : ${image}
[cj-eval] model   : ${CJ_EVAL_MODEL}
[cj-eval] tasks   : ${tasks}
[cj-eval] repeats : ${repeats}
[cj-eval] scope   : LangGraph native multi-agent + controlled tool reachability
EOF

echo "[cj-eval] phase 1/3: one-task LangGraph regression smoke"
(
  # Keep the gate in a separate checkpoint namespace. Otherwise its nine
  # records can be mistaken for the full linear HumanEval+ study when the
  # main phase backfills checkpoints from an existing run directory.
  export CJ_EVAL_EXPERIMENT_RUN="${CJ_EVAL_EXPERIMENT_RUN}-smoke-gate"
  CJ_EVAL_SYSTEMS="langgraph-real" \
  CJ_EVAL_TOPOLOGIES="linear" \
  CJ_EVAL_BENCHMARKS="humanevalplus" \
  CJ_EVAL_BENCHMARK_SUBSET="development" \
  CJ_EVAL_FAULT_SUITE="multi_agent" \
  CJ_EVAL_TASKS=1 \
  CJ_EVAL_REPEATS=1 \
  CJ_EVAL_PROXY_PORT="${CJ_EVAL_RECOVERY_SMOKE_PROXY_PORT:-18800}" \
    "${script_dir}/run_native_multi_agent_pilot.sh"
)

echo "[cj-eval] phase 2/3: four LangGraph replacement studies"
CJ_EVAL_SYSTEMS="langgraph-real" \
CJ_EVAL_TOPOLOGIES="linear closed_loop" \
CJ_EVAL_BENCHMARKS="humanevalplus mbppplus" \
CJ_EVAL_BENCHMARK_SUBSET="development" \
CJ_EVAL_FAULT_SUITE="multi_agent" \
CJ_EVAL_TASKS="${tasks}" \
CJ_EVAL_REPEATS="${repeats}" \
CJ_EVAL_PROXY_PORT="${CJ_EVAL_RECOVERY_LANGGRAPH_PROXY_PORT:-18900}" \
  "${script_dir}/run_native_multi_agent_pilot.sh"

echo "[cj-eval] phase 3/3: controlled tool-trigger coverage"
CJ_EVAL_TOOL_TRIGGERED_BENCHMARKS="humanevalplus mbppplus" \
CJ_EVAL_TOOL_TRIGGERED_BENCHMARK_SUBSET="development" \
CJ_EVAL_TOOL_TRIGGERED_FAULT_SUITES="tool" \
CJ_EVAL_TOOL_TRIGGERED_TASKS="${tasks}" \
CJ_EVAL_TOOL_TRIGGERED_REPEATS="${repeats}" \
CJ_EVAL_TOOL_TRIGGERED_PROXY_PORT="${CJ_EVAL_RECOVERY_TOOL_PROXY_PORT:-19000}" \
  "${script_dir}/run_tool_triggered_workload.sh"

echo "[cj-eval] targeted recovery complete: $(cj_eval_results_root)"
echo "[cj-eval] original campaign results were not modified"
