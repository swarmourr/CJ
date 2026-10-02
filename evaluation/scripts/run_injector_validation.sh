#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

cd "${CJ_EVAL_REPO_ROOT}"
cj_eval_model_defaults

if [[ "${CJ_EVAL_REBUILD_IMAGE:-0}" == "1" ]]; then
  evaluation/docker/rebuild_image.sh --force-remove
fi

image="$(cj_eval_default_image)"
cj_eval_require_image "${image}"

category="injector-validation"
name="${CJ_EVAL_RUN_NAME:-${CJ_EVAL_MODEL}-all-proxy-faults}"
run_dir="$(cj_eval_make_run_dir "${category}" "${name}")"
cj_eval_metadata "${run_dir}/study_manifest.json" "${category}" "${name}" "${image}"

system="${CJ_EVAL_SYSTEM:-autogen-real}"
benchmark="${CJ_EVAL_BENCHMARK:-humanevalplus}"
fault_suite="${CJ_EVAL_FAULT_SUITE:-all}"
tasks="${CJ_EVAL_TASKS:-1}"
repeats="${CJ_EVAL_REPEATS:-1}"
seed="${CJ_EVAL_SEED:-11}"
env_file="$(cj_eval_default_env_file)"
container_timeout="${CJ_EVAL_CONTAINER_TIMEOUT:-240}"
score_timeout="${CJ_EVAL_SCORE_TIMEOUT:-20}"
max_turns="${CJ_EVAL_MAX_TURNS:-3}"
cpus="${CJ_EVAL_CPUS:-1}"
memory="${CJ_EVAL_MEMORY:-2g}"
proxy_port="${CJ_EVAL_PROXY_PORT:-18089}"

cmd=(
  "${CJ_EVAL_PYTHON}" -m evaluation.run
  --publication-study
  --docker-image "${image}"
  --system "${system}"
  --agent-level individual
  --topology single
  --benchmark "${benchmark}"
  --fault-suite "${fault_suite}"
  --tasks "${tasks}"
  --repeats "${repeats}"
  --seed "${seed}"
  --base-url "${CJ_EVAL_BASE_URL}"
  --container-base-url "${CJ_EVAL_CONTAINER_BASE_URL}"
  --model "${CJ_EVAL_MODEL}"
  --env-file "${env_file}"
  --results-dir "${run_dir}"
  --container-timeout "${container_timeout}"
  --score-timeout "${score_timeout}"
  --max-turns "${max_turns}"
  --cpus "${cpus}"
  --memory "${memory}"
  --proxy-port "${proxy_port}"
)

if [[ -n "${CJ_EVAL_STUDY_ID:-}" ]]; then
  cmd+=(--study-id "${CJ_EVAL_STUDY_ID}")
fi

cj_eval_write_command "${run_dir}/run_command.sh" "${cmd[@]}"

echo "[cj-eval] category : ${category}"
echo "[cj-eval] results  : ${run_dir}"
echo "[cj-eval] faults   : ${fault_suite}"
cj_eval_run_logged "${run_dir}/logs/run.log" "${cmd[@]}"

echo "[cj-eval] injector-validation run complete: ${run_dir}"
