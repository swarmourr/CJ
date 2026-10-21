#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF'
Usage:
  evaluation/scripts/run_paper_grade_tests.sh [options]

Run the paper-grade validation tests in one organized result folder.
Each selected stage prints a title and an end-to-end progress bar.

Default behavior is no-cost: safe unit/integration tests only.  Real gateway,
Docker image, infrastructure, and real-model E2E checks are explicit because
they require Docker, credentials, capabilities, or model quota.

Options:
  --all                 Build image and run safe, Docker, gateway, infra smoke,
                        and bounded real-model E2E checks.
  --build-image         Rebuild the evaluation Docker image for this worktree.
  --force-remove        When building, remove old cj-eval-agent tags first.
  --no-cache            Build without Docker cache.
  --debug-image         Inspect/debug the evaluation Docker image and run one
                        dry container-entrypoint request with no model calls.
  --with-docker         Run Docker integration tests.
  --with-gateway        Run real LLM gateway tests against the configured endpoint.
  --with-infra-smoke    Run one infrastructure smoke fault.
  --with-real-e2e       Run the bounded real-model smoke experiment.
  --with-tool-triggered Run controlled tool-use workload for tool-fault trigger
                        coverage.
  --with-all-scenarios  Run every currently implemented evaluation scenario
                        through run_all_current_scenarios.sh.
  --with-missing        Run the missing paper experiment bundle once. This runs
                        implemented missing categories, including distributed
                        communication/role-local faults, distributed-native
                        multi-agent smoke, durable-state validation, native
                        framework multi-agent pilots, comparator gating, and
                        explicit unsupported artifacts only for categories
                        still not implemented.
  --paper-all           Build image, debug image, run validation, gateway, and
                        all current + missing scenario categories in one result
                        folder.
  --all-possible        Alias for --paper-all.
  --env-file PATH       Load model config/secrets from PATH. Default: .env.
  --results-run NAME    Result folder under results/.
  --no-resume           Ignore existing checkpoints in the selected results run.
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

  # Full paper-grade validation plus every current real scenario category.
  evaluation/scripts/run_paper_grade_tests.sh --paper-all --env-file .env

  # Same as --paper-all, named for one-shot end-to-end use.
  evaluation/scripts/run_paper_grade_tests.sh --all-possible --env-file .env

  # Only validate the configured real LLM gateway.
  evaluation/scripts/run_paper_grade_tests.sh --with-gateway --env-file .env
EOF
}

run_safe=1
run_docker=0
run_gateway=0
run_infra=0
run_real_e2e=0
run_tool_triggered=0
run_all_scenarios=0
run_image_debug=0
run_missing=0
build_image=0
build_mode="--keep-old"
no_cache=0
resume_enabled=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --all)
      build_image=1
      run_image_debug=1
      run_docker=1
      run_gateway=1
      run_infra=1
      run_real_e2e=1
      run_tool_triggered=1
      shift
      ;;
    --paper-all|--all-possible)
      build_image=1
      run_image_debug=1
      run_docker=1
      run_gateway=1
      run_all_scenarios=1
      run_missing=1
      run_tool_triggered=1
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
    --debug-image)
      run_image_debug=1
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
    --with-tool-triggered)
      run_tool_triggered=1
      shift
      ;;
    --with-all-scenarios)
      run_all_scenarios=1
      shift
      ;;
    --with-missing)
      run_missing=1
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
    --no-resume)
      resume_enabled=0
      shift
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

export CJ_EVAL_RESUME="${resume_enabled}"

cj_eval_progress_bar() {
  local current="$1"
  local total="$2"
  local width="${CJ_EVAL_PROGRESS_WIDTH:-28}"
  local filled=0
  local empty=0
  local bar=""
  local i
  if [[ "${total}" -gt 0 ]]; then
    filled=$((current * width / total))
  fi
  empty=$((width - filled))
  for ((i = 0; i < filled; i++)); do
    bar="${bar}#"
  done
  for ((i = 0; i < empty; i++)); do
    bar="${bar}-"
  done
  printf '%s' "${bar}"
}

cj_eval_stage_index=0
cj_eval_stage_total=0
cj_eval_stage() {
  local title="$1"
  cj_eval_stage_index=$((cj_eval_stage_index + 1))
  local bar
  bar="$(cj_eval_progress_bar "${cj_eval_stage_index}" "${cj_eval_stage_total}")"
  printf '\n[cj-eval] [%02d/%02d] [%s] %s\n' \
    "${cj_eval_stage_index}" "${cj_eval_stage_total}" "${bar}" "${title}"
}

cj_eval_checkpoint_dir() {
  echo "$(cj_eval_results_root)/.checkpoints"
}

cj_eval_checkpoint_path() {
  local key="$1"
  echo "$(cj_eval_checkpoint_dir)/$(cj_eval_slug "${key}").done.json"
}

