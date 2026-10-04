#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF'
Usage:
  evaluation/scripts/run_evaluation_campaign.sh <mode> [options]
  evaluation/scripts/run_evaluation_campaign.sh scenario <name> [options]
  evaluation/scripts/run_evaluation_campaign.sh --scenario <name> [options]

Modes:
  scenarios    List preconfigured scenarios.
  scenario     Run one preconfigured scenario by name.
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

Preconfigured scenarios:
  no-cost-verify              Tests and image check; no model calls.
  docker-check                Docker integration validation.
  local-ollama-one-fault      Local Ollama, one task, latency triplet.
  local-ollama-injector-all   Local Ollama, one task, all proxy faults.
  real-minimax-one-fault      .env cloud model, one task, latency triplet.
  real-minimax-all-faults     .env cloud model, one task, all proxy faults.
  individual-mini             One AutoGen individual-agent latency smoke.
  individual-framework-smoke  Three individual frameworks, one latency task.
  multi-reference-mini        Reference multi-agent, both topologies, one task.
  paper-smoke                 Small bundle across implemented paper categories.

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
  --results-root PATH         Root folder for generated results; defaults to repo results/.
  --experiment-run NAME       Override the auto-generated experiment folder name.
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

  scripts/run_evaluation_campaign.sh scenarios

  scripts/run_evaluation_campaign.sh scenario real-minimax-one-fault

  scripts/run_evaluation_campaign.sh real-smoke \
    --env-file ../.env \
    --model minimax-m2 \
    --fault-suite all \
    --tasks 1

  scripts/run_evaluation_campaign.sh multi \
    --topologies "linear closed_loop" \
    --tasks 1

  scripts/run_evaluation_campaign.sh smoke
EOF
}

mode="${1:-help}"
if [[ $# -gt 0 ]]; then
  shift
fi

normalize_list() {
  echo "$1" | tr ',' ' '
}

print_scenarios() {
  cat <<'EOF'
Preconfigured evaluation scenarios:

  no-cost-verify
    Mode: verify
    Purpose: static/unit validation and Docker-image readiness. No model calls.

  docker-check
    Mode: docker
    Purpose: Docker runner/target integration validation.

  local-ollama-one-fault
    Mode: real-smoke
    Defaults: qwen2.5:latest, local Ollama, fault-suite=smoke, tasks=1.
    Purpose: one no-paid local triplet if Ollama is running.

  local-ollama-injector-all
    Mode: injector
    Defaults: qwen2.5:latest, local Ollama, fault-suite=all, tasks=1.
    Purpose: validate all connected proxy faults against a local endpoint.

  real-minimax-one-fault
    Mode: real-smoke
    Defaults: .env, minimax-m2, fault-suite=smoke, tasks=1.
    Purpose: tiny paid real-model triplet.

  real-minimax-all-faults
    Mode: real-smoke
    Defaults: .env, minimax-m2, fault-suite=all, tasks=1.
    Purpose: paid all-proxy-fault smoke on one task.

  individual-mini
    Mode: individual
    Defaults: autogen-real, humanevalplus, fault-suite=smoke, tasks=1.
    Purpose: smallest individual-agent resilience check.

  individual-framework-smoke
    Mode: individual
    Defaults: autogen-real/langgraph-real/crewai-real, humanevalplus,
              fault-suite=smoke, tasks=1.
    Purpose: compare individual framework wiring cheaply.

  multi-reference-mini
    Mode: multi
    Defaults: reference workflow, humanevalplus, linear + closed_loop,
              fault-suite=multi_agent, tasks=1.
    Purpose: smallest reference multi-agent propagation check.

  paper-smoke
    Mode: smoke
    Defaults: tasks=1, repeats=1.
    Purpose: broad small bundle across the implemented paper categories.

All scenario defaults can be overridden with ordinary flags after the scenario
name, for example:

  scripts/run_evaluation_campaign.sh scenario real-minimax-one-fault --tasks 2
EOF
}

apply_scenario() {
  local name="$1"
  export CJ_EVAL_SCENARIO="${name}"

  case "${name}" in
    no-cost-verify)
      echo "verify"
      ;;
    docker-check)
      echo "docker"
      ;;
    local-ollama-one-fault)
      export CJ_EVAL_ENV_FILE="/dev/null"
      export CJ_EVAL_MODEL="qwen2.5:latest"
      export CJ_EVAL_BASE_URL="http://127.0.0.1:11434/v1"
      export CJ_EVAL_CONTAINER_BASE_URL="http://host.docker.internal:11434/v1"
      export CJ_EVAL_FAULT_SUITE="smoke"
      export CJ_EVAL_TASKS="1"
      export CJ_EVAL_REPEATS="1"
      export CJ_EVAL_RUN_NAME="local-ollama-one-fault"
      echo "real-smoke"
      ;;
    local-ollama-injector-all)
      export CJ_EVAL_ENV_FILE="/dev/null"
      export CJ_EVAL_MODEL="qwen2.5:latest"
      export CJ_EVAL_BASE_URL="http://127.0.0.1:11434/v1"
      export CJ_EVAL_CONTAINER_BASE_URL="http://host.docker.internal:11434/v1"
      export CJ_EVAL_FAULT_SUITE="all"
      export CJ_EVAL_TASKS="1"
      export CJ_EVAL_REPEATS="1"
      export CJ_EVAL_RUN_NAME="local-ollama-injector-all"
      echo "injector"
      ;;
    real-minimax-one-fault)
      export CJ_EVAL_ENV_FILE="${CJ_EVAL_REPO_ROOT}/.env"
      export CJ_EVAL_MODEL="minimax-m2"
      export CJ_EVAL_FAULT_SUITE="smoke"
      export CJ_EVAL_TASKS="1"
      export CJ_EVAL_REPEATS="1"
      export CJ_EVAL_RUN_NAME="minimax-m2-one-fault"
      echo "real-smoke"
      ;;
    real-minimax-all-faults)
      export CJ_EVAL_ENV_FILE="${CJ_EVAL_REPO_ROOT}/.env"
      export CJ_EVAL_MODEL="minimax-m2"
      export CJ_EVAL_FAULT_SUITE="all"
      export CJ_EVAL_TASKS="1"
      export CJ_EVAL_REPEATS="1"
      export CJ_EVAL_RUN_NAME="minimax-m2-all-faults"
      echo "real-smoke"
      ;;
    individual-mini)
      export CJ_EVAL_SYSTEMS="autogen-real"
      export CJ_EVAL_BENCHMARKS="humanevalplus"
      export CJ_EVAL_FAULT_SUITES="smoke"
      export CJ_EVAL_TASKS="1"
      export CJ_EVAL_REPEATS="1"
      export CJ_EVAL_RUN_NAME="individual-mini"
      echo "individual"
      ;;
    individual-framework-smoke)
      export CJ_EVAL_SYSTEMS="autogen-real langgraph-real crewai-real"
      export CJ_EVAL_BENCHMARKS="humanevalplus"
      export CJ_EVAL_FAULT_SUITES="smoke"
      export CJ_EVAL_TASKS="1"
      export CJ_EVAL_REPEATS="1"
      export CJ_EVAL_RUN_NAME="individual-framework-smoke"
      echo "individual"
      ;;
    multi-reference-mini)
      export CJ_EVAL_SYSTEMS="autogen-real"
      export CJ_EVAL_TOPOLOGIES="linear closed_loop"
      export CJ_EVAL_BENCHMARKS="humanevalplus"
      export CJ_EVAL_FAULT_SUITE="multi_agent"
      export CJ_EVAL_TASKS="1"
      export CJ_EVAL_REPEATS="1"
      export CJ_EVAL_RUN_NAME="multi-reference-mini"
      echo "multi"
      ;;
    paper-smoke)
      export CJ_EVAL_PROFILE="smoke"
      export CJ_EVAL_TASKS="1"
      export CJ_EVAL_REPEATS="1"
      echo "smoke"
      ;;
    *)
      echo "[cj-eval] unknown scenario: ${name}" >&2
      print_scenarios >&2
      exit 2
      ;;
  esac
}

