#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

usage() {
  cat <<'EOF'
Usage:
  evaluation/scripts/run_distributed_native_multi_agent_smoke.sh [options]

Run distributed-native multi-agent experiments: Planner, Coder, and Reviewer
run in separate Docker containers, and each role uses a real native framework
adapter: AutoGen, LangGraph, or CrewAI.

Options:
  --env-file PATH       Load model config/secrets from PATH. Default: .env.
  --model NAME          Override model.
  --systems LIST        Space/comma list. Default: autogen-real langgraph-real crewai-real.
  --benchmarks LIST     Space/comma list. Default: humanevalplus mbppplus.
  --topologies LIST     Space/comma list. Default: linear closed_loop.
  --tasks N             Number of tasks. Default: 1.
  --task-selection-seed N Fixed task-manifest seed. Default: 17.
  --fault NAME          Distributed fault or suite. Default: none.
  --fault-suite NAME    smoke, communication, role_local, coordination, or all.
  --triplet             Run direct/control/fault records for the selected fault.
  --fault-target-role R Target role for role-local or misrouting faults. Default: coder.
  --fault-from-role R   Handoff source role for communication faults. Default: planner.
  --fault-to-role R     Handoff destination role for communication faults. Default: coder.
  --fault-severity S    low, medium, or high. Default: medium.
  --fault-duration S    Coordinated pressure duration in seconds. Default: 300.
  --maximum-skew-ms N   Coordinated activation-skew limit. Default: 100.
  --proxy-port N         Preferred proxy-port pool hint. Default: 20600.
  --dry-run             Use deterministic no-model native-role responses.
  --results-run NAME    Result folder under results/.
  --help                Show this help.
EOF
}

normalize_list() {
  echo "$1" | tr ',' ' '
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      export CJ_EVAL_ENV_FILE="$2"
      shift 2
      ;;
    --model)
      export CJ_EVAL_MODEL="$2"
      shift 2
      ;;
    --systems)
      export CJ_EVAL_DISTRIBUTED_NATIVE_SYSTEMS="$(normalize_list "$2")"
      shift 2
      ;;
    --benchmarks)
      export CJ_EVAL_DISTRIBUTED_NATIVE_BENCHMARKS="$(normalize_list "$2")"
      shift 2
      ;;
    --topologies)
      export CJ_EVAL_DISTRIBUTED_NATIVE_TOPOLOGIES="$(normalize_list "$2")"
      shift 2
      ;;
    --tasks)
      export CJ_EVAL_DISTRIBUTED_NATIVE_TASKS="$2"
      shift 2
      ;;
    --task-selection-seed)
      export CJ_EVAL_TASK_SELECTION_SEED="$2"
      shift 2
      ;;
    --fault)
      export CJ_EVAL_DISTRIBUTED_NATIVE_FAULT="$2"
      shift 2
      ;;
    --fault-suite)
      export CJ_EVAL_DISTRIBUTED_NATIVE_FAULT_SUITE="$2"
      shift 2
      ;;
    --triplet)
      export CJ_EVAL_DISTRIBUTED_NATIVE_TRIPLET=1
      shift
      ;;
    --fault-target-role)
      export CJ_EVAL_DISTRIBUTED_FAULT_TARGET_ROLE="$2"
      shift 2
      ;;
    --fault-from-role)
      export CJ_EVAL_DISTRIBUTED_FAULT_FROM_ROLE="$2"
      shift 2
      ;;
    --fault-to-role)
      export CJ_EVAL_DISTRIBUTED_FAULT_TO_ROLE="$2"
      shift 2
      ;;
    --fault-severity)
      export CJ_EVAL_DISTRIBUTED_FAULT_SEVERITY="$2"
      shift 2
      ;;
    --fault-duration)
      export CJ_EVAL_DISTRIBUTED_FAULT_DURATION_S="$2"
      shift 2
      ;;
    --maximum-skew-ms)
      export CJ_EVAL_DISTRIBUTED_MAXIMUM_SKEW_MS="$2"
      shift 2
      ;;
    --proxy-port)
      export CJ_EVAL_DISTRIBUTED_PROXY_PORT="$2"
      shift 2
      ;;
    --dry-run)
      export CJ_EVAL_DISTRIBUTED_DRY_RUN=1
      shift
      ;;
    --results-run)
      export CJ_EVAL_EXPERIMENT_RUN="$2"
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

image="$(cj_eval_default_image)"
cj_eval_require_image "${image}"

