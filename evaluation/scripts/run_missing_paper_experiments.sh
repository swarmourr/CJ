#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF'
Usage:
  evaluation/scripts/run_missing_paper_experiments.sh [options]

Run every currently missing paper-evaluation category once, using real
implemented experiment paths where they exist and writing explicit
unsupported artifacts where they do not.

This script is additive: it does not replace the existing injector,
individual-agent, multi-agent-reference, overhead, or infrastructure scripts.

Options:
  --env-file PATH       Load model config/secrets from PATH. Default: .env.
  --results-run NAME    Result folder under results/.
  --model NAME          Override model.
  --tasks N             Default task count for real model categories.
  --repeats N           Repetitions. Default: 1.
  --skip-real-model     Skip real-model categories; write manifests only.
  --skip-infra          Skip DockerTarget infrastructure all-fault pass.
  --skip-process        Skip process/container diagnostic.
  --help                Show this help.

Environment:
  CJ_EVAL_MODELS              Space/comma list for model sensitivity.
                              Default: current model only.
  CJ_EVAL_MISSING_TASKS       Overrides --tasks.
  CJ_EVAL_MISSING_SYSTEMS     Default: autogen-real langgraph-real crewai-real.
  CJ_EVAL_MISSING_BENCHMARKS  Default: humanevalplus mbppplus.
EOF
}

run_real_model=1
run_infra=1
run_process=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      export CJ_EVAL_ENV_FILE="$2"
      shift 2
      ;;
    --results-run)
      export CJ_EVAL_EXPERIMENT_RUN="$2"
      shift 2
      ;;
    --model)
      export CJ_EVAL_MODEL="$2"
      shift 2
      ;;
    --tasks)
      export CJ_EVAL_MISSING_TASKS="$2"
      shift 2
      ;;
    --repeats)
      export CJ_EVAL_REPEATS="$2"
      shift 2
      ;;
    --skip-real-model)
      run_real_model=0
      shift
      ;;
    --skip-infra)
      run_infra=0
      shift
      ;;
    --skip-process)
      run_process=0
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "[cj-eval] unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

cd "${CJ_EVAL_REPO_ROOT}"
cj_eval_model_defaults

export CJ_EVAL_RUN_STAMP="${CJ_EVAL_RUN_STAMP:-$(cj_eval_timestamp)}"
export CJ_EVAL_EXPERIMENT_CREATED_AT="${CJ_EVAL_EXPERIMENT_CREATED_AT:-$(cj_eval_timestamp)}"
if [[ -z "${CJ_EVAL_EXPERIMENT_RUN:-}" ]]; then
  export CJ_EVAL_EXPERIMENT_RUN="missing-paper-experiments-${CJ_EVAL_RUN_STAMP}"
fi
cj_eval_init_experiment_run

image="$(cj_eval_default_image)"
if [[ "${run_real_model}" -eq 1 || "${run_infra}" -eq 1 ]]; then
  cj_eval_require_image "${image}"
fi
export CJ_EVAL_DOCKER_IMAGE="${image}"

tasks="${CJ_EVAL_MISSING_TASKS:-${CJ_EVAL_TASKS:-1}}"
repeats="${CJ_EVAL_REPEATS:-1}"
systems="${CJ_EVAL_MISSING_SYSTEMS:-autogen-real langgraph-real crewai-real}"
benchmarks="${CJ_EVAL_MISSING_BENCHMARKS:-humanevalplus mbppplus}"
models="${CJ_EVAL_MODELS:-${CJ_EVAL_MODEL}}"

cat <<EOF
[cj-eval] missing paper experiment bundle
[cj-eval] results root : $(cj_eval_results_root)
[cj-eval] image        : ${image}
[cj-eval] model        : ${CJ_EVAL_MODEL}
[cj-eval] models       : ${models}
[cj-eval] systems      : ${systems}
[cj-eval] benchmarks   : ${benchmarks}
[cj-eval] tasks/reps   : ${tasks}/${repeats}
[cj-eval] real model   : ${run_real_model}
[cj-eval] infra        : ${run_infra}
[cj-eval] process      : ${run_process}
EOF

