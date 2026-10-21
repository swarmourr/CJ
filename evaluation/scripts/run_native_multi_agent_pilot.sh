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

category="native-multi-agent-pilot"
systems="${CJ_EVAL_SYSTEMS:-autogen-real langgraph-real crewai-real}"
topologies="${CJ_EVAL_TOPOLOGIES:-linear closed_loop}"
benchmarks="${CJ_EVAL_BENCHMARKS:-humanevalplus mbppplus}"
benchmark_subset="${CJ_EVAL_BENCHMARK_SUBSET:-auto}"
fault_suite="${CJ_EVAL_FAULT_SUITE:-multi_agent}"
tasks="${CJ_EVAL_TASKS:-3}"
repeats="${CJ_EVAL_REPEATS:-1}"
seed="${CJ_EVAL_SEED:-211}"
env_file="$(cj_eval_default_env_file)"
container_timeout="${CJ_EVAL_CONTAINER_TIMEOUT:-420}"
score_timeout="${CJ_EVAL_SCORE_TIMEOUT:-25}"
max_turns="${CJ_EVAL_MAX_TURNS:-8}"
cpus="${CJ_EVAL_CPUS:-1}"
memory="${CJ_EVAL_MEMORY:-2g}"
proxy_port_base="${CJ_EVAL_PROXY_PORT:-18400}"

cat <<EOF
[cj-eval] category : ${category}
[cj-eval] note     : native AutoGen/LangGraph/CrewAI multi-agent workflows
[cj-eval]            are executed as framework_native=true records.
[cj-eval] systems  : ${systems}
[cj-eval] topologies: ${topologies}
[cj-eval] faults   : ${fault_suite}
EOF

run_index=0
for system in ${systems}; do
  for topology in ${topologies}; do
    for benchmark in ${benchmarks}; do
      run_index=$((run_index + 1))
      port=$((proxy_port_base + run_index * 10))
      name="native-${system}-${topology}-${benchmark}-${fault_suite}-${CJ_EVAL_MODEL}"
      checkpoint_key="${category}:${name}:subset=${benchmark_subset}:tasks=${tasks}:repeats=${repeats}:seed=${seed}:image=${image}"
      checkpoint_title="native ${system} ${topology} ${benchmark} ${fault_suite}"
      if cj_eval_checkpoint_done "${checkpoint_key}"; then
        cj_eval_skip_checkpoint_message "${checkpoint_key}" "${checkpoint_title}"
        continue
      fi
      if cj_eval_backfill_checkpoint_from_run_dir "${checkpoint_key}" "${checkpoint_title}" "${category}" "${name}" "runs.jsonl"; then
        continue
      fi

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
        --benchmark-subset "${benchmark_subset}"
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
      echo "[cj-eval] running native ${system} ${topology} ${benchmark} on proxy ports ${port}/$((port + 1)) -> ${run_dir}"
      cj_eval_run_logged "${run_dir}/logs/run.log" "${cmd[@]}"
      cj_eval_mark_checkpoint_done "${checkpoint_key}" "${checkpoint_title}"
    done
  done
done

echo "[cj-eval] native multi-agent pilot complete under $(cj_eval_results_root)/${category}"
