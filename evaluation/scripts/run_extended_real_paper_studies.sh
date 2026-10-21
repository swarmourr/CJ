#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"
cd "${CJ_EVAL_REPO_ROOT}"

usage() {
  cat <<'EOF'
Run the real-agent E7-E9 studies with checkpoints and Docker isolation.

Usage:
  evaluation/scripts/run_extended_real_paper_studies.sh [options]

Profiles:
  smoke   One real AutoGen/HumanEval+ task and one seed. This is the default.
  pilot   Three frameworks, two benchmarks, two topologies, 6 tasks, one seed.
  paper   Three frameworks, two benchmarks, two topologies, 30 tasks, 3 seeds.
          Requires --confirm-paper.

Options:
  --profile NAME          smoke, pilot, or paper
  --env-file PATH         Model credentials/configuration (default: .env)
  --model NAME            Model override (default: LLM_MODEL from env file)
  --results-run NAME      Folder name under results/
  --tasks N               Override profile task count
  --seeds LIST            Comma/space-separated seeds
  --task-selection-seed N Fixed task-manifest seed shared by all execution seeds
  --systems LIST          Frameworks to execute
  --benchmarks LIST       humanevalplus and/or mbppplus
  --topologies LIST       linear and/or closed_loop for E7
  --rebuild               Rebuild the evaluation image from this checkout
  --no-resume             Ignore existing checkpoints and rerun
  --confirm-paper         Confirm the large paper profile
  -h, --help              Show this help

Stages:
  E7-real  Native Planner/Coder/Reviewer containers with synchronized CPU pressure.
  E8-real  Direct/control/LLM/CPU/compound scored agent conditions.
  E9-real  Real-agent direct-vs-CJ-control overhead and restoration verification.
EOF
}

normalize_list() {
  echo "$1" | tr ',' ' '
}

profile="smoke"
env_file=".env"
model=""
results_run="real-e7-e9-$(cj_eval_timestamp)"
tasks=""
seeds=""
task_selection_seed="${CJ_EVAL_TASK_SELECTION_SEED:-17}"
systems=""
benchmarks=""
topologies=""
rebuild=0
confirm_paper=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile) profile="$2"; shift 2 ;;
    --env-file) env_file="$2"; shift 2 ;;
    --model) model="$2"; shift 2 ;;
    --results-run) results_run="$2"; shift 2 ;;
    --tasks) tasks="$2"; shift 2 ;;
    --seeds) seeds="$(normalize_list "$2")"; shift 2 ;;
    --task-selection-seed) task_selection_seed="$2"; shift 2 ;;
    --systems) systems="$(normalize_list "$2")"; shift 2 ;;
    --benchmarks) benchmarks="$(normalize_list "$2")"; shift 2 ;;
    --topologies) topologies="$(normalize_list "$2")"; shift 2 ;;
    --rebuild) rebuild=1; shift ;;
    --no-resume) export CJ_EVAL_RESUME=0; shift ;;
    --confirm-paper) confirm_paper=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "[cj-eval] unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "${profile}" in
  smoke)
    tasks="${tasks:-1}"
    seeds="${seeds:-17}"
    systems="${systems:-autogen-real}"
    benchmarks="${benchmarks:-humanevalplus}"
    topologies="${topologies:-linear}"
    ;;
  pilot)
    tasks="${tasks:-6}"
    seeds="${seeds:-17}"
    systems="${systems:-autogen-real langgraph-real crewai-real}"
    benchmarks="${benchmarks:-humanevalplus mbppplus}"
    topologies="${topologies:-linear closed_loop}"
    ;;
  paper)
    if [[ "${confirm_paper}" != "1" ]]; then
      echo "[cj-eval] paper profile is large; rerun with --confirm-paper" >&2
      exit 2
    fi
    tasks="${tasks:-30}"
    seeds="${seeds:-17 29 43}"
    systems="${systems:-autogen-real langgraph-real crewai-real}"
    benchmarks="${benchmarks:-humanevalplus mbppplus}"
    topologies="${topologies:-linear closed_loop}"
    ;;
  *) echo "[cj-eval] unsupported profile: ${profile}" >&2; exit 2 ;;
esac

export CJ_EVAL_ENV_FILE="${env_file}"
cj_eval_model_defaults
if [[ -n "${model}" ]]; then
  export CJ_EVAL_MODEL="${model}"
