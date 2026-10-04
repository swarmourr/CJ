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

cj_eval_results_base() {
  echo "${CJ_EVAL_RESULTS_ROOT:-${CJ_EVAL_REPO_ROOT}/results}"
}

cj_eval_slug() {
  echo "$1" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9._=-' '-' | sed 's/^-//;s/-$//'
}

cj_eval_json_string() {
  "${CJ_EVAL_PYTHON}" -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$1"
}

cj_eval_results_root() {
  local base
  base="$(cj_eval_results_base)"
  if [[ -n "${CJ_EVAL_EXPERIMENT_RUN:-}" ]]; then
    echo "${base}/$(cj_eval_slug "${CJ_EVAL_EXPERIMENT_RUN}")"
  else
    echo "${base}"
  fi
}

cj_eval_init_experiment_run() {
  local root
  root="$(cj_eval_results_root)"
  mkdir -p "${root}"
  if [[ -n "${CJ_EVAL_EXPERIMENT_RUN:-}" ]]; then
    cat > "${root}/experiment_manifest.json" <<EOF
{
  "experiment_run": $(cj_eval_json_string "${CJ_EVAL_EXPERIMENT_RUN}"),
  "created_at_utc": $(cj_eval_json_string "${CJ_EVAL_EXPERIMENT_CREATED_AT:-$(cj_eval_timestamp)}"),
  "repo_root": $(cj_eval_json_string "${CJ_EVAL_REPO_ROOT}"),
  "commit": $(cj_eval_json_string "$(cj_eval_commit)"),
  "results_base": $(cj_eval_json_string "$(cj_eval_results_base)"),
  "results_root": $(cj_eval_json_string "${root}"),
  "layout": "<results_root>/<category>/<scenario-or-condition>/",
  "model": $(cj_eval_json_string "${CJ_EVAL_MODEL:-}"),
  "host_base_url": $(cj_eval_json_string "${CJ_EVAL_BASE_URL:-}"),
  "container_base_url": $(cj_eval_json_string "${CJ_EVAL_CONTAINER_BASE_URL:-}")
}
EOF
  fi
}

cj_eval_make_run_dir() {
  local category="$1"
  local name="$2"
  local stamp="${CJ_EVAL_RUN_STAMP:-$(cj_eval_timestamp)}"
  local root
  root="$(cj_eval_results_root)"
  cj_eval_init_experiment_run
  local category_slug
  local name_slug
  category_slug="$(cj_eval_slug "${category}")"
  name_slug="$(cj_eval_slug "${name}")"
  local dir
  if [[ -n "${CJ_EVAL_EXPERIMENT_RUN:-}" ]]; then
    dir="${root}/${category_slug}/${name_slug}"
    if [[ -e "${dir}" ]]; then
      local i=2
      while [[ -e "${dir}-${i}" ]]; do
        i=$((i + 1))
      done
      dir="${dir}-${i}"
    fi
  else
    dir="${root}/${category_slug}/${name_slug}-${stamp}"
  fi
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

cj_eval_load_dotenv() {
  local env_path="${CJ_EVAL_ENV_FILE:-${CJ_EVAL_REPO_ROOT}/.env}"
  if [[ ! -f "${env_path}" ]]; then
    return 0
  fi
  while IFS='=' read -r key value; do
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    [[ -z "${key}" || "${key}" == \#* ]] && continue
    [[ "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    value="${value%$'\r'}"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    if [[ -z "${!key+x}" ]]; then
      export "${key}=${value}"
    fi
  done < "${env_path}"
}

cj_eval_default_env_file() {
  if [[ -n "${CJ_EVAL_ENV_FILE:-}" ]]; then
    echo "${CJ_EVAL_ENV_FILE}"
  elif [[ -f "${CJ_EVAL_REPO_ROOT}/.env" ]]; then
    echo "${CJ_EVAL_REPO_ROOT}/.env"
  else
    echo "/dev/null"
  fi
}

cj_eval_require_image() {
  local image="$1"
  if docker image inspect "${image}" >/dev/null 2>&1; then
    return 0
  fi

  if [[ "${CJ_EVAL_AUTO_BUILD_IMAGE:-1}" != "1" ]]; then
    cat >&2 <<EOF
[cj-eval] Docker image not found: ${image}
[cj-eval] Auto-build is disabled by CJ_EVAL_AUTO_BUILD_IMAGE=0.
[cj-eval] Build it first, for example:
  evaluation/docker/rebuild_image.sh --force-remove

Or set CJ_EVAL_DOCKER_IMAGE to an existing image tag.
EOF
    exit 2
  fi

  echo "[cj-eval] Docker image not found: ${image}"
  echo "[cj-eval] Auto-building missing evaluation image for the current checkout..."
  evaluation/docker/rebuild_image.sh --keep-old

  if docker image inspect "${image}" >/dev/null 2>&1; then
    return 0
  fi

  cat >&2 <<EOF
[cj-eval] Auto-build finished but expected image is still missing: ${image}
[cj-eval] Set CJ_EVAL_DOCKER_IMAGE to the built tag, or run:
  evaluation/docker/rebuild_image.sh --force-remove
EOF
  exit 2
}

cj_eval_model_defaults() {
  cj_eval_load_dotenv
  export CJ_EVAL_API_KEY="${CJ_EVAL_API_KEY:-${LLM_API_KEY:-dummy}}"
  export CJ_EVAL_MODEL="${CJ_EVAL_MODEL:-${LLM_MODEL:-qwen2.5:latest}}"
  export CJ_EVAL_BASE_URL="${CJ_EVAL_BASE_URL:-${LLM_BASE_URL:-http://127.0.0.1:11434/v1}}"
  export CJ_EVAL_CONTAINER_BASE_URL="${CJ_EVAL_CONTAINER_BASE_URL:-${LLM_BASE_URL:-${CJ_EVAL_BASE_URL}}}"
}

cj_eval_metadata() {
  local path="$1"
  local category="$2"
  local name="$3"
  local image="$4"
  cat > "${path}" <<EOF
{
  "category": $(cj_eval_json_string "${category}"),
  "name": $(cj_eval_json_string "${name}"),
  "created_at_utc": $(cj_eval_json_string "$(cj_eval_timestamp)"),
  "repo_root": $(cj_eval_json_string "${CJ_EVAL_REPO_ROOT}"),
  "commit": $(cj_eval_json_string "$(cj_eval_commit)"),
  "docker_image": $(cj_eval_json_string "${image}"),
  "model": $(cj_eval_json_string "${CJ_EVAL_MODEL:-}"),
  "host_base_url": $(cj_eval_json_string "${CJ_EVAL_BASE_URL:-}"),
  "container_base_url": $(cj_eval_json_string "${CJ_EVAL_CONTAINER_BASE_URL:-}")
}
EOF
  if [[ -n "${CJ_EVAL_EXPERIMENT_RUN:-}" ]]; then
    local root
    local run_path
    root="$(cj_eval_results_root)"
    run_path="$(cd "$(dirname "${path}")" && pwd)"
    cat >> "${root}/runs_index.jsonl" <<EOF
{"category":$(cj_eval_json_string "${category}"),"name":$(cj_eval_json_string "${name}"),"path":$(cj_eval_json_string "${run_path}"),"created_at_utc":$(cj_eval_json_string "$(cj_eval_timestamp)"),"commit":$(cj_eval_json_string "$(cj_eval_commit)"),"docker_image":$(cj_eval_json_string "${image}")}
EOF
  fi
}