category="distributed-native-multi-agent-smoke"
systems="${CJ_EVAL_DISTRIBUTED_NATIVE_SYSTEMS:-autogen-real langgraph-real crewai-real}"
benchmarks="${CJ_EVAL_DISTRIBUTED_NATIVE_BENCHMARKS:-humanevalplus mbppplus}"
benchmark_subset="${CJ_EVAL_DISTRIBUTED_NATIVE_SUBSET:-${CJ_EVAL_BENCHMARK_SUBSET:-smoke}}"
topologies="${CJ_EVAL_DISTRIBUTED_NATIVE_TOPOLOGIES:-linear closed_loop}"
tasks="${CJ_EVAL_DISTRIBUTED_NATIVE_TASKS:-${CJ_EVAL_TASKS:-1}}"
seed="${CJ_EVAL_SEED:-401}"
task_selection_seed="${CJ_EVAL_TASK_SELECTION_SEED:-17}"
env_file="$(cj_eval_default_env_file)"
container_timeout="${CJ_EVAL_CONTAINER_TIMEOUT:-420}"
score_timeout="${CJ_EVAL_SCORE_TIMEOUT:-25}"
max_turns="${CJ_EVAL_MAX_TURNS:-8}"
cpus="${CJ_EVAL_CPUS:-1}"
memory="${CJ_EVAL_MEMORY:-2g}"
dry_run="${CJ_EVAL_DISTRIBUTED_DRY_RUN:-0}"
fault="${CJ_EVAL_DISTRIBUTED_NATIVE_FAULT:-none}"
fault_suite="${CJ_EVAL_DISTRIBUTED_NATIVE_FAULT_SUITE:-}"
triplet="${CJ_EVAL_DISTRIBUTED_NATIVE_TRIPLET:-0}"
fault_target_role="${CJ_EVAL_DISTRIBUTED_FAULT_TARGET_ROLE:-coder}"
fault_from_role="${CJ_EVAL_DISTRIBUTED_FAULT_FROM_ROLE:-planner}"
fault_to_role="${CJ_EVAL_DISTRIBUTED_FAULT_TO_ROLE:-coder}"
fault_severity="${CJ_EVAL_DISTRIBUTED_FAULT_SEVERITY:-medium}"
fault_duration_s="${CJ_EVAL_DISTRIBUTED_FAULT_DURATION_S:-300}"
maximum_skew_ms="${CJ_EVAL_DISTRIBUTED_MAXIMUM_SKEW_MS:-100}"
proxy_port="${CJ_EVAL_DISTRIBUTED_PROXY_PORT:-20600}"

cat <<EOF
[cj-eval] category  : ${category}
[cj-eval] systems   : ${systems}
[cj-eval] topologies: ${topologies}
[cj-eval] benchmarks: ${benchmarks}
[cj-eval] tasks     : ${tasks}
[cj-eval] fault     : ${fault_suite:-${fault}}
[cj-eval] triplet   : ${triplet}
[cj-eval] dry-run   : ${dry_run}
[cj-eval] image     : ${image}
EOF

run_index=0
for system in ${systems}; do
  for topology in ${topologies}; do
    for benchmark in ${benchmarks}; do
      run_index=$((run_index + 1))
      name="distributed-native-${system}-${topology}-${benchmark}-${fault_suite:-${fault}}-${CJ_EVAL_MODEL}"
      checkpoint_key="${category}:${name}:subset=${benchmark_subset}:tasks=${tasks}:seed=${seed}:task-seed=${task_selection_seed}:triplet=${triplet}:dry=${dry_run}:image=${image}"
      checkpoint_title="distributed-native ${system} ${topology} ${benchmark} ${fault_suite:-${fault}}"
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
        "${CJ_EVAL_PYTHON}" -m evaluation.distributed_campaign
        --docker-image "${image}"
        --results-dir "${run_dir}"
        --agent-system "${system}"
        --benchmark "${benchmark}"
        --benchmark-subset "${benchmark_subset}"
        --tasks "${tasks}"
        --seed "${seed}"
        --task-selection-seed "${task_selection_seed}"
        --topology "${topology}"
        --base-url "${CJ_EVAL_BASE_URL}"
        --container-base-url "${CJ_EVAL_CONTAINER_BASE_URL}"
        --model "${CJ_EVAL_MODEL}"
        --env-file "${env_file}"
        --container-timeout "${container_timeout}"
        --score-timeout "${score_timeout}"
        --max-turns "${max_turns}"
        --cpus "${cpus}"
        --memory "${memory}"
        --fault-target-role "${fault_target_role}"
        --fault-from-role "${fault_from_role}"
        --fault-to-role "${fault_to_role}"
        --fault-severity "${fault_severity}"
        --fault-duration-s "${fault_duration_s}"
        --maximum-skew-ms "${maximum_skew_ms}"
        --proxy-port "${proxy_port}"
      )

      if [[ -n "${fault_suite}" ]]; then
        cmd+=(--fault-suite "${fault_suite}")
      else
        cmd+=(--fault "${fault}")
      fi
      if [[ "${triplet}" == "1" ]]; then
        cmd+=(--triplet)
      fi
      if [[ "${dry_run}" == "1" ]]; then
        cmd+=(--dry-run)
      fi

      cj_eval_write_command "${run_dir}/run_command.sh" "${cmd[@]}"
      echo "[cj-eval] running distributed-native ${system} ${topology} ${benchmark} -> ${run_dir}"
      cj_eval_run_logged "${run_dir}/logs/run.log" "${cmd[@]}"
      cj_eval_mark_checkpoint_done "${checkpoint_key}" "${checkpoint_title}"
    done
  done
done

echo "[cj-eval] distributed-native multi-agent smoke complete under $(cj_eval_results_root)/${category}"