cj_eval_mark_stage_done() {
  local key="$1"
  local title="$2"
  local path
  path="$(cj_eval_checkpoint_path "${key}")"
  mkdir -p "$(dirname "${path}")"
  cat > "${path}.tmp" <<EOF
{
  "stage_key": $(cj_eval_json_string "${key}"),
  "stage_title": $(cj_eval_json_string "${title}"),
  "completed_at_utc": $(cj_eval_json_string "$(cj_eval_timestamp)"),
  "repo_commit": $(cj_eval_json_string "$(cj_eval_commit)"),
  "docker_image": $(cj_eval_json_string "${CJ_EVAL_DOCKER_IMAGE:-}"),
  "results_run": $(cj_eval_json_string "${CJ_EVAL_EXPERIMENT_RUN:-}")
}
EOF
  mv "${path}.tmp" "${path}"
}

cj_eval_run_checkpointed_stage() {
  local key="$1"
  local title="$2"
  shift 2
  local checkpoint
  checkpoint="$(cj_eval_checkpoint_path "${key}")"
  cj_eval_stage "${title}"
  if [[ "${resume_enabled}" -eq 1 && -f "${checkpoint}" ]]; then
    echo "[cj-eval] checkpoint exists; skipping completed stage: ${key}"
    echo "[cj-eval] checkpoint: ${checkpoint}"
    return 0
  fi
  echo "[cj-eval] checkpoint key: ${key}"
  "$@"
  cj_eval_mark_stage_done "${key}" "${title}"
  echo "[cj-eval] checkpoint written: ${checkpoint}"
}

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

image_required=0
if [[ "${run_image_debug}" -eq 1 || "${run_docker}" -eq 1 || "${run_infra}" -eq 1 || "${run_real_e2e}" -eq 1 || "${run_all_scenarios}" -eq 1 || "${run_missing}" -eq 1 ]]; then
  image_required=1
fi
if [[ "${run_tool_triggered}" -eq 1 ]]; then
  image_required=1
fi
cj_eval_stage_total=0
if [[ "${build_image}" -eq 1 || "${image_required}" -eq 1 ]]; then
  cj_eval_stage_total=$((cj_eval_stage_total + 1))
fi
[[ "${run_safe}" -eq 1 ]] && cj_eval_stage_total=$((cj_eval_stage_total + 1))
[[ "${run_image_debug}" -eq 1 ]] && cj_eval_stage_total=$((cj_eval_stage_total + 1))
[[ "${run_docker}" -eq 1 ]] && cj_eval_stage_total=$((cj_eval_stage_total + 1))
[[ "${run_gateway}" -eq 1 ]] && cj_eval_stage_total=$((cj_eval_stage_total + 1))
[[ "${run_infra}" -eq 1 ]] && cj_eval_stage_total=$((cj_eval_stage_total + 1))
[[ "${run_real_e2e}" -eq 1 ]] && cj_eval_stage_total=$((cj_eval_stage_total + 1))
[[ "${run_tool_triggered}" -eq 1 ]] && cj_eval_stage_total=$((cj_eval_stage_total + 1))
[[ "${run_all_scenarios}" -eq 1 ]] && cj_eval_stage_total=$((cj_eval_stage_total + 1))
[[ "${run_missing}" -eq 1 ]] && cj_eval_stage_total=$((cj_eval_stage_total + 1))
if [[ "${cj_eval_stage_total}" -eq 0 ]]; then
  cj_eval_stage_total=1
fi

cat <<EOF
[cj-eval] paper-grade validation
[cj-eval] results root : $(cj_eval_results_root)
[cj-eval] run          : ${CJ_EVAL_EXPERIMENT_RUN}
[cj-eval] image        : ${image}
[cj-eval] image debug  : ${run_image_debug}
[cj-eval] safe tests   : ${run_safe}
[cj-eval] docker tests : ${run_docker}
[cj-eval] gateway      : ${run_gateway}
[cj-eval] infra smoke  : ${run_infra}
[cj-eval] real e2e     : ${run_real_e2e}
[cj-eval] tool trigger : ${run_tool_triggered}
[cj-eval] all scenarios: ${run_all_scenarios}
[cj-eval] missing exp. : ${run_missing}
[cj-eval] model        : ${CJ_EVAL_MODEL}
[cj-eval] base url     : ${CJ_EVAL_BASE_URL}
[cj-eval] stages       : ${cj_eval_stage_total}
[cj-eval] resume       : ${resume_enabled}
EOF

stage_docker_image_build() {
  build_dir="$(cj_eval_make_run_dir "docker-build" "evaluation-image")"
  build_cmd=(evaluation/docker/rebuild_image.sh "${build_mode}")
  if [[ "${no_cache}" -eq 1 ]]; then
    build_cmd+=(--no-cache)
  fi
  cj_eval_metadata "${build_dir}/metadata.json" "docker-build" "evaluation-image" "${image}"
  cj_eval_write_command "${build_dir}/build_command.sh" "${build_cmd[@]}"
  cj_eval_run_logged "${build_dir}/logs/docker_build.log" "${build_cmd[@]}"
}

