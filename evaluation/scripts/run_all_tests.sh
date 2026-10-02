#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

cd "${CJ_EVAL_REPO_ROOT}"
export CJ_EVAL_RUN_STAMP="${CJ_EVAL_RUN_STAMP:-$(cj_eval_timestamp)}"

cat <<EOF
[cj-eval] test wrapper
[cj-eval] stamp       : ${CJ_EVAL_RUN_STAMP}
[cj-eval] root        : $(cj_eval_results_root)
[cj-eval] docker tests: ${CJ_EVAL_RUN_DOCKER_TESTS:-0}
EOF

"${script_dir}/run_static_validation.sh"

if [[ "${CJ_EVAL_RUN_DOCKER_TESTS:-0}" == "1" ]]; then
  "${script_dir}/run_docker_integration_tests.sh"
else
  echo "[cj-eval] Docker tests skipped. Run with CJ_EVAL_RUN_DOCKER_TESTS=1 to include them."
fi

echo "[cj-eval] test wrapper complete under $(cj_eval_results_root)"
