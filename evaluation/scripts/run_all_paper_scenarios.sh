#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

cd "${CJ_EVAL_REPO_ROOT}"
export CJ_EVAL_RUN_STAMP="${CJ_EVAL_RUN_STAMP:-$(cj_eval_timestamp)}"

profile="${CJ_EVAL_PROFILE:-smoke}"
export CJ_EVAL_EXPERIMENT_CREATED_AT="${CJ_EVAL_EXPERIMENT_CREATED_AT:-$(cj_eval_timestamp)}"
if [[ -z "${CJ_EVAL_EXPERIMENT_RUN:-}" ]]; then
  export CJ_EVAL_EXPERIMENT_RUN="paper-${profile}-${CJ_EVAL_RUN_STAMP}"
fi
cj_eval_init_experiment_run

cat <<EOF
[cj-eval] paper scenario wrapper
[cj-eval] profile : ${profile}
[cj-eval] stamp   : ${CJ_EVAL_RUN_STAMP}
[cj-eval] root    : $(cj_eval_results_root)
[cj-eval] run     : ${CJ_EVAL_EXPERIMENT_RUN}
EOF

if [[ "${CJ_EVAL_REBUILD_IMAGE:-0}" == "1" ]]; then
  evaluation/docker/rebuild_image.sh --force-remove
  export CJ_EVAL_REBUILD_IMAGE=0
fi

if [[ "${CJ_EVAL_RUN_STATIC:-1}" == "1" ]]; then
  "${script_dir}/run_static_validation.sh"
fi

if [[ "${CJ_EVAL_RUN_DOCKER_TESTS:-0}" == "1" ]]; then
  "${script_dir}/run_docker_integration_tests.sh"
fi

case "${profile}" in
  smoke)
    CJ_EVAL_TASKS="${CJ_EVAL_TASKS:-1}" \
    CJ_EVAL_REPEATS="${CJ_EVAL_REPEATS:-1}" \
    CJ_EVAL_SYSTEM="${CJ_EVAL_SYSTEM:-autogen-real}" \
    CJ_EVAL_BENCHMARK="${CJ_EVAL_BENCHMARK:-humanevalplus}" \
    CJ_EVAL_FAULT_SUITE="${CJ_EVAL_FAULT_SUITE:-all}" \
      "${script_dir}/run_injector_validation.sh"

    CJ_EVAL_TASKS="${CJ_EVAL_TASKS:-1}" \
    CJ_EVAL_REPEATS="${CJ_EVAL_REPEATS:-1}" \
    CJ_EVAL_SYSTEM="${CJ_EVAL_SYSTEM:-autogen-real}" \
    CJ_EVAL_BENCHMARK="${CJ_EVAL_BENCHMARK:-humanevalplus}" \
      "${script_dir}/run_cj_overhead_study.sh"

    CJ_EVAL_TASKS="${CJ_EVAL_TASKS:-1}" \
    CJ_EVAL_REPEATS="${CJ_EVAL_REPEATS:-1}" \
    CJ_EVAL_SYSTEMS="${CJ_EVAL_SYSTEMS:-autogen-real}" \
    CJ_EVAL_BENCHMARKS="${CJ_EVAL_BENCHMARKS:-humanevalplus}" \
    CJ_EVAL_FAULT_SUITES="${CJ_EVAL_FAULT_SUITES:-smoke}" \
      "${script_dir}/run_individual_resilience_pilot.sh"

    CJ_EVAL_TASKS="${CJ_EVAL_TASKS:-1}" \
    CJ_EVAL_REPEATS="${CJ_EVAL_REPEATS:-1}" \
    CJ_EVAL_SYSTEMS="${CJ_EVAL_SYSTEMS:-autogen-real}" \
    CJ_EVAL_BENCHMARKS="${CJ_EVAL_BENCHMARKS:-humanevalplus}" \
    CJ_EVAL_TOPOLOGIES="${CJ_EVAL_TOPOLOGIES:-linear}" \
      "${script_dir}/run_multi_agent_reference_pilot.sh"
    ;;
  pilot)
    "${script_dir}/run_injector_validation.sh"
    "${script_dir}/run_cj_overhead_study.sh"
    "${script_dir}/run_individual_resilience_pilot.sh"
    "${script_dir}/run_multi_agent_reference_pilot.sh"
    ;;
  *)
    echo "Unknown CJ_EVAL_PROFILE=${profile}; expected smoke or pilot" >&2
    exit 2
    ;;
esac

echo "[cj-eval] paper scenario wrapper complete under $(cj_eval_results_root)"
