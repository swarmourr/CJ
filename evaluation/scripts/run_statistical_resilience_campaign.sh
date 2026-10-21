#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF'
Usage:
  evaluation/scripts/run_statistical_resilience_campaign.sh [options]

Cost-controlled paper campaign for statistically stronger CJ evaluation.

Design:
  1. Comprehensive fault-injector validation remains small and broad.
  2. Individual-agent resilience uses HumanEval+ and MBPP+, all three
     frameworks, 30 distinct tasks per benchmark by default, and three seeds.
  3. Native multi-agent resilience is representative rather than exhaustive.
  4. Optional second-model sensitivity is small and explicitly configured.

Options:
  --env-file PATH       Load ignored .env with LLM_API_KEY, LLM_BASE_URL, LLM_MODEL.
  --model NAME          Primary model. Default: minimax-m2 or .env LLM_MODEL.
  --second-model NAME   Optional smaller sensitivity model. Not run unless set.
  --tasks N             Individual-agent tasks per benchmark. Default: 30.
  --seeds N             Number of seeds/repeats. Default: 3.
  --multi-tasks N       Native multi-agent tasks per benchmark. Default: 12.
  --skip-injector       Skip all-fault injector validation.
  --skip-individual     Skip individual-agent resilience.
  --skip-multi          Skip native multi-agent pilot.
  --skip-second-model   Skip second-model sensitivity even if configured.
  --help                Show this help.

Environment overrides:
  CJ_EVAL_STAT_SYSTEMS          Default: autogen-real langgraph-real crewai-real
  CJ_EVAL_STAT_BENCHMARKS       Default: humanevalplus mbppplus
  CJ_EVAL_STAT_FAULT_SUITES     Default: resilience_core
  CJ_EVAL_STAT_MULTI_FAULT_SUITE Default: multi_agent
  CJ_EVAL_SECOND_MODEL_SYSTEMS  Default: autogen-real
  CJ_EVAL_SECOND_MODEL_TASKS    Default: 6
  CJ_EVAL_SECOND_MODEL_SEEDS    Default: 1
  CJ_EVAL_SECOND_MODEL_FAULT_SUITES Default: resilience_core
EOF
}

run_injector=1
run_individual=1
run_multi=1
run_second_model=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      export CJ_EVAL_ENV_FILE="$2"
      shift 2
      ;;
    --model)
      export CJ_EVAL_MODEL="$2"
      shift 2
      ;;
    --second-model)
      export CJ_EVAL_SECOND_MODEL="$2"
      shift 2
      ;;
    --tasks)
      export CJ_EVAL_STAT_TASKS="$2"
      shift 2
      ;;
    --seeds|--repeats)
      export CJ_EVAL_STAT_SEEDS="$2"
      shift 2
      ;;
    --multi-tasks)
      export CJ_EVAL_STAT_MULTI_TASKS="$2"
      shift 2
      ;;
    --skip-injector)
      run_injector=0
      shift
      ;;
    --skip-individual)
      run_individual=0
      shift
      ;;
    --skip-multi)
      run_multi=0
      shift
      ;;
    --skip-second-model)
      run_second_model=0
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
  export CJ_EVAL_EXPERIMENT_RUN="paper-statistical-${CJ_EVAL_RUN_STAMP}"
fi
cj_eval_init_experiment_run

image="$(cj_eval_default_image)"
cj_eval_require_image "${image}"
export CJ_EVAL_DOCKER_IMAGE="${image}"

primary_model="${CJ_EVAL_MODEL:-minimax-m2}"
systems="${CJ_EVAL_STAT_SYSTEMS:-autogen-real langgraph-real crewai-real}"
benchmarks="${CJ_EVAL_STAT_BENCHMARKS:-humanevalplus mbppplus}"
individual_tasks="${CJ_EVAL_STAT_TASKS:-30}"
seeds="${CJ_EVAL_STAT_SEEDS:-3}"
fault_suites="${CJ_EVAL_STAT_FAULT_SUITES:-resilience_core}"
multi_tasks="${CJ_EVAL_STAT_MULTI_TASKS:-12}"
multi_fault_suite="${CJ_EVAL_STAT_MULTI_FAULT_SUITE:-multi_agent}"
benchmark_subset="${CJ_EVAL_BENCHMARK_SUBSET:-development}"

