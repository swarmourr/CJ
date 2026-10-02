#!/usr/bin/env bash
set -Eeuo pipefail

cj_eval_repo_root() {
  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cd "${script_dir}/../.." && pwd
}

CJ_EVAL_REPO_ROOT="${CJ_EVAL_REPO_ROOT:-$(cj_eval_repo_root)}"
CJ_EVAL_PYTHON="${CJ_EVAL_PYTHON:-/opt/homebrew/bin/python3.12}"
if [[ ! -x "${CJ_EVAL_PYTHON}" ]]; then
  CJ_EVAL_PYTHON="${PYTHON:-python3}"
fi

cj_eval_timestamp() {
  date -u +"%Y%m%dT%H%M%SZ"
}

cj_eval_commit() {
  git -C "${CJ_EVAL_REPO_ROOT}" rev-parse HEAD
}

cj_eval_default_image() {
  local commit
  commit="$(cj_eval_commit)"
  echo "${CJ_EVAL_DOCKER_IMAGE:-${CJ_EVAL_IMAGE_NAME:-cj-eval-agent}:${CJ_EVAL_IMAGE_TAG:-${commit}}}"
}

cj_eval_results_root() {
  echo "${CJ_EVAL_RESULTS_ROOT:-${CJ_EVAL_REPO_ROOT}/results/paper}"
}

cj_eval_slug() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9._=-' '-' | sed 's/^-//;s/-$//'
}

cj_eval_make_run_dir() {
  local category="$1"
  local name="$2"
  local stamp="${CJ_EVAL_RUN_STAMP:-$(cj_eval_timestamp)}"
  local root
  root="$(cj_eval_results_root)"
  local dir="${root}/$(cj_eval_slug "${category}")/$(cj_eval_slug "${name}")-${stamp}"
  mkdir -p "${dir}/logs"
  echo "${dir}"
}

cj_eval_print_command() {
  printf '%q ' "$@"
  printf '\n'
}

cj_eval_write_command() {
  local path="$1"
  shift
  {
    printf '#!/usr/bin/env bash\n'
    printf 'set -Eeuo pipefail\n'
    printf 'cd %q\n' "${CJ_EVAL_REPO_ROOT}"
    cj_eval_print_command "$@"
  } > "${path}"
  chmod +x "${path}"
}

cj_eval_run_logged() {
  local log_path="$1"
  shift
  echo "[cj-eval] command: $(cj_eval_print_command "$@")"
  "$@" 2>&1 | tee "${log_path}"
}

cj_eval_require_image() {
  local image="$1"
  if ! docker image inspect "${image}" >/dev/null 2>&1; then
    cat >&2 <<EOF
[cj-eval] Docker image not found: ${image}
[cj-eval] Build it first, for example:
  evaluation/docker/rebuild_image.sh --force-remove

Or set CJ_EVAL_DOCKER_IMAGE to an existing image tag.
EOF
    exit 2
  fi
}

cj_eval_model_defaults() {
  export CJ_EVAL_API_KEY="${CJ_EVAL_API_KEY:-dummy}"
  export CJ_EVAL_MODEL="${CJ_EVAL_MODEL:-qwen2.5:latest}"
  export CJ_EVAL_BASE_URL="${CJ_EVAL_BASE_URL:-http://127.0.0.1:11434/v1}"
  export CJ_EVAL_CONTAINER_BASE_URL="${CJ_EVAL_CONTAINER_BASE_URL:-http://host.docker.internal:11434/v1}"
}

cj_eval_metadata() {
  local path="$1"
  local category="$2"
  local name="$3"
  local image="$4"
  cat > "${path}" <<EOF
{
  "category": "${category}",
  "name": "${name}",
  "created_at_utc": "$(cj_eval_timestamp)",
  "repo_root": "${CJ_EVAL_REPO_ROOT}",
  "commit": "$(cj_eval_commit)",
  "docker_image": "${image}",
  "model": "${CJ_EVAL_MODEL:-}",
  "host_base_url": "${CJ_EVAL_BASE_URL:-}",
  "container_base_url": "${CJ_EVAL_CONTAINER_BASE_URL:-}"
}
EOF
}
