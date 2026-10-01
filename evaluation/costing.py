"""Profiles, cost estimation, and pilot sizing helpers for CJ studies."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class StudyProfile:
    name: str
    tasks_per_benchmark: int
    repetitions: int
    frameworks: tuple[str, ...]
    topologies: tuple[str, ...]
    individual_faults: tuple[str, ...]
    multi_agent_faults: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


SMOKE_PROFILE = StudyProfile(
    name="smoke",
    tasks_per_benchmark=1,
    repetitions=1,
    frameworks=("autogen-real",),
    topologies=("single", "linear"),
    individual_faults=("llm_unavailable",),
    multi_agent_faults=("planner_llm_unavailable",),
)

PILOT_PROFILE = StudyProfile(
    name="pilot",
    tasks_per_benchmark=5,
    repetitions=2,
    frameworks=("autogen-real", "langgraph-real", "crewai-real"),
    topologies=("single", "linear", "closed_loop"),
    individual_faults=("llm_latency", "llm_unavailable", "network_delay", "cpu_stress"),
    multi_agent_faults=("planner_llm_unavailable", "reviewer_corrupt", "coder_tool_fault"),
)

MAIN_STUDY_PROFILE = StudyProfile(
    name="main",
    tasks_per_benchmark=0,  # supplied by manifest; do not hardcode final N here
    repetitions=0,
    frameworks=("autogen-real", "langgraph-real", "crewai-real"),
    topologies=("single", "linear", "closed_loop"),
    individual_faults=(
        "llm_latency",
        "llm_rate_limit",
        "llm_unavailable",
        "llm_response_corrupt",
        "tool_fault",
        "network_delay",
        "network_loss",
        "cpu_stress",
        "memory_stress",
        "io_stress",
        "process_kill",
        "container_kill",
    ),
    multi_agent_faults=(
        "planner_llm_unavailable",
        "reviewer_response_corrupt",
        "coder_tool_fault",
        "container_network_delay",
        "coordinator_container_kill",
        "compound_planner_delay_reviewer_corrupt",
    ),
)


PROFILES = {
    "smoke": SMOKE_PROFILE,
    "pilot": PILOT_PROFILE,
    "main": MAIN_STUDY_PROFILE,
}


def estimate_condition_count(
    *,
    tasks_per_benchmark: int,
    repetitions: int,
    frameworks: int,
    individual_faults: int,
    multi_agent_faults: int,
    multi_agent_topologies: int,
    benchmarks: int = 2,
) -> int:
    """Return number of Docker executions including direct/control/fault triplets."""
    individual_groups = benchmarks * tasks_per_benchmark * repetitions * frameworks * individual_faults
    multi_groups = (
        benchmarks
        * tasks_per_benchmark
        * repetitions
        * frameworks
        * multi_agent_topologies
        * multi_agent_faults
    )
    return 3 * (individual_groups + multi_groups)


def estimate_cost_usd(
    *,
    executions: int,
    avg_prompt_tokens: int,
    avg_completion_tokens: int,
    llm_calls_per_execution: float,
    input_price_per_1k: float,
    output_price_per_1k: float,
) -> float:
    per_call = (
        avg_prompt_tokens * input_price_per_1k / 1000.0
        + avg_completion_tokens * output_price_per_1k / 1000.0
    )
    return executions * llm_calls_per_execution * per_call


def pilot_power_recommendation(
    *,
    pilot_task_count: int,
    observed_sd: float,
    minimum_detectable_effect: float,
    alpha: float = 0.05,
    power: float = 0.8,
) -> dict:
    """Approximate paired-study task count using a normal approximation."""
    if observed_sd <= 0 or minimum_detectable_effect <= 0:
        return {
            "recommended_tasks": None,
            "reason": "observed_sd and minimum_detectable_effect must be positive",
        }
    z_alpha = 1.96 if alpha == 0.05 else 1.96
    z_power = 0.84 if abs(power - 0.8) < 1e-9 else 0.84
    n = math.ceil(((z_alpha + z_power) * observed_sd / minimum_detectable_effect) ** 2)
    return {
        "pilot_task_count": pilot_task_count,
        "recommended_tasks": max(n, pilot_task_count),
        "observed_sd": observed_sd,
        "minimum_detectable_effect": minimum_detectable_effect,
        "alpha": alpha,
        "power": power,
    }
