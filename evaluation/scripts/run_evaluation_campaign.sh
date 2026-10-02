#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF'
Usage:
  evaluation/scripts/run_evaluation_campaign.sh <mode>

Modes:
  verify       Run static/unit validation and ensure the Docker image exists.
  docker       Run Docker integration tests.
  injector     Run the CJ injector-validation campaign.
  overhead     Run the direct-vs-CJ-control overhead study.
  individual   Run the individual-agent resilience pilot.
  multi         Run the reference multi-agent pilot.
  smoke        Run the full smoke bundle: verify, injector, overhead, individual, multi.
  pilot        Run the broader pilot bundle from run_all_paper_scenarios.sh.
  real-smoke   Run the low-count real-LLM smoke script.
  help         Show this help.

Common environment overrides:
  CJ_EVAL_MODEL=minimax-m2
  CJ_EVAL_ENV_FILE=/absolute/path/to/.env
  CJ_EVAL_FAULT_SUITE=all
  CJ_EVAL_TASKS=1
  CJ_EVAL_REPEATS=1
  CJ_EVAL_REBUILD_IMAGE=1
  CJ_EVAL_AUTO_BUILD_IMAGE=0

Examples:
  cd evaluation
  scripts/run_evaluation_campaign.sh verify

  cd ..
  CJ_EVAL_FAULT_SUITE=all evaluation/scripts/run_evaluation_campaign.sh real-smoke

  CJ_EVAL_PROFILE=smoke evaluation/scripts/run_evaluation_campaign.sh smoke
EOF
}

mode="${1:-help}"

case "${mode}" in
  help|-h|--help)
    usage
    exit 0
    ;;
esac

cd "${CJ_EVAL_REPO_ROOT}"

ensure_image() {
  local image
  image="$(cj_eval_default_image)"

  if [[ "${CJ_EVAL_REBUILD_IMAGE:-0}" == "1" ]]; then
    evaluation/docker/rebuild_image.sh --force-remove
  else
    cj_eval_require_image "${image}"
  fi

  echo "[cj-eval] Docker image ready: ${image}"
}

case "${mode}" in
  verify)
    "${script_dir}/run_static_validation.sh"
    ensure_image
    ;;
  docker)
    ensure_image
    "${script_dir}/run_docker_integration_tests.sh"
    ;;
  injector)
    ensure_image
    "${script_dir}/run_injector_validation.sh"
    ;;
  overhead)
    ensure_image
    "${script_dir}/run_cj_overhead_study.sh"
    ;;
  individual)
    ensure_image
    "${script_dir}/run_individual_resilience_pilot.sh"
    ;;
  multi)
    ensure_image
    "${script_dir}/run_multi_agent_reference_pilot.sh"
    ;;
  smoke)
    export CJ_EVAL_PROFILE="${CJ_EVAL_PROFILE:-smoke}"
    "${script_dir}/run_all_paper_scenarios.sh"
    ;;
  pilot)
    export CJ_EVAL_PROFILE="${CJ_EVAL_PROFILE:-pilot}"
    "${script_dir}/run_all_paper_scenarios.sh"
    ;;
  real-smoke)
    ensure_image
    "${script_dir}/run_real_llm_smoke.sh"
    ;;
  *)
    echo "[cj-eval] unknown campaign mode: ${mode}" >&2
    usage >&2
    exit 2
    ;;
esac
