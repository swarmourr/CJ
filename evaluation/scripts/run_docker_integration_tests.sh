#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

cd "${CJ_EVAL_REPO_ROOT}"

category="stage-c-docker-integration"
name="${CJ_EVAL_RUN_NAME:-docker-integration-tests}"
run_dir="$(cj_eval_make_run_dir "${category}" "${name}")"
image="$(cj_eval_default_image)"
cj_eval_metadata "${run_dir}/study_manifest.json" "${category}" "${name}" "${image}"

cmd=(
  "${CJ_EVAL_PYTHON}" -m pytest -q
  -m docker
  evaluation/tests/test_docker_integration.py
)

cj_eval_write_command "${run_dir}/docker_tests_command.sh" "${cmd[@]}"

echo "[cj-eval] results: ${run_dir}"
cj_eval_run_logged "${run_dir}/logs/docker_tests.log" "${cmd[@]}"

echo "[cj-eval] Docker integration validation complete: ${run_dir}"
