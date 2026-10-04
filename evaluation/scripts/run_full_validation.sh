#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF'
Usage:
  evaluation/scripts/run_full_validation.sh [options]

Build the current evaluation Docker image, then run the complete local
validation stack:

  1. Docker image build for the current commit.
  2. Static/unit validation, including the old and new safe tests.
  3. Docker integration tests.
  4. Container-scoped infrastructure smoke, dry-run by default.

This script does not run a paid real-model smoke unless explicitly requested.

Options:
  --force-remove          Remove old cj-eval-agent tags before building.
  --keep-old              Keep old image tags while building. Default.
  --no-cache              Build the Docker image without Docker layer cache.
  --skip-docker-tests     Build image and run static/unit tests only.
  --skip-infra-smoke      Skip the infrastructure smoke.
  --real-smoke            Also run the bounded real-LLM smoke script.
  --help                  Show this help.

Environment:
  CJ_EVAL_EXPERIMENT_RUN  Optional result folder name.
  CJ_EVAL_INFRA_FAULT     Infrastructure smoke fault. Default: cpu_pressure.
  CJ_EVAL_ENV_FILE        Optional .env file for model settings.

Examples:
  evaluation/scripts/run_full_validation.sh
  evaluation/scripts/run_full_validation.sh --force-remove
  evaluation/scripts/run_full_validation.sh --no-cache --real-smoke
EOF
}

build_mode="--keep-old"
no_cache=0
run_docker_tests=1
run_infra_smoke=1
run_real_smoke=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force-remove)
      build_mode="--force-remove"
      shift
      ;;
    --keep-old)
      build_mode="--keep-old"
      shift
      ;;
    --no-cache)
      no_cache=1
      shift
      ;;
    --skip-docker-tests)
      run_docker_tests=0
      shift
      ;;
    --skip-infra-smoke)
      run_infra_smoke=0
      shift
      ;;
    --real-smoke)
      run_real_smoke=1
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
export CJ_EVAL_RUN_STAMP="${CJ_EVAL_RUN_STAMP:-$(cj_eval_timestamp)}"
export CJ_EVAL_EXPERIMENT_CREATED_AT="${CJ_EVAL_EXPERIMENT_CREATED_AT:-$(cj_eval_timestamp)}"
if [[ -z "${CJ_EVAL_EXPERIMENT_RUN:-}" ]]; then
  export CJ_EVAL_EXPERIMENT_RUN="full-validation-${CJ_EVAL_RUN_STAMP}"
fi
cj_eval_init_experiment_run

image="$(cj_eval_default_image)"
build_dir="$(cj_eval_make_run_dir "docker-build" "evaluation-image")"
build_cmd=(evaluation/docker/rebuild_image.sh "${build_mode}")
if [[ "${no_cache}" -eq 1 ]]; then
  build_cmd+=(--no-cache)
fi

cat <<EOF
[cj-eval] full validation
[cj-eval] results root : $(cj_eval_results_root)
[cj-eval] run          : ${CJ_EVAL_EXPERIMENT_RUN}
[cj-eval] image        : ${image}
[cj-eval] build mode   : ${build_mode}
[cj-eval] docker tests : ${run_docker_tests}
[cj-eval] infra smoke  : ${run_infra_smoke}
[cj-eval] real smoke   : ${run_real_smoke}
EOF

cj_eval_metadata "${build_dir}/metadata.json" "docker-build" "evaluation-image" "${image}"
cj_eval_write_command "${build_dir}/build_command.sh" "${build_cmd[@]}"
cj_eval_run_logged "${build_dir}/logs/docker_build.log" "${build_cmd[@]}"

export CJ_EVAL_DOCKER_IMAGE="${image}"

"${script_dir}/run_static_validation.sh"

if [[ "${run_docker_tests}" -eq 1 ]]; then
  "${script_dir}/run_docker_integration_tests.sh"
else
  echo "[cj-eval] Docker integration tests skipped by --skip-docker-tests"
fi

if [[ "${run_infra_smoke}" -eq 1 ]]; then
  CJ_EVAL_INFRA_FAULT="${CJ_EVAL_INFRA_FAULT:-cpu_pressure}" \
  CJ_EVAL_BENCHMARK="${CJ_EVAL_INFRA_BENCHMARK:-toy}" \
  CJ_EVAL_TASKS="${CJ_EVAL_INFRA_TASKS:-1}" \
  CJ_EVAL_DRY_RUN="${CJ_EVAL_INFRA_DRY_RUN:-1}" \
    "${script_dir}/run_infrastructure_fault_smoke.sh"
else
  echo "[cj-eval] Infrastructure smoke skipped by --skip-infra-smoke"
fi

if [[ "${run_real_smoke}" -eq 1 ]]; then
  "${script_dir}/run_real_llm_smoke.sh"
else
  echo "[cj-eval] Real-model smoke skipped. Use --real-smoke to opt in."
fi

echo "[cj-eval] full validation complete under $(cj_eval_results_root)"
