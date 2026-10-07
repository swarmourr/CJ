#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF'
Usage:
  evaluation/scripts/run_comparator_gate.sh [options]

Record whether the external Toxiproxy comparator is installed and ready for a
matched CJ-vs-comparator fault-fidelity experiment. This does not fabricate
comparator results when Toxiproxy is absent.

Options:
  --results-run NAME    Result folder under results/.
  --help                Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
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
export CJ_EVAL_RUN_STAMP="${CJ_EVAL_RUN_STAMP:-$(cj_eval_timestamp)}"

image="$(cj_eval_default_image)"
category="comparator"
name="${CJ_EVAL_RUN_NAME:-cj-vs-toxiproxy-gate}"
run_dir="$(cj_eval_make_run_dir "${category}" "${name}")"
cj_eval_metadata "${run_dir}/study_manifest.json" "${category}" "${name}" "${image}"

cmd=(
  "${CJ_EVAL_PYTHON}" -m evaluation.comparator_campaign
  --results-dir "${run_dir}"
  --comparator toxiproxy
)

cj_eval_write_command "${run_dir}/run_command.sh" "${cmd[@]}"

cat <<EOF
[cj-eval] category : ${category}
[cj-eval] results  : ${run_dir}
EOF
cj_eval_run_logged "${run_dir}/logs/run.log" "${cmd[@]}"

echo "[cj-eval] comparator gate complete: ${run_dir}"