case "${mode}" in
  help|-h|--help)
    usage
    exit 0
    ;;
  scenarios|list-scenarios)
    print_scenarios
    exit 0
    ;;
esac

if [[ "${mode}" == "--scenario" ]]; then
  mode="scenario"
fi

if [[ "${mode}" == "scenario" ]]; then
  if [[ $# -eq 0 ]]; then
    echo "[cj-eval] scenario mode requires a scenario name" >&2
    print_scenarios >&2
    exit 2
  fi
  scenario_name="$1"
  shift
  mode="$(apply_scenario "${scenario_name}")"
  echo "[cj-eval] scenario : ${scenario_name}"
  echo "[cj-eval] mode     : ${mode}"
fi

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
    --experiment-run|--experiment)
      export CJ_EVAL_EXPERIMENT_RUN="$2"
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
export CJ_EVAL_RUN_STAMP="${CJ_EVAL_RUN_STAMP:-$(cj_eval_timestamp)}"
export CJ_EVAL_EXPERIMENT_CREATED_AT="${CJ_EVAL_EXPERIMENT_CREATED_AT:-$(cj_eval_timestamp)}"
if [[ -z "${CJ_EVAL_EXPERIMENT_RUN:-}" ]]; then
  export CJ_EVAL_EXPERIMENT_RUN="${CJ_EVAL_SCENARIO:-${mode}}-${CJ_EVAL_RUN_STAMP}"
fi
cj_eval_init_experiment_run

cat <<EOF
[cj-eval] experiment run: ${CJ_EVAL_EXPERIMENT_RUN}
[cj-eval] results root  : $(cj_eval_results_root)
EOF

ensure_image() {
  local image
  image="$(cj_eval_default_image)"

  if [[ "${CJ_EVAL_REBUILD_IMAGE:-0}" == "1" ]]; then
    evaluation/docker/rebuild_image.sh --force-remove
  else
    cj_eval_require_image "${image}"
  fi

  echo "[cj-eval] Docker image ready: ${image}"
  export CJ_EVAL_DOCKER_IMAGE="${image}"
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

exit 0
