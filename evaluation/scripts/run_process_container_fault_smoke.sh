#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF'
Usage:
  evaluation/scripts/run_process_container_fault_smoke.sh [options]

Run a no-model process/container fault diagnostic against a disposable Docker
container. This is a lifecycle/safety validation, not a task-resilience result.

Options:
  --image IMAGE       Base image to run. Default: python:3.12-slim.
  --results-run NAME  Result folder under results/.
  --run-name NAME     Name under process-container-validation/.
  --help              Show this help.
EOF
}

base_image="${CJ_EVAL_PROCESS_BASE_IMAGE:-python:3.12-slim}"
run_name="${CJ_EVAL_RUN_NAME:-process-container-smoke}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image)
      base_image="${2:?--image requires a value}"
      shift 2
      ;;
    --results-run)
      export CJ_EVAL_EXPERIMENT_RUN="$2"
      shift 2
      ;;
    --run-name)
      run_name="$2"
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
export CJ_EVAL_EXPERIMENT_CREATED_AT="${CJ_EVAL_EXPERIMENT_CREATED_AT:-$(cj_eval_timestamp)}"
if [[ -z "${CJ_EVAL_EXPERIMENT_RUN:-}" ]]; then
  export CJ_EVAL_EXPERIMENT_RUN="process-container-${CJ_EVAL_RUN_STAMP}"
fi
cj_eval_init_experiment_run

dir="$(cj_eval_make_run_dir "process-container-validation" "${run_name}")"
cj_eval_metadata "${dir}/metadata.json" "process-container-validation" "${run_name}" "$(cj_eval_default_image)"
container_name="cj-process-smoke-$(cj_eval_slug "${CJ_EVAL_RUN_STAMP}")"

cat <<EOF
[cj-eval] process/container diagnostic
[cj-eval] base image : ${base_image}
[cj-eval] results    : ${dir}
EOF

if ! docker image inspect "${base_image}" >/dev/null 2>&1; then
  echo "[cj-eval] pulling missing base image ${base_image}" | tee "${dir}/logs/process_container.log"
  docker pull "${base_image}" 2>&1 | tee -a "${dir}/logs/process_container.log"
fi

cid=""
cleanup() {
  if [[ -n "${cid}" ]]; then
    docker rm -f "${cid}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

{
  echo "[cj-eval] starting disposable target container"
  cid="$(docker run -d --rm --name "${container_name}" \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --cpus "${CJ_EVAL_CPUS:-0.5}" \
    --memory "${CJ_EVAL_MEMORY:-256m}" \
    "${base_image}" \
    /bin/sh -lc "python -c 'import time; time.sleep(300)'")"
  echo "[cj-eval] container id: ${cid}"

  echo "[cj-eval] verifying process exists"
  before="$(docker exec "${cid}" /bin/sh -lc "pgrep -f 'time.sleep\\(300\\)' || true")"
  echo "[cj-eval] matching pids before kill: ${before}"
  test -n "${before}"

  echo "[cj-eval] injecting process kill inside container"
  docker exec "${cid}" /bin/sh -lc "pkill -KILL -f 'time.sleep\\(300\\)' || true"
  sleep 1
  after="$(docker exec "${cid}" /bin/sh -lc "pgrep -f 'time.sleep\\(300\\)' || true" 2>/dev/null || true)"
  echo "[cj-eval] matching pids after kill: ${after}"
  test -z "${after}"

  echo "[cj-eval] restarting process for container kill check"
  docker exec -d "${cid}" /bin/sh -lc "python -c 'import time; time.sleep(300)'"
  sleep 1
  running_before="$(docker inspect --format '{{.State.Running}}' "${cid}")"
  echo "[cj-eval] container running before kill: ${running_before}"
  test "${running_before}" = "true"

  echo "[cj-eval] injecting container kill"
  docker kill "${cid}" >/dev/null
  running_after="$(docker inspect --format '{{.State.Running}}' "${cid}" 2>/dev/null || echo missing)"
  echo "[cj-eval] container running after kill: ${running_after}"
  test "${running_after}" != "true"
} 2>&1 | tee "${dir}/logs/process_container.log"

cat > "${dir}/process_container_summary.json" <<EOF
{
  "status": "ok",
  "evaluation_scope": "process_container_fault_lifecycle_diagnostic",
  "base_image": $(cj_eval_json_string "${base_image}"),
  "container_id": $(cj_eval_json_string "${cid}"),
  "process_fault": {
    "configured": true,
    "activated": true,
    "triggered": true,
    "manifested": true,
    "evidence": "pgrep matched before kill and no longer matched after pkill inside container"
  },
  "container_fault": {
    "configured": true,
    "activated": true,
    "triggered": true,
    "manifested": true,
    "evidence": "docker inspect reported container stopped after docker kill"
  },
  "paper_limitation": "This smoke validates lifecycle confinement. Publication task-resilience records require deterministic trigger integration with DockerAgentRunner."
}
EOF

echo "[cj-eval] process/container diagnostic complete: ${dir}"
