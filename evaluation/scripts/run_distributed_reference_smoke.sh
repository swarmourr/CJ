#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF'
Usage:
  evaluation/scripts/run_distributed_reference_smoke.sh [options]

Run the reference distributed multi-agent architecture: Planner, Coder, and
Reviewer each execute in a separate Docker container. This validates
container-per-role orchestration and scoring, not native framework multi-agent
implementations.

Options:
  --env-file PATH       Load model config/secrets from PATH. Default: .env.
  --model NAME          Override model.
  --benchmark NAME      humanevalplus or mbppplus. Default: humanevalplus.
  --topology NAME       linear or closed_loop. Default: linear.
  --tasks N             Number of tasks. Default: 1.
  --dry-run             Use deterministic no-model role responses.
  --results-run NAME    Result folder under results/.
  --help                Show this help.
EOF
}

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
    --benchmark)
      export CJ_EVAL_BENCHMARK="$2"
      shift 2
      ;;
    --topology)
      export CJ_EVAL_TOPOLOGY="$2"
      shift 2
      ;;
    --tasks)
      export CJ_EVAL_TASKS="$2"
      shift 2
      ;;
    --dry-run)
      export CJ_EVAL_DISTRIBUTED_DRY_RUN=1
      shift
      ;;
    --results-run)
      export CJ_EVAL_EXPERIMENT_RUN="$2"
      shift 2
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

image="$(cj_eval_default_image)"
cj_eval_require_image "${image}"

category="distributed-reference-smoke"
benchmark="${CJ_EVAL_BENCHMARK:-humanevalplus}"
benchmark_subset="${CJ_EVAL_BENCHMARK_SUBSET:-smoke}"
topology="${CJ_EVAL_TOPOLOGY:-linear}"
tasks="${CJ_EVAL_TASKS:-1}"
seed="${CJ_EVAL_SEED:-301}"
env_file="$(cj_eval_default_env_file)"
container_timeout="${CJ_EVAL_CONTAINER_TIMEOUT:-420}"
score_timeout="${CJ_EVAL_SCORE_TIMEOUT:-25}"
max_turns="${CJ_EVAL_MAX_TURNS:-8}"
cpus="${CJ_EVAL_CPUS:-1}"
memory="${CJ_EVAL_MEMORY:-2g}"
dry_run="${CJ_EVAL_DISTRIBUTED_DRY_RUN:-0}"
name="${CJ_EVAL_RUN_NAME:-${topology}-${benchmark}-${CJ_EVAL_MODEL}}"
run_dir="$(cj_eval_make_run_dir "${category}" "${name}")"
cj_eval_metadata "${run_dir}/study_manifest.json" "${category}" "${name}" "${image}"

cmd=(
  "${CJ_EVAL_PYTHON}" -m evaluation.distributed_campaign
  --docker-image "${image}"
  --results-dir "${run_dir}"
  --benchmark "${benchmark}"
  --benchmark-subset "${benchmark_subset}"
  --tasks "${tasks}"
  --seed "${seed}"
  --topology "${topology}"
  --base-url "${CJ_EVAL_BASE_URL}"
  --container-base-url "${CJ_EVAL_CONTAINER_BASE_URL}"
  --model "${CJ_EVAL_MODEL}"
  --env-file "${env_file}"
  --container-timeout "${container_timeout}"
  --score-timeout "${score_timeout}"
  --max-turns "${max_turns}"
  --cpus "${cpus}"
  --memory "${memory}"
)

if [[ "${dry_run}" == "1" ]]; then
  cmd+=(--dry-run)
fi

cj_eval_write_command "${run_dir}/run_command.sh" "${cmd[@]}"

cat <<EOF
[cj-eval] category : ${category}
[cj-eval] results  : ${run_dir}
[cj-eval] topology : ${topology}
[cj-eval] benchmark: ${benchmark}
[cj-eval] model    : ${CJ_EVAL_MODEL}
[cj-eval] dry-run  : ${dry_run}
[cj-eval] image    : ${image}
EOF
cj_eval_run_logged "${run_dir}/logs/run.log" "${cmd[@]}"

echo "[cj-eval] distributed reference smoke complete: ${run_dir}"
