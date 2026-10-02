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

category="multi-agent-reference-pilot"
systems="${CJ_EVAL_SYSTEMS:-autogen-real}"
topologies="${CJ_EVAL_TOPOLOGIES:-linear closed_loop}"
benchmarks="${CJ_EVAL_BENCHMARKS:-humanevalplus mbppplus}"
fault_suite="${CJ_EVAL_FAULT_SUITE:-multi_agent}"
tasks="${CJ_EVAL_TASKS:-3}"
repeats="${CJ_EVAL_REPEATS:-1}"
seed="${CJ_EVAL_SEED:-201}"
env_file="$(cj_eval_default_env_file)"
container_timeout="${CJ_EVAL_CONTAINER_TIMEOUT:-420}"
score_timeout="${CJ_EVAL_SCORE_TIMEOUT:-25}"
max_turns="${CJ_EVAL_MAX_TURNS:-8}"
cpus="${CJ_EVAL_CPUS:-1}"
memory="${CJ_EVAL_MEMORY:-2g}"
proxy_port_base="${CJ_EVAL_PROXY_PORT:-18200}"

cat <<EOF
[cj-eval] category : ${category}
[cj-eval] note     : current multi-agent path is framework-neutral and is
[cj-eval]            reported as reference-multi-agent in output records.
[cj-eval] topologies: ${topologies}
[cj-eval] faults   : ${fault_suite}
EOF

run_index=0
for system in ${systems}; do
  for topology in ${topologies}; do
    for benchmark in ${benchmarks}; do
      run_index=$((run_index + 1))
      port=$((proxy_port_base + run_index))
      name="reference-${topology}-${benchmark}-${fault_suite}-${CJ_EVAL_MODEL}"
      run_dir="$(cj_eval_make_run_dir "${category}" "${name}")"
      cj_eval_metadata "${run_dir}/study_manifest.json" "${category}" "${name}" "${image}"

      cmd=(
        "${CJ_EVAL_PYTHON}" -m evaluation.run
        --publication-study
        --docker-image "${image}"
        --system "${system}"
        --agent-level multi_agent
        --topology "${topology}"
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
        --proxy-port "${port}"
      )

      cj_eval_write_command "${run_dir}/run_command.sh" "${cmd[@]}"
      echo "[cj-eval] running ${topology} ${benchmark} -> ${run_dir}"
      cj_eval_run_logged "${run_dir}/logs/run.log" "${cmd[@]}"
    done
  done
done

echo "[cj-eval] multi-agent reference pilot complete under $(cj_eval_results_root)/${category}"
