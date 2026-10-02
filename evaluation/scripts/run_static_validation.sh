#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

cd "${CJ_EVAL_REPO_ROOT}"

category="stage-a-static-unit"
name="${CJ_EVAL_RUN_NAME:-static-validation}"
run_dir="$(cj_eval_make_run_dir "${category}" "${name}")"
image="$(cj_eval_default_image)"
cj_eval_metadata "${run_dir}/study_manifest.json" "${category}" "${name}" "${image}"

compile_cmd=(
  python3 -m compileall -q
  chaos_jungle
  evaluation
  tests
)
focused_cmd=(
  "${CJ_EVAL_PYTHON}" -m pytest -q
  evaluation/tests/test_output.py
  evaluation/tests/test_metrics.py
  evaluation/tests/test_publication_eval.py
)
safe_cmd=(
  "${CJ_EVAL_PYTHON}" -m pytest -q
  -m "not docker and not real_eval"
  -k "not TestEvalPlusScoring"
)

cj_eval_write_command "${run_dir}/compile_command.sh" "${compile_cmd[@]}"
cj_eval_write_command "${run_dir}/focused_tests_command.sh" "${focused_cmd[@]}"
cj_eval_write_command "${run_dir}/safe_tests_command.sh" "${safe_cmd[@]}"

echo "[cj-eval] results: ${run_dir}"
cj_eval_run_logged "${run_dir}/logs/compile.log" "${compile_cmd[@]}"
cj_eval_run_logged "${run_dir}/logs/focused_tests.log" "${focused_cmd[@]}"
cj_eval_run_logged "${run_dir}/logs/safe_tests.log" "${safe_cmd[@]}"

echo "[cj-eval] static/unit validation complete: ${run_dir}"
