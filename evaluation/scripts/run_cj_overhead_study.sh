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

category="cj-overhead"
name="${CJ_EVAL_RUN_NAME:-${CJ_EVAL_MODEL}-direct-vs-control}"
system="${CJ_EVAL_SYSTEM:-autogen-real}"
benchmark="${CJ_EVAL_BENCHMARK:-humanevalplus}"
benchmark_subset="${CJ_EVAL_BENCHMARK_SUBSET:-auto}"
tasks="${CJ_EVAL_TASKS:-5}"
repeats="${CJ_EVAL_REPEATS:-1}"
seed="${CJ_EVAL_SEED:-301}"
task_selection_seed="${CJ_EVAL_TASK_SELECTION_SEED:-17}"
env_file="$(cj_eval_default_env_file)"
container_timeout="${CJ_EVAL_CONTAINER_TIMEOUT:-240}"
score_timeout="${CJ_EVAL_SCORE_TIMEOUT:-20}"
max_turns="${CJ_EVAL_MAX_TURNS:-3}"
cpus="${CJ_EVAL_CPUS:-1}"
memory="${CJ_EVAL_MEMORY:-2g}"
proxy_port="${CJ_EVAL_PROXY_PORT:-18300}"
checkpoint_key="${category}:${name}:system=${system}:benchmark=${benchmark}:subset=${benchmark_subset}:tasks=${tasks}:repeats=${repeats}:seed=${seed}:task-seed=${task_selection_seed}:image=${image}"
checkpoint_title="CJ overhead ${system} ${benchmark}"
if cj_eval_checkpoint_done "${checkpoint_key}"; then
  cj_eval_skip_checkpoint_message "${checkpoint_key}" "${checkpoint_title}"
  exit 0
fi
if cj_eval_backfill_checkpoint_from_run_dir "${checkpoint_key}" "${checkpoint_title}" "${category}" "${name}" "runs.jsonl"; then
  exit 0
fi

run_dir="$(cj_eval_make_run_dir "${category}" "${name}")"
cj_eval_metadata "${run_dir}/study_manifest.json" "${category}" "${name}" "${image}"

cmd=(
  "${CJ_EVAL_PYTHON}" -m evaluation.run
  --publication-study
  --docker-image "${image}"
  --system "${system}"
  --agent-level individual
  --topology single
  --benchmark "${benchmark}"
  --benchmark-subset "${benchmark_subset}"
  --fault-suite smoke
  --tasks "${tasks}"
  --repeats "${repeats}"
  --seed "${seed}"
  --task-selection-seed "${task_selection_seed}"
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

cj_eval_write_command "${run_dir}/run_command.sh" "${cmd[@]}"

echo "[cj-eval] category : ${category}"
echo "[cj-eval] results  : ${run_dir}"
echo "[cj-eval] use      : ${run_dir}/cj_overhead_summary.csv"
cj_eval_run_logged "${run_dir}/logs/run.log" "${cmd[@]}"
cj_eval_mark_checkpoint_done "${checkpoint_key}" "${checkpoint_title}"

echo "[cj-eval] CJ overhead study complete: ${run_dir}"
