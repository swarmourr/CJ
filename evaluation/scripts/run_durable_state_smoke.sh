#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF'
Usage:
  evaluation/scripts/run_durable_state_smoke.sh [options]

Run the Docker-scoped durable-state validation. It creates a checkpoint file
inside one disposable evaluation container, corrupts it with CJ JsonStateCorrupt,
verifies field/checksum manifestation, reverts it, and verifies recovery.

Options:
  --env-file PATH       Load environment file for Docker run metadata.
  --results-run NAME    Result folder under results/.
  --help                Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      export CJ_EVAL_ENV_FILE="$2"
      shift 2
      ;;
    --results-run)
      export CJ_EVAL_EXPERIMENT_RUN="$2"
      shift 2
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

image="$(cj_eval_default_image)"
cj_eval_require_image "${image}"

category="durable-state-validation"
name="${CJ_EVAL_RUN_NAME:-json-state-corrupt}"
checkpoint_key="${category}:${name}:image=${image}:timeout=${CJ_EVAL_DURABLE_TIMEOUT:-60}"
checkpoint_title="durable state ${name}"
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
  "${CJ_EVAL_PYTHON}" -m evaluation.durable_state_campaign
  --docker-image "${image}"
  --results-dir "${run_dir}"
  --env-file "$(cj_eval_default_env_file)"
  --cpus "${CJ_EVAL_CPUS:-1}"
  --memory "${CJ_EVAL_MEMORY:-1g}"
  --timeout "${CJ_EVAL_DURABLE_TIMEOUT:-60}"
)

cj_eval_write_command "${run_dir}/run_command.sh" "${cmd[@]}"

cat <<EOF
[cj-eval] category : ${category}
[cj-eval] results  : ${run_dir}
[cj-eval] image    : ${image}
EOF
cj_eval_run_logged "${run_dir}/logs/run.log" "${cmd[@]}"
cj_eval_mark_checkpoint_done "${checkpoint_key}" "${checkpoint_title}"

echo "[cj-eval] durable-state validation complete: ${run_dir}"
