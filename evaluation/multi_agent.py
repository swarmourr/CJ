"""Framework-neutral multi-agent workflow and trace event schema.

This module provides the shared role prompts, topology semantics, and event
schema used by the publication evaluation. Framework adapters can delegate to
these prompts or map their native callbacks into the same event contract.
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from evaluation.agents.base import AgentRunResult, ModelClient, extract_python_code


ROLES = ("planner", "coder", "reviewer")
LINEAR = "linear"
CLOSED_LOOP = "closed_loop"
MAX_REVISIONS = 2

ROLE_PROMPTS = {
    "planner": (
        "You are the Planner. Decompose the coding task into a concise plan. "
        "Do not write the final code."
    ),
    "coder": (
        "You are the Coder. Implement the final Python solution. Return only "
        "the solution code in a Python code block when possible."
    ),
    "reviewer": (
        "You are the Reviewer/Tester. Inspect the proposed solution for bugs. "
        "Reply with ACCEPT if it is ready, or REVISION_REQUEST followed by a "
        "short concrete fix request."
    ),
}


@dataclass
class TraceEvent:
    study_id: str
    run_id: str
    pair_id: str
    agent_role: str
    step_index: int
    timestamp: float
    trace_id: str
    event_type: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "study_id": self.study_id,
            "run_id": self.run_id,
            "pair_id": self.pair_id,
            "agent_role": self.agent_role,
            "step_index": self.step_index,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "event_type": self.event_type,
            "payload": self.payload,
        }


def validate_event_schema(event: dict[str, Any]) -> None:
    required = {
        "study_id",
        "run_id",
        "pair_id",
        "agent_role",
        "step_index",
        "timestamp",
        "trace_id",
        "span_id",
        "event_type",
    }
    missing = sorted(required - set(event))
    if missing:
        raise ValueError(f"multi-agent event is missing required fields: {missing}")
    if event["agent_role"] not in ROLES and event["agent_role"] != "coordinator":
        raise ValueError(f"unknown agent role: {event['agent_role']!r}")


@contextmanager
def role_metadata(run_id: str, role: str, step: int):
    old = {
        "CJ_RUN_ID": os.environ.get("CJ_RUN_ID"),
        "CJ_AGENT_ROLE": os.environ.get("CJ_AGENT_ROLE"),
        "CJ_STEP": os.environ.get("CJ_STEP"),
    }
    os.environ["CJ_RUN_ID"] = run_id
    os.environ["CJ_AGENT_ROLE"] = role
    os.environ["CJ_STEP"] = str(step)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class MultiAgentWorkflow:
    """Three-role Planner/Coder/Reviewer workflow with two topologies."""

    def __init__(
        self,
        client: ModelClient | None = None,
        *,
        topology: str = LINEAR,
        max_revisions: int = MAX_REVISIONS,
        dry_run: bool = False,
        study_id: str = "",
        pair_id: str = "",
        run_id: str = "",
    ) -> None:
        if topology not in (LINEAR, CLOSED_LOOP):
            raise ValueError(f"unsupported topology: {topology!r}")
        if max_revisions != MAX_REVISIONS:
            raise ValueError("publication evaluation requires exactly two possible revisions")
        self.client = client or ModelClient(dry_run=dry_run)
        self.topology = topology
        self.max_revisions = max_revisions
        self.study_id = study_id
        self.pair_id = pair_id
        self.run_id = run_id or uuid.uuid4().hex

    def run(self, task: str, seed: int = 0) -> AgentRunResult:
        t0 = time.time()
        events: list[dict[str, Any]] = []
        llm_calls = 0
        turns = 0
        reported_error = 0.0
        termination = "completed"
        exception = ""
        final_code = ""
        trace_id = uuid.uuid4().hex

        def emit(role: str, step: int, event_type: str, **payload) -> None:
            event = TraceEvent(
                study_id=self.study_id,
                run_id=self.run_id,
                pair_id=self.pair_id,
                agent_role=role,
                step_index=step,
                timestamp=time.time(),
                trace_id=trace_id,
                event_type=event_type,
                payload=payload,
            ).to_dict()
            validate_event_schema(event)
            events.append(event)

        def call_role(role: str, step: int, messages: list[dict[str, str]]) -> str:
            nonlocal llm_calls, turns
            emit(role, step, "agent_activation")
            emit(role, step, "llm_invocation", message_count=len(messages))
            with role_metadata(self.run_id, role, step):
                text = self.client.complete(messages, seed=seed)
            llm_calls += 1
            turns += 1
            emit(role, step, "final_answer" if role == "coder" else "sent_message",
                 content_preview=text[:500])
            return text

        try:
            step = 0
            plan = call_role(
                "planner",
                step,
                [
                    {"role": "system", "content": ROLE_PROMPTS["planner"]},
                    {"role": "user", "content": task},
                ],
            )
            emit("planner", step, "handoff", to_role="coder")
            step += 1

            code = call_role(
                "coder",
                step,
                [
                    {"role": "system", "content": ROLE_PROMPTS["coder"]},
                    {"role": "user", "content": f"Task:\n{task}\n\nPlan:\n{plan}"},
                ],
            )
            final_code = extract_python_code(code)
            emit("coder", step, "handoff", to_role="reviewer")
            step += 1

            review = call_role(
                "reviewer",
                step,
                [
                    {"role": "system", "content": ROLE_PROMPTS["reviewer"]},
                    {"role": "user", "content": f"Task:\n{task}\n\nSolution:\n{final_code}"},
                ],
            )
            needs_revision = "REVISION_REQUEST" in review.upper()
            emit("reviewer", step, "review_decision",
                 accepted=not needs_revision, content_preview=review[:500])

            revisions = 0
            while self.topology == CLOSED_LOOP and needs_revision and revisions < self.max_revisions:
                emit("reviewer", step, "revision_request", revision_round=revisions + 1)
                step += 1
                code = call_role(
                    "coder",
                    step,
                    [
                        {"role": "system", "content": ROLE_PROMPTS["coder"]},
                        {
                            "role": "user",
                            "content": (
                                f"Task:\n{task}\n\nPrior solution:\n{code}\n\n"
                                f"Reviewer request:\n{review}"
                            ),
                        },
                    ],
                )
                final_code = extract_python_code(code)
                emit("coder", step, "attempted_recovery", revision_round=revisions + 1)
                step += 1
                review = call_role(
                    "reviewer",
                    step,
                    [
                        {"role": "system", "content": ROLE_PROMPTS["reviewer"]},
                        {"role": "user", "content": f"Task:\n{task}\n\nSolution:\n{final_code}"},
                    ],
                )
                needs_revision = "REVISION_REQUEST" in review.upper()
                emit("reviewer", step, "review_decision",
                     accepted=not needs_revision, content_preview=review[:500])
                revisions += 1
        except Exception as exc:  # noqa: BLE001
            reported_error = 1.0
            termination = "exception"
            exception = repr(exc)
            emit("coordinator", len(events), "error_detection", exception=exception)

        return AgentRunResult(
            success=0.0,
            duration_s=time.time() - t0,
            reported_error=reported_error,
            llm_calls=llm_calls,
            tool_calls=0,
            turns=turns,
            generated_code=final_code,
            execution_trace=events,
            termination_reason=termination,
            exception=exception,
        )
