#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${script_dir}/common.sh"

cd "${CJ_EVAL_REPO_ROOT}"
cj_eval_model_defaults
export CJ_EVAL_RUN_STAMP="${CJ_EVAL_RUN_STAMP:-$(cj_eval_timestamp)}"
export CJ_EVAL_EXPERIMENT_CREATED_AT="${CJ_EVAL_EXPERIMENT_CREATED_AT:-$(cj_eval_timestamp)}"
if [[ -z "${CJ_EVAL_EXPERIMENT_RUN:-}" ]]; then
  export CJ_EVAL_EXPERIMENT_RUN="all-scenarios-${CJ_EVAL_RUN_STAMP}"
fi
cj_eval_init_experiment_run

image="$(cj_eval_default_image)"
if [[ "${CJ_EVAL_REBUILD_IMAGE:-0}" == "1" ]]; then
  evaluation/docker/rebuild_image.sh --force-remove
else
  cj_eval_require_image "${image}"
fi
export CJ_EVAL_DOCKER_IMAGE="${image}"

tasks="${CJ_EVAL_TASKS:-3}"
repeats="${CJ_EVAL_REPEATS:-1}"
benchmarks="${CJ_EVAL_BENCHMARKS:-humanevalplus mbppplus}"
topologies="${CJ_EVAL_TOPOLOGIES:-linear closed_loop}"
systems="${CJ_EVAL_SYSTEMS:-autogen-real langgraph-real crewai-real}"
single_system="${CJ_EVAL_SYSTEM:-autogen-real}"
injector_suite="${CJ_EVAL_FAULT_SUITE:-all}"
individual_suites="${CJ_EVAL_FAULT_SUITES:-all semantic_generated}"
if [[ "${individual_suites}" == "all" ]]; then
  individual_suites="all semantic_generated"
fi
multi_suites="${CJ_EVAL_MULTI_FAULT_SUITES:-multi_agent multi_agent_semantic}"
distributed_native_fault="${CJ_EVAL_DISTRIBUTED_NATIVE_FAULT:-none}"
infra_faults="${CJ_EVAL_INFRA_FAULTS:-cpu_pressure memory_pressure network_latency packet_loss disk_full}"

cat <<EOF
[cj-eval] all current scenarios
[cj-eval] results root       : $(cj_eval_results_root)
[cj-eval] run                : ${CJ_EVAL_EXPERIMENT_RUN}
[cj-eval] image              : ${image}
[cj-eval] model              : ${CJ_EVAL_MODEL}
[cj-eval] tasks/repeats      : ${tasks}/${repeats}
[cj-eval] benchmarks         : ${benchmarks}
[cj-eval] topologies         : ${topologies}
[cj-eval] systems            : ${systems}
[cj-eval] injector suite     : ${injector_suite}
[cj-eval] individual suites  : ${individual_suites}
[cj-eval] multi suites       : ${multi_suites}
[cj-eval] distributed native : ${distributed_native_fault}
[cj-eval] infra faults       : ${infra_faults}
EOF

if [[ "${CJ_EVAL_RUN_STATIC:-1}" == "1" ]]; then
  "${script_dir}/run_static_validation.sh"
fi

if [[ "${CJ_EVAL_RUN_DOCKER_TESTS:-1}" == "1" ]]; then
  "${script_dir}/run_docker_integration_tests.sh"
fi

CJ_EVAL_SYSTEM="${single_system}" \
CJ_EVAL_BENCHMARK="${CJ_EVAL_BENCHMARK:-humanevalplus}" \
CJ_EVAL_FAULT_SUITE="${injector_suite}" \
CJ_EVAL_TASKS="${tasks}" \
CJ_EVAL_REPEATS="${repeats}" \
  "${script_dir}/run_injector_validation.sh"

CJ_EVAL_SYSTEM="${single_system}" \
CJ_EVAL_BENCHMARK="${CJ_EVAL_BENCHMARK:-humanevalplus}" \
CJ_EVAL_TASKS="${tasks}" \
CJ_EVAL_REPEATS="${repeats}" \
  "${script_dir}/run_cj_overhead_study.sh"

CJ_EVAL_SYSTEMS="${systems}" \
CJ_EVAL_BENCHMARKS="${benchmarks}" \
CJ_EVAL_FAULT_SUITES="${individual_suites}" \
CJ_EVAL_TASKS="${tasks}" \
CJ_EVAL_REPEATS="${repeats}" \
  "${script_dir}/run_individual_resilience_pilot.sh"

for suite in ${multi_suites}; do
  CJ_EVAL_SYSTEMS="${CJ_EVAL_MULTI_SYSTEMS:-${systems}}" \
  CJ_EVAL_BENCHMARKS="${benchmarks}" \
  CJ_EVAL_TOPOLOGIES="${topologies}" \
  CJ_EVAL_FAULT_SUITE="${suite}" \
  CJ_EVAL_TASKS="${tasks}" \
  CJ_EVAL_REPEATS="${repeats}" \
    "${script_dir}/run_native_multi_agent_pilot.sh"
done

CJ_EVAL_DISTRIBUTED_NATIVE_SYSTEMS="${CJ_EVAL_DISTRIBUTED_NATIVE_SYSTEMS:-${systems}}" \
CJ_EVAL_DISTRIBUTED_NATIVE_BENCHMARKS="${benchmarks}" \
CJ_EVAL_DISTRIBUTED_NATIVE_TOPOLOGIES="${topologies}" \
CJ_EVAL_DISTRIBUTED_NATIVE_FAULT="${distributed_native_fault}" \
CJ_EVAL_DISTRIBUTED_NATIVE_TASKS="${CJ_EVAL_DISTRIBUTED_NATIVE_TASKS:-${tasks}}" \
  "${script_dir}/run_distributed_native_multi_agent_smoke.sh"

for fault in ${infra_faults}; do
  CJ_EVAL_INFRA_FAULT="${fault}" \
  CJ_EVAL_BENCHMARK="${CJ_EVAL_INFRA_BENCHMARK:-toy}" \
  CJ_EVAL_TASKS="${CJ_EVAL_INFRA_TASKS:-1}" \
  CJ_EVAL_DRY_RUN="${CJ_EVAL_INFRA_DRY_RUN:-1}" \
  CJ_EVAL_RUN_NAME="infra-${fault}" \
    "${script_dir}/run_infrastructure_fault_smoke.sh"
done

if [[ "${CJ_EVAL_RUN_REAL_SMOKE:-1}" == "1" ]]; then
  CJ_EVAL_FAULT_SUITE="${CJ_EVAL_REAL_FAULT_SUITE:-${injector_suite}}" \
  CJ_EVAL_TASKS="${CJ_EVAL_REAL_TASKS:-1}" \
  CJ_EVAL_REPEATS="${CJ_EVAL_REAL_REPEATS:-1}" \
    "${script_dir}/run_real_llm_smoke.sh"
fi

echo "[cj-eval] all current scenarios complete under $(cj_eval_results_root)"