fi
export CJ_EVAL_EXPERIMENT_RUN="${results_run}"
export CJ_EVAL_TASK_SELECTION_SEED="${task_selection_seed}"
export CJ_EVAL_EXPERIMENT_CREATED_AT="${CJ_EVAL_EXPERIMENT_CREATED_AT:-$(cj_eval_timestamp)}"
export CJ_EVAL_RESUME="${CJ_EVAL_RESUME:-1}"
cj_eval_init_experiment_run

if [[ "${profile}" == "paper" ]] && [[ -n "$(git status --porcelain)" ]]; then
  echo "[cj-eval] paper profile requires a clean Git worktree for reproducible provenance" >&2
  exit 2
fi

image="$(cj_eval_default_image)"
if [[ "${rebuild}" == "1" ]]; then
  echo "[cj-eval] rebuilding evaluation image from the current checkout"
  evaluation/docker/rebuild_image.sh --keep-old
fi
cj_eval_require_image "${image}"
export CJ_EVAL_DOCKER_IMAGE="${image}"

root="$(cj_eval_results_root)"
mkdir -p "${root}/logs"
cat <<EOF
[cj-eval] ================================================================
[cj-eval] REAL E7-E9 EXPERIMENTS
[cj-eval] profile    : ${profile}
[cj-eval] results    : ${root}
[cj-eval] image      : ${image}
[cj-eval] model      : ${CJ_EVAL_MODEL}
[cj-eval] systems    : ${systems}
[cj-eval] benchmarks : ${benchmarks}
[cj-eval] topologies : ${topologies}
[cj-eval] tasks      : ${tasks}
[cj-eval] seeds      : ${seeds}
[cj-eval] task seed  : ${task_selection_seed}
[cj-eval] resume     : ${CJ_EVAL_RESUME}
[cj-eval] ================================================================
EOF

stage() {
  local title="$1"
  shift
  echo
  echo "[cj-eval] >>> ${title}"
  "$@"
  echo "[cj-eval] <<< ${title}: complete"
}

seed_csv="$(echo "${seeds}" | tr ' ' ',' | tr -s ',')"

# E7: actual native framework roles, one container per role, with a CJ
# InjectionGroup applying CPU pressure across all three containers.
for seed in ${seeds}; do
  stage "E7-real distributed coordination (seed ${seed})" \
    env \
      CJ_EVAL_SEED="${seed}" \
      CJ_EVAL_BENCHMARK_SUBSET="development" \
      CJ_EVAL_DISTRIBUTED_NATIVE_SUBSET="development" \
      CJ_EVAL_DISTRIBUTED_FAULT_DURATION_S="${CJ_EVAL_CONTAINER_TIMEOUT:-480}" \
      CJ_EVAL_DISTRIBUTED_MAXIMUM_SKEW_MS="${CJ_EVAL_E7_MAXIMUM_SKEW_MS:-100}" \
      evaluation/scripts/run_distributed_native_multi_agent_smoke.sh \
        --env-file "${env_file}" \
        --model "${CJ_EVAL_MODEL}" \
        --systems "${systems}" \
        --benchmarks "${benchmarks}" \
        --topologies "${topologies}" \
        --tasks "${tasks}" \
        --task-selection-seed "${task_selection_seed}" \
        --fault-suite coordination \
        --fault-duration "${CJ_EVAL_CONTAINER_TIMEOUT:-480}" \
        --maximum-skew-ms "${CJ_EVAL_E7_MAXIMUM_SKEW_MS:-100}" \
        --triplet \
        --results-run "${results_run}"
done

