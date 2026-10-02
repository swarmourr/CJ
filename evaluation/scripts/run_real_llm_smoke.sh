#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

cd "${CJ_EVAL_REPO_ROOT}"
cj_eval_model_defaults

image="${CJ_EVAL_DOCKER_IMAGE:-cj-eval-agent:real-llm-smoke}"
if docker image inspect "${image}" >/dev/null 2>&1 && [[ "${CJ_EVAL_REBUILD_IMAGE:-0}" != "1" ]]; then
  echo "[cj-eval] using existing Docker image: ${image}"
else
  image_name="${image%%:*}"
  image_tag="${image#*:}"
  if [[ "${image_name}" == "${image_tag}" ]]; then
    image_tag="real-llm-smoke"
  fi
  build_commit="$(cj_eval_commit)"
  if ! git diff --quiet || ! git diff --cached --quiet; then
    build_commit="${build_commit}-dirty"
  fi
  echo "[cj-eval] building missing/stale real-LLM smoke image: ${image_name}:${image_tag}"
  evaluation/docker/rebuild_image.sh \
    --keep-old \
    --image-name "${image_name}" \
    --tag "${image_tag}" \
    --commit "${build_commit}"
  if ! docker image inspect "${image}" >/dev/null 2>&1; then
    echo "[cj-eval] Docker image build finished but ${image} is still unavailable" >&2
    exit 2
  fi
fi

if [[ "${CJ_EVAL_BASE_URL}" =~ ^https?:// ]] \
   && [[ "${CJ_EVAL_BASE_URL}" != *"127.0.0.1"* ]] \
   && [[ "${CJ_EVAL_BASE_URL}" != *"localhost"* ]] \
   && [[ "${CJ_EVAL_API_KEY}" == "dummy" ]]; then
  cat >&2 <<'EOF'
[cj-eval] Refusing to run a remote real-LLM smoke with CJ_EVAL_API_KEY=dummy.
[cj-eval] Put LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL in the ignored .env file,
[cj-eval] or export CJ_EVAL_API_KEY/CJ_EVAL_BASE_URL/CJ_EVAL_MODEL explicitly.
EOF
  exit 2
fi

category="real-llm-smoke"
system="${CJ_EVAL_SYSTEM:-autogen-real}"
benchmark="${CJ_EVAL_BENCHMARK:-humanevalplus}"
fault_suite="${CJ_EVAL_FAULT_SUITE:-smoke}"
tasks="${CJ_EVAL_TASKS:-1}"
repeats="${CJ_EVAL_REPEATS:-1}"
seed="${CJ_EVAL_SEED:-17}"
env_file="$(cj_eval_default_env_file)"
container_timeout="${CJ_EVAL_CONTAINER_TIMEOUT:-240}"
score_timeout="${CJ_EVAL_SCORE_TIMEOUT:-20}"
max_turns="${CJ_EVAL_MAX_TURNS:-3}"
cpus="${CJ_EVAL_CPUS:-1}"
memory="${CJ_EVAL_MEMORY:-2g}"
proxy_port="${CJ_EVAL_PROXY_PORT:-18350}"
name="${CJ_EVAL_RUN_NAME:-${CJ_EVAL_MODEL}-${system}-${fault_suite}}"
run_dir="$(cj_eval_make_run_dir "${category}" "${name}")"
cj_eval_metadata "${run_dir}/study_manifest.json" "${category}" "${name}" "${image}"

cmd=(
  "${CJ_EVAL_PYTHON}" -m evaluation.run
  --publication-study
  --docker-image "${image}"
  --system "${system}"
  --agent-level individual
  --topology single
  --benchmark "${benchmark}"
  --fault-suite "${fault_suite}"
  --tasks "${tasks}"
  --repeats "${repeats}"
  --seed "${seed}"
  --base-url "${CJ_EVAL_BASE_URL}"
  --container-base-url "${CJ_EVAL_CONTAINER_BASE_URL}"
  --model "${CJ_EVAL_MODEL}"
  --env-file "${env_file}"
  --results-dir "${run_dir}"
  --container-timeout "${container_timeout}"
  --score-timeout "${score_timeout}"
  --max-turns "${max_turns}"
  --cpus "${cpus}"
  --memory "${memory}"
  --proxy-port "${proxy_port}"
)

if [[ -n "${CJ_EVAL_STUDY_ID:-}" ]]; then
  cmd+=(--study-id "${CJ_EVAL_STUDY_ID}")
fi

cj_eval_write_command "${run_dir}/run_command.sh" "${cmd[@]}"

cat <<EOF
[cj-eval] category : ${category}
[cj-eval] results  : ${run_dir}
[cj-eval] model    : ${CJ_EVAL_MODEL}
[cj-eval] faults   : ${fault_suite}
[cj-eval] image    : ${image}
EOF
cj_eval_run_logged "${run_dir}/logs/run.log" "${cmd[@]}"

echo "[cj-eval] real-LLM smoke complete: ${run_dir}"
