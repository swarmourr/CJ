#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF'
Usage:
  evaluation/scripts/run_paper_grade_tests.sh [options]

Run the paper-grade validation tests in one organized result folder.

Default behavior is no-cost: safe unit/integration tests only.  Real gateway,
Docker image, infrastructure, and real-model E2E checks are explicit because
they require Docker, credentials, capabilities, or model quota.

Options:
  --all                 Build image and run safe, Docker, gateway, infra smoke,
                        and bounded real-model E2E checks.
  --build-image         Rebuild the evaluation Docker image for this worktree.
  --force-remove        When building, remove old cj-eval-agent tags first.
  --no-cache            Build without Docker cache.
  --with-docker         Run Docker integration tests.
  --with-gateway        Run real LLM gateway tests against the configured endpoint.
  --with-infra-smoke    Run one infrastructure smoke fault.
  --with-real-e2e       Run the bounded real-model smoke experiment.
  --env-file PATH       Load model config/secrets from PATH. Default: .env.
  --results-run NAME    Result folder under results/.
  --skip-safe           Skip safe unit/integration tests.
  --help                Show this help.

Environment:
  LLM_API_KEY / LLM_BASE_URL / LLM_MODEL, or CJ_EVAL_* equivalents.
  CJ_EVAL_INFRA_FAULT       Infrastructure smoke fault. Default: cpu_pressure.
  CJ_EVAL_REAL_FAULT_SUITE  Real smoke fault suite. Default inherited by script.

Examples:
  # No-cost validation only.
  evaluation/scripts/run_paper_grade_tests.sh

  # Everything currently implemented, including real gateway/model checks.
  evaluation/scripts/run_paper_grade_tests.sh --all --env-file .env

  # Only validate the configured real LLM gateway.
  evaluation/scripts/run_paper_grade_tests.sh --with-gateway --env-file .env
EOF
}

run_safe=1
run_docker=0
run_gateway=0
run_infra=0
run_real_e2e=0
build_image=0
build_mode="--keep-old"
no_cache=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --all)
      build_image=1
      run_docker=1
      run_gateway=1
      run_infra=1
      run_real_e2e=1
      shift
      ;;
    --build-image)
      build_image=1
      shift
      ;;
    --force-remove)
      build_mode="--force-remove"
      shift
      ;;
    --no-cache)
      no_cache=1
      shift
      ;;
    --with-docker)
      run_docker=1
      shift
      ;;
    --with-gateway)
      run_gateway=1
      shift
      ;;
    --with-infra-smoke)
      run_infra=1
      shift
      ;;
    --with-real-e2e)
      run_real_e2e=1
      shift
      ;;
    --env-file)
      export CJ_EVAL_ENV_FILE="$2"
      shift 2
      ;;
    --results-run)
      export CJ_EVAL_EXPERIMENT_RUN="$2"
      shift 2
      ;;
    --skip-safe)
      run_safe=0
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
  export CJ_EVAL_EXPERIMENT_RUN="paper-grade-tests-${CJ_EVAL_RUN_STAMP}"
fi
cj_eval_init_experiment_run

image="$(cj_eval_default_image)"
export CJ_EVAL_DOCKER_IMAGE="${image}"
export CJ_EVAL_IMAGE="${image}"

cat <<EOF
[cj-eval] paper-grade validation
[cj-eval] results root : $(cj_eval_results_root)
[cj-eval] run          : ${CJ_EVAL_EXPERIMENT_RUN}
[cj-eval] image        : ${image}
[cj-eval] safe tests   : ${run_safe}
[cj-eval] docker tests : ${run_docker}
[cj-eval] gateway      : ${run_gateway}
[cj-eval] infra smoke  : ${run_infra}
[cj-eval] real e2e     : ${run_real_e2e}
[cj-eval] model        : ${CJ_EVAL_MODEL}
[cj-eval] base url     : ${CJ_EVAL_BASE_URL}
EOF

if [[ "${build_image}" -eq 1 ]]; then
  build_dir="$(cj_eval_make_run_dir "docker-build" "evaluation-image")"
  build_cmd=(evaluation/docker/rebuild_image.sh "${build_mode}")
  if [[ "${no_cache}" -eq 1 ]]; then
    build_cmd+=(--no-cache)
  fi
  cj_eval_metadata "${build_dir}/metadata.json" "docker-build" "evaluation-image" "${image}"
  cj_eval_write_command "${build_dir}/build_command.sh" "${build_cmd[@]}"
  cj_eval_run_logged "${build_dir}/logs/docker_build.log" "${build_cmd[@]}"
elif [[ "${run_docker}" -eq 1 || "${run_infra}" -eq 1 || "${run_real_e2e}" -eq 1 ]]; then
  cj_eval_require_image "${image}"
else
  echo "[cj-eval] Docker image not required for selected test groups."
fi

if [[ "${run_safe}" -eq 1 ]]; then
  safe_dir="$(cj_eval_make_run_dir "stage-a-static-unit" "safe-suite")"
  safe_cmd=(
    "${CJ_EVAL_PYTHON}" -m pytest -q
    evaluation/tests
    tests/test_fault_verification.py
  )
  cj_eval_metadata "${safe_dir}/metadata.json" "stage-a-static-unit" "safe-suite" "${image}"
  cj_eval_write_command "${safe_dir}/safe_tests_command.sh" "${safe_cmd[@]}"
  cj_eval_run_logged "${safe_dir}/logs/safe_tests.log" "${safe_cmd[@]}"
fi

if [[ "${run_docker}" -eq 1 ]]; then
  "${script_dir}/run_docker_integration_tests.sh"
fi

if [[ "${run_gateway}" -eq 1 ]]; then
  gateway_dir="$(cj_eval_make_run_dir "stage-b-gateway" "real-llm-gateway")"
  gateway_cmd=(
    env
    "CJ_EVAL_RUN_GATEWAY_TESTS=1"
    "CJ_EVAL_ENV_FILE=$(cj_eval_default_env_file)"
    "PYTEST_ADDOPTS="
    "${CJ_EVAL_PYTHON}" -m pytest -q
    -m "gateway and real_eval"
    evaluation/tests/test_llm_gateway.py
  )
  cj_eval_metadata "${gateway_dir}/metadata.json" "stage-b-gateway" "real-llm-gateway" "${image}"
  cj_eval_write_command "${gateway_dir}/gateway_tests_command.sh" "${gateway_cmd[@]}"
  cj_eval_run_logged "${gateway_dir}/logs/gateway_tests.log" "${gateway_cmd[@]}"
fi

if [[ "${run_infra}" -eq 1 ]]; then
  CJ_EVAL_INFRA_FAULT="${CJ_EVAL_INFRA_FAULT:-cpu_pressure}" \
  CJ_EVAL_BENCHMARK="${CJ_EVAL_INFRA_BENCHMARK:-toy}" \
  CJ_EVAL_TASKS="${CJ_EVAL_INFRA_TASKS:-1}" \
  CJ_EVAL_DRY_RUN="${CJ_EVAL_INFRA_DRY_RUN:-1}" \
    "${script_dir}/run_infrastructure_fault_smoke.sh"
fi

if [[ "${run_real_e2e}" -eq 1 ]]; then
  "${script_dir}/run_real_llm_smoke.sh"
fi

echo "[cj-eval] paper-grade validation complete under $(cj_eval_results_root)"