# E8: a five-condition matched factorial study. Each framework/benchmark gets
# a separate immutable JSONL and its own checkpoints/artifacts.
for system in ${systems}; do
  for benchmark in ${benchmarks}; do
    name="${system}-${benchmark}-${CJ_EVAL_MODEL}"
    checkpoint="e8-real:${name}:tasks=${tasks}:seeds=${seed_csv}:image=${image}"
    if cj_eval_checkpoint_done "${checkpoint}"; then
      cj_eval_skip_checkpoint_message "${checkpoint}" "E8-real ${name}"
      continue
    fi
    output_dir="$(cj_eval_make_run_dir "e8-real-cross-layer" "${name}")"
    mkdir -p "${output_dir}/logs"
    cmd=(
      "${CJ_EVAL_PYTHON}" -m evaluation.extended_real_campaign
      --docker-image "${image}"
      --results-dir "${output_dir}"
      --system "${system}"
      --benchmark "${benchmark}"
      --benchmark-subset development
      --tasks "${tasks}"
      --seeds "${seed_csv}"
      --task-selection-seed "${task_selection_seed}"
      --model "${CJ_EVAL_MODEL}"
      --base-url "${CJ_EVAL_BASE_URL}"
      --container-base-url "${CJ_EVAL_CONTAINER_BASE_URL}"
      --env-file "${env_file}"
      --delay-s "${CJ_EVAL_E8_DELAY_S:-0.5}"
      --stress-duration "${CJ_EVAL_CONTAINER_TIMEOUT:-480}"
      --maximum-skew-ms "${CJ_EVAL_E8_MAXIMUM_SKEW_MS:-250}"
      --container-timeout "${CJ_EVAL_CONTAINER_TIMEOUT:-420}"
      --score-timeout "${CJ_EVAL_SCORE_TIMEOUT:-25}"
      --max-turns "${CJ_EVAL_MAX_TURNS:-5}"
      --cpus "${CJ_EVAL_CPUS:-1}"
      --memory "${CJ_EVAL_MEMORY:-2g}"
    )
    cj_eval_write_command "${output_dir}/run_command.sh" "${cmd[@]}"
    stage "E8-real cross-layer ${system}/${benchmark}" \
      cj_eval_run_logged "${output_dir}/logs/run.log" "${cmd[@]}"
    cj_eval_mark_checkpoint_done "${checkpoint}" "E8-real ${name}"
  done
done

# E9: run the existing real-agent direct/control path for every selected
# framework, benchmark, and seed. It measures proxy overhead from actual model
# calls and retains the matched latency-fault condition as a lifecycle check.
for seed in ${seeds}; do
  for system in ${systems}; do
    for benchmark in ${benchmarks}; do
      stage "E9-real overhead ${system}/${benchmark} (seed ${seed})" \
        env \
          CJ_EVAL_SEED="${seed}" \
          CJ_EVAL_SYSTEM="${system}" \
          CJ_EVAL_BENCHMARK="${benchmark}" \
          CJ_EVAL_BENCHMARK_SUBSET="development" \
          CJ_EVAL_TASKS="${tasks}" \
          CJ_EVAL_RUN_NAME="e9-real-${system}-${benchmark}-seed-${seed}-${CJ_EVAL_MODEL}" \
          CJ_EVAL_PROXY_PORT="${CJ_EVAL_E9_PROXY_PORT:-18300}" \
          evaluation/scripts/run_cj_overhead_study.sh
    done
  done
done

# High-repetition restoration timing uses the deterministic local upstream so
# provider variability and cost do not contaminate cleanup measurements. The
# real-agent E9 runs above independently establish real endpoint overhead.
restoration_checkpoint="e9-real:restoration:image=${image}"
if ! cj_eval_checkpoint_done "${restoration_checkpoint}"; then
  restoration_dir="$(cj_eval_make_run_dir "e9-real-restoration" "restoration-stress")"
  mkdir -p "${restoration_dir}/logs"
  restoration_cmd=(
    "${CJ_EVAL_PYTHON}" -m evaluation.extended_campaign e9
    --results-dir "${restoration_dir}"
    --requests "${CJ_EVAL_E9_REQUESTS:-200}"
    --rounds "${CJ_EVAL_E9_ROUNDS:-5}"
    --warmup-requests "${CJ_EVAL_E9_WARMUP_REQUESTS:-10}"
    --concurrency "${CJ_EVAL_E9_CONCURRENCY:-8}"
    --cycles "${CJ_EVAL_E9_CYCLES:-30}"
    --delay-s "${CJ_EVAL_E9_DELAY_S:-0.05}"
  )
  cj_eval_write_command "${restoration_dir}/run_command.sh" "${restoration_cmd[@]}"
  stage "E9 restoration stress" \
    cj_eval_run_logged "${restoration_dir}/logs/run.log" "${restoration_cmd[@]}"
  cj_eval_mark_checkpoint_done "${restoration_checkpoint}" "E9 restoration stress"
else
  cj_eval_skip_checkpoint_message "${restoration_checkpoint}" "E9 restoration stress"
fi

echo
echo "[cj-eval] real E7-E9 experiments complete"
echo "[cj-eval] results: ${root}"
