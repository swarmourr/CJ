#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

cd "${CJ_EVAL_REPO_ROOT}"
cj_eval_model_defaults
export CJ_EVAL_RUN_STAMP="${CJ_EVAL_RUN_STAMP:-$(cj_eval_timestamp)}"

image="$(cj_eval_default_image)"
cj_eval_require_image "${image}"

category="tool-triggered-controlled-workload"
benchmarks="${CJ_EVAL_TOOL_TRIGGERED_BENCHMARKS:-humanevalplus mbppplus}"
benchmark_subset="${CJ_EVAL_TOOL_TRIGGERED_BENCHMARK_SUBSET:-auto}"
fault_suites="${CJ_EVAL_TOOL_TRIGGERED_FAULT_SUITES:-tool}"
tasks="${CJ_EVAL_TOOL_TRIGGERED_TASKS:-${CJ_EVAL_TASKS:-3}}"
repeats="${CJ_EVAL_TOOL_TRIGGERED_REPEATS:-${CJ_EVAL_REPEATS:-1}}"
seed="${CJ_EVAL_TOOL_TRIGGERED_SEED:-${CJ_EVAL_SEED:-131}}"
env_file="$(cj_eval_default_env_file)"
container_timeout="${CJ_EVAL_TOOL_TRIGGERED_CONTAINER_TIMEOUT:-${CJ_EVAL_CONTAINER_TIMEOUT:-300}}"
score_timeout="${CJ_EVAL_TOOL_TRIGGERED_SCORE_TIMEOUT:-${CJ_EVAL_SCORE_TIMEOUT:-25}}"
max_turns="${CJ_EVAL_TOOL_TRIGGERED_MAX_TURNS:-${CJ_EVAL_MAX_TURNS:-3}}"
cpus="${CJ_EVAL_CPUS:-1}"
memory="${CJ_EVAL_MEMORY:-2g}"
proxy_port_base="${CJ_EVAL_TOOL_TRIGGERED_PROXY_PORT:-18700}"

echo "[cj-eval] category : ${category}"
echo "[cj-eval] benches  : ${benchmarks}"
echo "[cj-eval] suites   : ${fault_suites}"
echo "[cj-eval] tasks    : ${tasks}"
echo "[cj-eval] model    : ${CJ_EVAL_MODEL}"

run_index=0
for benchmark in ${benchmarks}; do
  for suite in ${fault_suites}; do
    run_index=$((run_index + 1))
    port=$((proxy_port_base + run_index))
    name="tool-triggered-controlled-${benchmark}-${suite}-${CJ_EVAL_MODEL}"
    checkpoint_key="${category}:${name}:subset=${benchmark_subset}:tasks=${tasks}:repeats=${repeats}:seed=${seed}:image=${image}"
    checkpoint_title="tool-triggered ${benchmark} ${suite}"
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
      --system tool-triggered-controlled
      --agent-level individual
      --topology single
      --benchmark "${benchmark}"
      --benchmark-subset "${benchmark_subset}"
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
    echo "[cj-eval] running tool-triggered ${benchmark} ${suite} on proxy port ${port}"
    cj_eval_run_logged "${run_dir}/logs/run.log" "${cmd[@]}"
    cj_eval_mark_checkpoint_done "${checkpoint_key}" "${checkpoint_title}"
  done
done

echo "[cj-eval] tool-triggered workload complete under $(cj_eval_results_root)/${category}"
