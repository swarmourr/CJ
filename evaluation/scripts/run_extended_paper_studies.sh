#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"
cd "${CJ_EVAL_REPO_ROOT}"

env_file="${CJ_EVAL_ENV_FILE:-.env}"
model="${CJ_EVAL_MODEL:-}"
base_url="${CJ_EVAL_BASE_URL:-}"
container_base_url="${CJ_EVAL_CONTAINER_BASE_URL:-}"
results_run="${CJ_EVAL_EXPERIMENT_RUN:-extended-evaluation-$(cj_eval_timestamp)}"
skip_e8=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) env_file="$2"; shift 2 ;;
    --model) model="$2"; shift 2 ;;
    --base-url) base_url="$2"; shift 2 ;;
    --container-base-url) container_base_url="$2"; shift 2 ;;
    --results-run) results_run="$2"; shift 2 ;;
    --skip-e8) skip_e8=1; shift ;;
    -h|--help)
      cat <<'EOF'
Run additive E7-E9 paper studies without changing canonical results.

Usage:
  evaluation/scripts/run_extended_paper_studies.sh [options]

Options:
  --env-file PATH           dotenv file for E8 model access (default: .env)
  --model NAME              model for E8 (defaults to LLM_MODEL from dotenv)
  --base-url URL            host-reachable /v1 URL (defaults to LLM_BASE_URL)
  --container-base-url URL  container-reachable /v1 URL (defaults to base URL)
  --results-run NAME        results folder name
  --skip-e8                 run no paid/remote cross-layer request

Optional environment controls:
  CJ_EVAL_E7_CYCLES=3
  CJ_EVAL_E8_CYCLES=1
  CJ_EVAL_E9_REQUESTS=200
  CJ_EVAL_E9_ROUNDS=5
  CJ_EVAL_E9_CONCURRENCY=8
  CJ_EVAL_E9_CYCLES=30
  CJ_EVAL_RESUME=1
EOF
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

export CJ_EVAL_ENV_FILE="${env_file}"
cj_eval_load_dotenv
model="${model:-${LLM_MODEL:-minimax-m2}}"
base_url="${base_url:-${LLM_BASE_URL:-}}"
container_base_url="${container_base_url:-${base_url}}"
export CJ_EVAL_MODEL="${model}"
export CJ_EVAL_BASE_URL="${base_url}"
export CJ_EVAL_CONTAINER_BASE_URL="${container_base_url}"
export CJ_EVAL_EXPERIMENT_RUN="${results_run}"
export CJ_EVAL_EXPERIMENT_CREATED_AT="${CJ_EVAL_EXPERIMENT_CREATED_AT:-$(cj_eval_timestamp)}"
cj_eval_init_experiment_run

image="${CJ_EVAL_DOCKER_IMAGE:-$(cj_eval_default_image)}"
if ! docker image inspect "${image}" >/dev/null 2>&1; then
  echo "[cj-eval] Docker image missing; building ${image}"
  CJ_EVAL_IMAGE_NAME="${image%%:*}" \
    CJ_EVAL_IMAGE_TAG="${image#*:}" \
    evaluation/docker/rebuild_image.sh
fi
if ! docker image inspect "${image}" >/dev/null 2>&1; then
  echo "[cj-eval] Docker image is still unavailable after build: ${image}" >&2
  exit 1
fi
export CJ_EVAL_DOCKER_IMAGE="${image}"

root="$(cj_eval_results_root)/extended-evaluation"
mkdir -p "${root}"

run_stage() {
  local index="$1"
  local total="$2"
  local key="$3"
  local title="$4"
  local output_dir="$5"
  shift 5
  echo
  echo "================================================================"
  echo "[cj-eval] [${index}/${total}] ${title}"
  echo "[cj-eval] output: ${output_dir}"
  echo "================================================================"
  if cj_eval_checkpoint_done "${key}"; then
    cj_eval_skip_checkpoint_message "${key}" "${title}"
    return 0
  fi
  mkdir -p "${output_dir}/logs"
  cj_eval_write_command "${output_dir}/run_command.sh" "$@"
  cj_eval_run_logged "${output_dir}/logs/run.log" "$@"
  cj_eval_mark_checkpoint_done "${key}" "${title}"
}

run_stage 1 3 "extended-e7" "E7 - distributed coordination" "${root}/E7-distributed-coordination" \
  "${CJ_EVAL_PYTHON}" -m evaluation.extended_campaign e7 \
  --docker-image "${image}" \
  --results-dir "${root}/E7-distributed-coordination" \
  --cycles "${CJ_EVAL_E7_CYCLES:-3}" \
  --roles "${CJ_EVAL_E7_ROLES:-planner,coder,reviewer}" \
  --maximum-skew-ms "${CJ_EVAL_E7_MAXIMUM_SKEW_MS:-100}" \
  --start-after "${CJ_EVAL_E7_START_AFTER_S:-0.5}" \
  --stress-duration "${CJ_EVAL_E7_STRESS_DURATION_S:-20}" \
  --cpus "${CJ_EVAL_CPUS:-1}" \
  --memory "${CJ_EVAL_MEMORY:-1g}"

if [[ "${skip_e8}" == "1" ]]; then
  echo "[cj-eval] [2/3] E8 explicitly skipped by --skip-e8"
else
  if [[ -z "${base_url}" || -z "${container_base_url}" ]]; then
    echo "[cj-eval] E8 requires LLM_BASE_URL or --base-url and a container URL" >&2
    exit 2
  fi
  run_stage 2 3 "extended-e8" "E8 - cross-layer chaos" "${root}/E8-cross-layer-chaos" \
    "${CJ_EVAL_PYTHON}" -m evaluation.extended_campaign e8 \
    --docker-image "${image}" \
    --results-dir "${root}/E8-cross-layer-chaos" \
    --upstream "${base_url}" \
    --container-upstream "${container_base_url}" \
    --model "${model}" \
    --env-file "${env_file}" \
    --cycles "${CJ_EVAL_E8_CYCLES:-1}" \
    --proxy-port "${CJ_EVAL_E8_PROXY_PORT:-20380}" \
    --delay-s "${CJ_EVAL_E8_DELAY_S:-0.5}" \
    --maximum-skew-ms "${CJ_EVAL_E8_MAXIMUM_SKEW_MS:-250}" \
    --cpus "${CJ_EVAL_CPUS:-1}" \
    --memory "${CJ_EVAL_MEMORY:-2g}"
fi

run_stage 3 3 "extended-e9" "E9 - performance and restoration" "${root}/E9-performance-restoration" \
  "${CJ_EVAL_PYTHON}" -m evaluation.extended_campaign e9 \
  --results-dir "${root}/E9-performance-restoration" \
  --requests "${CJ_EVAL_E9_REQUESTS:-200}" \
  --rounds "${CJ_EVAL_E9_ROUNDS:-5}" \
  --warmup-requests "${CJ_EVAL_E9_WARMUP_REQUESTS:-10}" \
  --concurrency "${CJ_EVAL_E9_CONCURRENCY:-8}" \
  --cycles "${CJ_EVAL_E9_CYCLES:-30}" \
  --delay-s "${CJ_EVAL_E9_DELAY_S:-0.05}"

echo
echo "[cj-eval] E7-E9 extended studies complete"
echo "[cj-eval] results: ${root}"