stage_docker_image_availability() {
  cj_eval_require_image "${image}"
}

stage_static_unit_validation() {
  safe_dir="$(cj_eval_make_run_dir "stage-a-static-unit" "safe-suite")"
  safe_cmd=(
    "${CJ_EVAL_PYTHON}" -m pytest -q
    evaluation/tests
    tests/test_fault_verification.py
  )
  cj_eval_metadata "${safe_dir}/metadata.json" "stage-a-static-unit" "safe-suite" "${image}"
  cj_eval_write_command "${safe_dir}/safe_tests_command.sh" "${safe_cmd[@]}"
  cj_eval_run_logged "${safe_dir}/logs/safe_tests.log" "${safe_cmd[@]}"
}

stage_docker_image_debug() {
  "${script_dir}/debug_docker_image.sh" --image "${image}" --run-name "evaluation-image"
}

stage_docker_integration_tests() {
  "${script_dir}/run_docker_integration_tests.sh"
}

stage_llm_gateway_tests() {
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
}

stage_infrastructure_smoke() {
  CJ_EVAL_INFRA_FAULT="${CJ_EVAL_INFRA_FAULT:-cpu_pressure}" \
  CJ_EVAL_BENCHMARK="${CJ_EVAL_INFRA_BENCHMARK:-toy}" \
  CJ_EVAL_TASKS="${CJ_EVAL_INFRA_TASKS:-1}" \
  CJ_EVAL_DRY_RUN="${CJ_EVAL_INFRA_DRY_RUN:-1}" \
    "${script_dir}/run_infrastructure_fault_smoke.sh"
}

stage_real_model_e2e_smoke() {
  "${script_dir}/run_real_llm_smoke.sh"
}

stage_tool_triggered_workload() {
  "${script_dir}/run_tool_triggered_workload.sh"
}

stage_all_current_scenarios() {
  CJ_EVAL_REBUILD_IMAGE="${CJ_EVAL_REBUILD_IMAGE:-0}" \
    "${script_dir}/run_all_current_scenarios.sh"
}

stage_missing_paper_bundle() {
  "${script_dir}/run_missing_paper_experiments.sh"
}

if [[ "${build_image}" -eq 1 ]]; then
  cj_eval_run_checkpointed_stage "docker-image-build" "Docker image build" stage_docker_image_build
elif [[ "${image_required}" -eq 1 ]]; then
  cj_eval_run_checkpointed_stage "docker-image-availability" "Docker image availability" stage_docker_image_availability
else
  echo "[cj-eval] Docker image not required for selected test groups."
fi

if [[ "${run_safe}" -eq 1 ]]; then
  cj_eval_run_checkpointed_stage "static-unit-validation" "Static and unit validation" stage_static_unit_validation
fi

if [[ "${run_image_debug}" -eq 1 ]]; then
  cj_eval_run_checkpointed_stage "docker-image-debug" "Docker image debug" stage_docker_image_debug
fi

if [[ "${run_docker}" -eq 1 ]]; then
  cj_eval_run_checkpointed_stage "docker-integration-tests" "Docker integration tests" stage_docker_integration_tests
fi

if [[ "${run_gateway}" -eq 1 ]]; then
  cj_eval_run_checkpointed_stage "llm-gateway-tests" "LLM gateway tests" stage_llm_gateway_tests
fi

if [[ "${run_infra}" -eq 1 ]]; then
  cj_eval_run_checkpointed_stage "infrastructure-smoke" "Infrastructure smoke" stage_infrastructure_smoke
fi

if [[ "${run_real_e2e}" -eq 1 ]]; then
  cj_eval_run_checkpointed_stage "real-model-e2e-smoke" "Real-model E2E smoke" stage_real_model_e2e_smoke
fi

if [[ "${run_tool_triggered}" -eq 1 ]]; then
  cj_eval_run_checkpointed_stage "tool-triggered-workload" "Tool-triggered controlled workload" stage_tool_triggered_workload
fi

if [[ "${run_all_scenarios}" -eq 1 ]]; then
  cj_eval_run_checkpointed_stage "all-current-evaluation-scenarios" "All current evaluation scenarios" stage_all_current_scenarios
fi

if [[ "${run_missing}" -eq 1 ]]; then
  cj_eval_run_checkpointed_stage "missing-paper-experiment-bundle" "Missing paper experiment bundle" stage_missing_paper_bundle
fi

echo
echo "[cj-eval] [${cj_eval_stage_total}/${cj_eval_stage_total}] [$(cj_eval_progress_bar "${cj_eval_stage_total}" "${cj_eval_stage_total}")] complete"
echo "[cj-eval] paper-grade validation complete under $(cj_eval_results_root)"
