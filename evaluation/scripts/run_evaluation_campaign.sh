#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF'
Usage:
  evaluation/scripts/run_evaluation_campaign.sh <mode> [options]

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

Common options:
  --env-file PATH             Load model credentials/config from PATH.
  --model NAME                Model name, for example minimax-m2.
  --base-url URL              Host-reachable model endpoint.
  --container-base-url URL    Container-reachable model endpoint.
  --image IMAGE               Docker image tag to use.
  --system NAME               Single framework/system name.
  --systems LIST              Space/comma-separated systems for pilot modes.
  --benchmark NAME            Single benchmark name.
  --benchmarks LIST           Space/comma-separated benchmarks for pilot modes.
  --topology NAME             Single topology for multi mode.
  --topologies LIST           Space/comma-separated topologies for multi mode.
  --fault-suite NAME          Fault suite, for example smoke, all, llm_api.
  --tasks N                   Number of tasks.
  --repeats N                 Repetitions per task.
  --seed N                    Random seed.
  --max-turns N               Agent max turns.
  --container-timeout SEC     Container execution timeout.
  --score-timeout SEC         Scoring timeout.
  --cpus N                    Docker CPU limit.
  --memory SIZE               Docker memory limit, for example 2g.
  --proxy-port PORT           CJ proxy port.
  --results-root PATH         Root folder for generated results.
  --run-name NAME             Human-readable run name.
  --profile NAME              smoke or pilot, for bundle modes.
  --rebuild-image             Rebuild the Docker image before running.
  --no-auto-build             Fail if the image is missing.
  --help                      Show this help.

Secrets:
  Put LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL in an ignored .env file.
  This wrapper intentionally has no --api-key flag.

Examples:
  cd evaluation
  scripts/run_evaluation_campaign.sh verify

  scripts/run_evaluation_campaign.sh real-smoke \
    --env-file ../.env \
    --model minimax-m2 \
    --fault-suite all \
    --tasks 1

  scripts/run_evaluation_campaign.sh multi \
    --topologies "linear closed_loop" \
    --tasks 1
EOF
}

mode="${1:-help}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "${mode}" in
  help|-h|--help)
    usage
    exit 0
    ;;
esac

normalize_list() {
  echo "$1" | tr ',' ' '
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
    --base-url)
      export CJ_EVAL_BASE_URL="$2"
      shift 2
      ;;
    --container-base-url)
      export CJ_EVAL_CONTAINER_BASE_URL="$2"
      shift 2
      ;;
    --image|--docker-image)
      export CJ_EVAL_DOCKER_IMAGE="$2"
      shift 2
      ;;
    --system)
      export CJ_EVAL_SYSTEM="$2"
      export CJ_EVAL_SYSTEMS="$2"
      shift 2
      ;;
    --systems)
      export CJ_EVAL_SYSTEMS="$(normalize_list "$2")"
      shift 2
      ;;
    --benchmark)
      export CJ_EVAL_BENCHMARK="$2"
      export CJ_EVAL_BENCHMARKS="$2"
      shift 2
      ;;
    --benchmarks)
      export CJ_EVAL_BENCHMARKS="$(normalize_list "$2")"
      shift 2
      ;;
    --topology)
      export CJ_EVAL_TOPOLOGY="$2"
      export CJ_EVAL_TOPOLOGIES="$2"
      shift 2
      ;;
    --topologies)
      export CJ_EVAL_TOPOLOGIES="$(normalize_list "$2")"
      shift 2
      ;;
    --fault-suite)
      export CJ_EVAL_FAULT_SUITE="$2"
      export CJ_EVAL_FAULT_SUITES="$2"
      shift 2
      ;;
    --fault-suites)
      export CJ_EVAL_FAULT_SUITES="$(normalize_list "$2")"
      shift 2
      ;;
    --tasks)
      export CJ_EVAL_TASKS="$2"
      shift 2
      ;;
    --repeats)
      export CJ_EVAL_REPEATS="$2"
      shift 2
      ;;
    --seed)
      export CJ_EVAL_SEED="$2"
      shift 2
      ;;
    --max-turns)
      export CJ_EVAL_MAX_TURNS="$2"
      shift 2
      ;;
    --container-timeout)
      export CJ_EVAL_CONTAINER_TIMEOUT="$2"
      shift 2
      ;;
    --score-timeout)
      export CJ_EVAL_SCORE_TIMEOUT="$2"
      shift 2
      ;;
    --cpus)
      export CJ_EVAL_CPUS="$2"
      shift 2
      ;;
    --memory)
      export CJ_EVAL_MEMORY="$2"
      shift 2
      ;;
    --proxy-port)
      export CJ_EVAL_PROXY_PORT="$2"
      shift 2
      ;;
    --results-root)
      export CJ_EVAL_RESULTS_ROOT="$2"
      shift 2
      ;;
    --run-name)
      export CJ_EVAL_RUN_NAME="$2"
      shift 2
      ;;
    --profile)
      export CJ_EVAL_PROFILE="$2"
      shift 2
      ;;
    --rebuild-image)
      export CJ_EVAL_REBUILD_IMAGE=1
      shift
      ;;
    --no-auto-build)
      export CJ_EVAL_AUTO_BUILD_IMAGE=0
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --*)
      echo "[cj-eval] unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      echo "[cj-eval] unexpected argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

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