if [[ "${run_real_model}" -eq 1 ]]; then
  # Strict EvalPlus gate and scoring reliability: one official-development
  # triplet before broader paid/pilot campaigns.
  CJ_EVAL_SYSTEM="${CJ_EVAL_STRICT_SYSTEM:-autogen-real}" \
  CJ_EVAL_BENCHMARK="${CJ_EVAL_STRICT_BENCHMARK:-humanevalplus}" \
  CJ_EVAL_BENCHMARK_SUBSET="${CJ_EVAL_STRICT_SUBSET:-development}" \
  CJ_EVAL_FAULT_SUITE="${CJ_EVAL_STRICT_FAULT_SUITE:-smoke}" \
  CJ_EVAL_TASKS="${CJ_EVAL_STRICT_TASKS:-1}" \
  CJ_EVAL_REPEATS="${repeats}" \
  CJ_EVAL_RUN_NAME="strict-evalplus-gate-${CJ_EVAL_MODEL}" \
    "${script_dir}/run_real_llm_smoke.sh"

  # Tool-required/tool-fault pass. These adapters expose execution tools; the
  # resulting records decide whether the task actually reached tool traffic.
  CJ_EVAL_SYSTEMS="${systems}" \
  CJ_EVAL_BENCHMARKS="${benchmarks}" \
  CJ_EVAL_BENCHMARK_SUBSET="${CJ_EVAL_BENCHMARK_SUBSET:-development}" \
  CJ_EVAL_FAULT_SUITES="tool" \
  CJ_EVAL_TASKS="${tasks}" \
  CJ_EVAL_REPEATS="${repeats}" \
  CJ_EVAL_RUN_NAME="tool-required" \
    "${script_dir}/run_individual_resilience_pilot.sh"

  # Semantic static-vs-generated comparison.
  CJ_EVAL_SYSTEMS="${systems}" \
  CJ_EVAL_BENCHMARKS="${benchmarks}" \
  CJ_EVAL_BENCHMARK_SUBSET="${CJ_EVAL_BENCHMARK_SUBSET:-development}" \
  CJ_EVAL_FAULT_SUITES="semantic_static semantic_generated" \
  CJ_EVAL_TASKS="${tasks}" \
  CJ_EVAL_REPEATS="${repeats}" \
  CJ_EVAL_RUN_NAME="semantic-static-vs-generated" \
    "${script_dir}/run_individual_resilience_pilot.sh"

  # Model sensitivity: bounded single-fault smoke per selected model.
  for model in ${models//,/ }; do
    CJ_EVAL_MODEL="${model}" \
    CJ_EVAL_FAULT_SUITE="${CJ_EVAL_MODEL_SENSITIVITY_FAULT_SUITE:-smoke}" \
    CJ_EVAL_TASKS="${CJ_EVAL_MODEL_SENSITIVITY_TASKS:-1}" \
    CJ_EVAL_REPEATS="${repeats}" \
    CJ_EVAL_RUN_NAME="model-sensitivity-${model}" \
      "${script_dir}/run_real_llm_smoke.sh"
  done
else
  "${script_dir}/write_unsupported_experiment.sh" \
    "strict-evalplus" "strict-evalplus-gate-skipped" \
    "real-model execution was disabled with --skip-real-model" \
    "rerun without --skip-real-model" \
    "provide .env with LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL"
fi

if [[ "${run_infra}" -eq 1 ]]; then
  for fault in cpu_pressure memory_pressure network_latency packet_loss disk_full; do
    CJ_EVAL_INFRA_FAULT="${fault}" \
    CJ_EVAL_BENCHMARK="${CJ_EVAL_INFRA_BENCHMARK:-toy}" \
    CJ_EVAL_TASKS="${CJ_EVAL_INFRA_TASKS:-1}" \
    CJ_EVAL_DRY_RUN="${CJ_EVAL_INFRA_DRY_RUN:-1}" \
    CJ_EVAL_RUN_NAME="infra-${fault}" \
      "${script_dir}/run_infrastructure_fault_smoke.sh"
  done
fi

if [[ "${run_process}" -eq 1 ]]; then
  "${script_dir}/run_process_container_fault_smoke.sh"
fi

"${script_dir}/write_unsupported_experiment.sh" \
  "native-multi-agent" "native-autogen-langgraph-crewai" \
  "native framework multi-agent planner/coder/reviewer workflows are not implemented yet" \
  "AutoGen native multi-agent workflow" \
  "LangGraph native multi-agent graph" \
  "CrewAI native multi-role crew" \
  "framework_native=true records from real native adapters"

"${script_dir}/write_unsupported_experiment.sh" \
  "distributed-multi-agent" "container-per-role" \
  "one-container-per-agent-role distributed architecture is not implemented yet" \
  "planner container" \
  "coder container" \
  "reviewer container" \
  "message bus with trace IDs" \
  "role-local DockerTarget/process/container faults"

"${script_dir}/write_unsupported_experiment.sh" \
  "communication-faults" "message-delay-drop-duplication-misrouting" \
  "communication-fault injection between agent roles is not implemented yet" \
  "message delay fault" \
  "message drop fault" \
  "message duplication fault" \
  "message misrouting fault" \
  "handoff-level manifestation evidence"

"${script_dir}/write_unsupported_experiment.sh" \
  "comparator" "cj-vs-toxiproxy" \
  "external comparator adapter is not implemented yet" \
  "Toxiproxy or equivalent injector adapter" \
  "matched task/model/seed matrix" \
  "equivalent latency/network fault parameters"

echo "[cj-eval] missing paper experiment bundle complete under $(cj_eval_results_root)"
