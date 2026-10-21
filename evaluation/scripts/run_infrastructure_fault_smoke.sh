#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

cd "${CJ_EVAL_REPO_ROOT}"
cj_eval_model_defaults

image="$(cj_eval_default_image)"
cj_eval_require_image "${image}"

fault="${CJ_EVAL_INFRA_FAULT:-cpu_pressure}"
benchmark="${CJ_EVAL_BENCHMARK:-toy}"
tasks="${CJ_EVAL_TASKS:-1}"
seed="${CJ_EVAL_SEED:-31}"
run_name="${CJ_EVAL_RUN_NAME:-${CJ_EVAL_MODEL}-${fault}-${benchmark}}"
checkpoint_key="infrastructure-validation:${run_name}:fault=${fault}:benchmark=${benchmark}:tasks=${tasks}:seed=${seed}:dry=${CJ_EVAL_DRY_RUN:-1}:image=${image}"
checkpoint_title="infrastructure ${fault} ${benchmark}"
if cj_eval_checkpoint_done "${checkpoint_key}"; then
  cj_eval_skip_checkpoint_message "${checkpoint_key}" "${checkpoint_title}"
  exit 0
fi
if cj_eval_backfill_checkpoint_from_run_dir "${checkpoint_key}" "${checkpoint_title}" "infrastructure-validation" "${run_name}" "runs.jsonl"; then
  exit 0
fi

results_dir="$(cj_eval_make_run_dir "infrastructure-validation" "${run_name}")"

cmd=(
  "${CJ_EVAL_PYTHON}" -m evaluation.infrastructure_campaign
  --docker-image "${image}"
  --results-dir "${results_dir}"
  --fault "${fault}"
  --benchmark "${benchmark}"
  --tasks "${tasks}"
  --seed "${seed}"
  --system "${CJ_EVAL_SYSTEM:-autogen-real}"
  --topology "${CJ_EVAL_TOPOLOGY:-single}"
  --model "${CJ_EVAL_MODEL}"
  --base-url "${CJ_EVAL_BASE_URL}"
  --container-base-url "${CJ_EVAL_CONTAINER_BASE_URL}"
  --env-file "$(cj_eval_default_env_file)"
  --container-timeout "${CJ_EVAL_CONTAINER_TIMEOUT:-180}"
  --score-timeout "${CJ_EVAL_SCORE_TIMEOUT:-10}"
  --max-turns "${CJ_EVAL_MAX_TURNS:-2}"
  --cpus "${CJ_EVAL_CPUS:-1}"
  --memory "${CJ_EVAL_MEMORY:-2g}"
)

if [[ "${CJ_EVAL_DRY_RUN:-1}" == "1" ]]; then
  cmd+=(--dry-run)
else
  cmd+=(--no-dry-run)
fi

cj_eval_metadata "${results_dir}/metadata.json" "infrastructure-validation" "${run_name}" "${image}"
cj_eval_write_command "${results_dir}/run_command.sh" "${cmd[@]}"

cat <<EOF
[cj-eval] category : infrastructure-validation
[cj-eval] results  : ${results_dir}
[cj-eval] fault    : ${fault}
[cj-eval] benchmark: ${benchmark}
[cj-eval] image    : ${image}
EOF

cj_eval_run_logged "${results_dir}/logs/infrastructure.log" "${cmd[@]}"
cj_eval_mark_checkpoint_done "${checkpoint_key}" "${checkpoint_title}"

echo "[cj-eval] infrastructure smoke complete: ${results_dir}"
