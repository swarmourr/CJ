#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF'
Usage:
  evaluation/scripts/write_unsupported_experiment.sh CATEGORY NAME REASON [missing...]

Write a machine-readable unsupported-experiment artifact under the current
results run. This is used by the paper bundle for scientific honesty: planned
architectures that do not yet have a real implementation are recorded as
unsupported instead of being skipped or reported as valid experiments.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ $# -lt 3 ]]; then
  usage >&2
  exit 2
fi

category="$1"
name="$2"
reason="$3"
shift 3

cd "${CJ_EVAL_REPO_ROOT}"
export CJ_EVAL_RUN_STAMP="${CJ_EVAL_RUN_STAMP:-$(cj_eval_timestamp)}"
export CJ_EVAL_EXPERIMENT_CREATED_AT="${CJ_EVAL_EXPERIMENT_CREATED_AT:-$(cj_eval_timestamp)}"
if [[ -z "${CJ_EVAL_EXPERIMENT_RUN:-}" ]]; then
  export CJ_EVAL_EXPERIMENT_RUN="unsupported-${CJ_EVAL_RUN_STAMP}"
fi
cj_eval_init_experiment_run

image="$(cj_eval_default_image)"
dir="$(cj_eval_make_run_dir "${category}" "${name}")"
cj_eval_metadata "${dir}/metadata.json" "${category}" "${name}" "${image}"

missing_json="$(
  "${CJ_EVAL_PYTHON}" - "$@" <<'PY'
import json
import sys
print(json.dumps(list(sys.argv[1:])))
PY
)"

cat > "${dir}/unsupported.json" <<EOF
{
  "status": "unsupported",
  "category": $(cj_eval_json_string "${category}"),
  "name": $(cj_eval_json_string "${name}"),
  "reason": $(cj_eval_json_string "${reason}"),
  "missing_requirements": ${missing_json},
  "created_at_utc": $(cj_eval_json_string "$(cj_eval_timestamp)"),
  "repo_root": $(cj_eval_json_string "${CJ_EVAL_REPO_ROOT}"),
  "commit": $(cj_eval_json_string "$(cj_eval_commit)"),
  "docker_image": $(cj_eval_json_string "${image}"),
  "paper_rule": "Do not claim this category as evaluated until a real implementation writes valid task records."
}
EOF

cat > "${dir}/README.md" <<EOF
# Unsupported Experiment: ${name}

Status: unsupported

Reason: ${reason}

Missing requirements:
EOF
for item in "$@"; do
  printf -- "- %s\n" "${item}" >> "${dir}/README.md"
done

echo "[cj-eval] unsupported experiment recorded: ${dir}"
