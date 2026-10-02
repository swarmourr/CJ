#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

cd "${CJ_EVAL_REPO_ROOT}"
cj_eval_model_defaults
export CJ_EVAL_RUN_STAMP="${CJ_EVAL_RUN_STAMP:-$(cj_eval_timestamp)}"

if [[ "${CJ_EVAL_REBUILD_IMAGE:-0}" == "1" ]]; then
  evaluation/docker/rebuild_image.sh --force-remove
fi

image="$(cj_eval_default_image)"
cj_eval_require_image "${image}"

category="individual-resilience-pilot"
systems="${CJ_EVAL_SYSTEMS:-autogen-real langgraph-real crewai-real}"
benchmarks="${CJ_EVAL_BENCHMARKS:-humanevalplus mbppplus}"
fault_suites="${CJ_EVAL_FAULT_SUITES:-llm_api response tool}"
tasks="${CJ_EVAL_TASKS:-5}"
repeats="${CJ_EVAL_REPEATS:-1}"
seed="${CJ_EVAL_SEED:-101}"
env_file="$(cj_eval_default_env_file)"
container_timeout="${CJ_EVAL_CONTAINER_TIMEOUT:-300}"
score_timeout="${CJ_EVAL_SCORE_TIMEOUT:-25}"
max_turns="${CJ_EVAL_MAX_TURNS:-5}"
cpus="${CJ_EVAL_CPUS:-1}"
memory="${CJ_EVAL_MEMORY:-2g}"
proxy_port_base="${CJ_EVAL_PROXY_PORT:-18100}"

echo "[cj-eval] category : ${category}"
echo "[cj-eval] systems  : ${systems}"
echo "[cj-eval] benches  : ${benchmarks}"
echo "[cj-eval] suites   : ${fault_suites}"
echo "[cj-eval] tasks    : ${tasks}"

run_index=0
for system in ${systems}; do
  for benchmark in ${benchmarks}; do
    for suite in ${fault_suites}; do
      run_index=$((run_index + 1))
      port=$((proxy_port_base + run_index))
      name="${system}-${benchmark}-${suite}-${CJ_EVAL_MODEL}"
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
        --fault-suite "${suite}"
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
        --proxy-port "${port}"
      )

      cj_eval_write_command "${run_dir}/run_command.sh" "${cmd[@]}"
      echo "[cj-eval] running ${system} ${benchmark} ${suite} -> ${run_dir}"
      cj_eval_run_logged "${run_dir}/logs/run.log" "${cmd[@]}"
    done
  done
done

echo "[cj-eval] individual resilience pilot complete under $(cj_eval_results_root)/${category}"