cat <<EOF
[cj-eval] statistical resilience campaign
[cj-eval] results root     : $(cj_eval_results_root)
[cj-eval] image            : ${image}
[cj-eval] primary model    : ${primary_model}
[cj-eval] systems          : ${systems}
[cj-eval] benchmarks       : ${benchmarks}
[cj-eval] individual tasks : ${individual_tasks}
[cj-eval] seeds/repeats    : ${seeds}
[cj-eval] fault suites     : ${fault_suites}
[cj-eval] multi tasks      : ${multi_tasks}
[cj-eval] second model     : ${CJ_EVAL_SECOND_MODEL:-<disabled>}
EOF

if [[ "${run_injector}" -eq 1 ]]; then
  CJ_EVAL_MODEL="${primary_model}" \
  CJ_EVAL_SYSTEM="${CJ_EVAL_INJECTOR_SYSTEM:-autogen-real}" \
  CJ_EVAL_BENCHMARK="${CJ_EVAL_INJECTOR_BENCHMARK:-humanevalplus}" \
  CJ_EVAL_BENCHMARK_SUBSET="${CJ_EVAL_INJECTOR_SUBSET:-smoke}" \
  CJ_EVAL_FAULT_SUITE="${CJ_EVAL_INJECTOR_FAULT_SUITE:-all}" \
  CJ_EVAL_TASKS="${CJ_EVAL_INJECTOR_TASKS:-1}" \
  CJ_EVAL_REPEATS="${CJ_EVAL_INJECTOR_SEEDS:-1}" \
  CJ_EVAL_RUN_NAME="injector-all-faults-${primary_model}" \
    "${script_dir}/run_injector_validation.sh"
fi

if [[ "${run_individual}" -eq 1 ]]; then
  CJ_EVAL_MODEL="${primary_model}" \
  CJ_EVAL_SYSTEMS="${systems}" \
  CJ_EVAL_BENCHMARKS="${benchmarks}" \
  CJ_EVAL_BENCHMARK_SUBSET="${benchmark_subset}" \
  CJ_EVAL_FAULT_SUITES="${fault_suites}" \
  CJ_EVAL_TASKS="${individual_tasks}" \
  CJ_EVAL_REPEATS="${seeds}" \
  CJ_EVAL_RUN_NAME="individual-statistical-${primary_model}" \
    "${script_dir}/run_individual_resilience_pilot.sh"
fi

if [[ "${run_multi}" -eq 1 ]]; then
  CJ_EVAL_MODEL="${primary_model}" \
  CJ_EVAL_SYSTEMS="${systems}" \
  CJ_EVAL_BENCHMARKS="${benchmarks}" \
  CJ_EVAL_BENCHMARK_SUBSET="${benchmark_subset}" \
  CJ_EVAL_TOPOLOGIES="${CJ_EVAL_STAT_TOPOLOGIES:-linear closed_loop}" \
  CJ_EVAL_FAULT_SUITE="${multi_fault_suite}" \
  CJ_EVAL_TASKS="${multi_tasks}" \
  CJ_EVAL_REPEATS="${seeds}" \
  CJ_EVAL_RUN_NAME="native-multi-statistical-${primary_model}" \
    "${script_dir}/run_native_multi_agent_pilot.sh"
fi

if [[ "${run_second_model}" -eq 1 && -n "${CJ_EVAL_SECOND_MODEL:-}" ]]; then
  CJ_EVAL_MODEL="${CJ_EVAL_SECOND_MODEL}" \
  CJ_EVAL_SYSTEMS="${CJ_EVAL_SECOND_MODEL_SYSTEMS:-autogen-real}" \
  CJ_EVAL_BENCHMARKS="${benchmarks}" \
  CJ_EVAL_BENCHMARK_SUBSET="${benchmark_subset}" \
  CJ_EVAL_FAULT_SUITES="${CJ_EVAL_SECOND_MODEL_FAULT_SUITES:-resilience_core}" \
  CJ_EVAL_TASKS="${CJ_EVAL_SECOND_MODEL_TASKS:-6}" \
  CJ_EVAL_REPEATS="${CJ_EVAL_SECOND_MODEL_SEEDS:-1}" \
  CJ_EVAL_RUN_NAME="second-model-sensitivity-${CJ_EVAL_SECOND_MODEL}" \
    "${script_dir}/run_individual_resilience_pilot.sh"
fi

echo "[cj-eval] statistical resilience campaign complete under $(cj_eval_results_root)"
