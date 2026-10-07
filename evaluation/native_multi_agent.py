"""Native framework multi-agent workflows for publication evaluation.

The reference workflow in :mod:`evaluation.multi_agent` is intentionally
framework-neutral.  This module keeps a separate implementation for native
AutoGen, LangGraph, and CrewAI multi-agent runs so paper outputs can distinguish
real framework evidence from reference/distributed evidence.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import os
import time
import uuid
from contextlib import contextmanager
from typing import Any, TypedDict

from evaluation.agents.base import AgentRunResult, extract_python_code
from evaluation.agents.crewai_real import _crewai_llm_kwargs
from evaluation.model_config import ModelConfig
from evaluation.multi_agent import (
    CLOSED_LOOP,
    LINEAR,
    MAX_REVISIONS,
    ROLE_PROMPTS,
    ROLES,
    TraceEvent,
    role_metadata,
    validate_event_schema,
)


NATIVE_MULTI_AGENT_SYSTEMS = {"autogen-real", "langgraph-real", "crewai-real"}


def native_multi_agent_impl_name(system_name: str) -> str:
    if system_name not in NATIVE_MULTI_AGENT_SYSTEMS:
        raise ValueError(f"unsupported native multi-agent system: {system_name!r}")
    return f"native-{system_name}-multi-agent"


def run_native_multi_agent(
    *,
    system_name: str,
    model_config: ModelConfig,
    topology: str,
    task: str,
    seed: int,
    max_turns: int,
    dry_run: bool,
    study_id: str,
    pair_id: str,
    run_id: str,
) -> AgentRunResult:
    """Run a native Planner/Coder/Reviewer workflow for one framework."""
    if topology not in {LINEAR, CLOSED_LOOP}:
        raise ValueError(f"unsupported native multi-agent topology: {topology!r}")
    if system_name == "autogen-real":
        return _run_autogen_native(
            model_config=model_config,
            topology=topology,
            task=task,
            seed=seed,
            max_turns=max_turns,
            dry_run=dry_run,
            study_id=study_id,
            pair_id=pair_id,
            run_id=run_id,
        )
    if system_name == "langgraph-real":
        return _run_langgraph_native(
            model_config=model_config,
            topology=topology,
            task=task,
            seed=seed,
            max_turns=max_turns,
            dry_run=dry_run,
            study_id=study_id,
            pair_id=pair_id,
            run_id=run_id,
        )
    if system_name == "crewai-real":
        return _run_crewai_native(
            model_config=model_config,
            topology=topology,
            task=task,
            seed=seed,
            max_turns=max_turns,
            dry_run=dry_run,
            study_id=study_id,
            pair_id=pair_id,
            run_id=run_id,
        )
    raise ValueError(f"unsupported native multi-agent system: {system_name!r}")


class _NativeTrace:
    def __init__(self, *, study_id: str, pair_id: str, run_id: str) -> None:
        self.study_id = study_id
        self.pair_id = pair_id
        self.run_id = run_id
        self.trace_id = uuid.uuid4().hex
        self.events: list[dict[str, Any]] = []

    def emit(self, role: str, step: int, event_type: str, **payload: Any) -> None:
        event = TraceEvent(
            study_id=self.study_id,
            run_id=self.run_id,
            pair_id=self.pair_id,
            agent_role=role,
            step_index=step,
            timestamp=time.time(),
            trace_id=self.trace_id,
            event_type=event_type,
            payload=payload,
        ).to_dict()
        validate_event_schema(event)
        self.events.append(event)


def _dry_role_response(role: str, task: str) -> str:
    if role == "planner":
        return "Plan: implement the requested function directly and keep the code minimal."
    if role == "reviewer":
        return "ACCEPT"
    return (
        "```python\n"
        "def solution(*args, **kwargs):\n"
        "    return None\n"
        "```"
    )


def _trace_headers(run_id: str, role: str, step: int) -> dict[str, str]:
    return {
        "X-CJ-Run-ID": run_id,
        "X-CJ-Agent-Role": role,
        "X-CJ-Step": str(step),
    }


@contextmanager
def _role_env(run_id: str, role: str, step: int):
    with role_metadata(run_id, role, step):
        yield


def _usage_dict(prompt_tokens: int = 0, completion_tokens: int = 0) -> dict[str, int]:
    return {
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": int(prompt_tokens or 0) + int(completion_tokens or 0),
    }


def _finish_result(
    *,
    model_config: ModelConfig,
    t0: float,
    trace: _NativeTrace,
    generated_code: str,
    usage: dict[str, int],
    llm_calls: int,
    turns: int,
    reported_error: float = 0.0,
    termination_reason: str = "completed",
    exception: str = "",
    revisions: int = 0,
) -> AgentRunResult:
    cost = model_config.compute_cost(usage["prompt_tokens"], usage["completion_tokens"])
    trace.emit(
        "coordinator",
        turns,
        "final_answer",
        generated_code_chars=len(generated_code),
        revision_rounds=revisions,
        termination_reason=termination_reason,
    )
    return AgentRunResult(
        success=0.0,
        duration_s=round(time.time() - t0, 3),
        reported_error=reported_error,
        retries=revisions,
        llm_calls=llm_calls,
        tool_calls=0,
        turns=turns,
        prompt_tokens=usage["prompt_tokens"] if usage["total_tokens"] else None,
        completion_tokens=usage["completion_tokens"] if usage["total_tokens"] else None,
        total_tokens=usage["total_tokens"] if usage["total_tokens"] else None,
        cost_usd=cost,
        token_usage_status="measured" if usage["total_tokens"] else "unavailable",
        cost_status="measured" if cost is not None else "unavailable",
        generated_code=generated_code,
        execution_trace=trace.events,
        termination_reason=termination_reason,
        exception=exception,
    )


def _review_requests_revision(text: str) -> bool:
    return "REVISION_REQUEST" in str(text or "").upper()


def _message_content(msg: Any) -> str:
    content = getattr(msg, "content", msg)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content or "")


def _run_autogen_native(
    *,
    model_config: ModelConfig,
    topology: str,
    task: str,
    seed: int,
    max_turns: int,
    dry_run: bool,
    study_id: str,
    pair_id: str,
    run_id: str,
) -> AgentRunResult:
    async def _async() -> AgentRunResult:
        t0 = time.time()
        trace = _NativeTrace(study_id=study_id, pair_id=pair_id, run_id=run_id)
        usage = _usage_dict()
        llm_calls = 0
        turns = 0
        revisions = 0
        generated_code = ""
        exception = ""
        termination = "completed"
        reported_error = 0.0
        try:
            plan, u = await _autogen_call_role("planner", 0, task, model_config, seed, max_turns, dry_run, trace, run_id)
            usage = _add_usage(usage, u)
            llm_calls += 0 if dry_run else 1
            turns += 1
            trace.emit("planner", 0, "handoff", to_role="coder")

            code_text, u = await _autogen_call_role(
                "coder",
                1,
                f"Task:\n{task}\n\nPlan:\n{plan}",
                model_config,
                seed,
                max_turns,
                dry_run,
                trace,
                run_id,
            )
            usage = _add_usage(usage, u)
            llm_calls += 0 if dry_run else 1
            turns += 1
            generated_code = extract_python_code(code_text)
            trace.emit("coder", 1, "handoff", to_role="reviewer")

            review, u = await _autogen_call_role(
                "reviewer",
                2,
                f"Task:\n{task}\n\nSolution:\n{generated_code}",
                model_config,
                seed,
                max_turns,
                dry_run,
                trace,
                run_id,
            )
            usage = _add_usage(usage, u)
            llm_calls += 0 if dry_run else 1
            turns += 1
            needs_revision = _review_requests_revision(review)
            trace.emit("reviewer", 2, "review_decision", accepted=not needs_revision)

            step = 3
            while topology == CLOSED_LOOP and needs_revision and revisions < MAX_REVISIONS:
                revisions += 1
                trace.emit("reviewer", step - 1, "revision_request", revision_round=revisions)
                code_text, u = await _autogen_call_role(
                    "coder",
                    step,
                    f"Task:\n{task}\n\nPrior solution:\n{generated_code}\n\nReviewer request:\n{review}",
                    model_config,
                    seed,
                    max_turns,
                    dry_run,
                    trace,
                    run_id,
                )
                usage = _add_usage(usage, u)
                llm_calls += 0 if dry_run else 1
                turns += 1
                generated_code = extract_python_code(code_text)
                trace.emit("coder", step, "attempted_recovery", revision_round=revisions)
                step += 1
                review, u = await _autogen_call_role(
                    "reviewer",
                    step,
                    f"Task:\n{task}\n\nSolution:\n{generated_code}",
                    model_config,
                    seed,
                    max_turns,
                    dry_run,
                    trace,
                    run_id,
                )
                usage = _add_usage(usage, u)
                llm_calls += 0 if dry_run else 1
                turns += 1
                needs_revision = _review_requests_revision(review)
                trace.emit("reviewer", step, "review_decision", accepted=not needs_revision)
                step += 1
        except Exception as exc:  # noqa: BLE001
            reported_error = 1.0
            exception = repr(exc)
            termination = "agent_exception"
            trace.emit("coordinator", turns, "error_detection", exception=exception)
        return _finish_result(
            model_config=model_config,
            t0=t0,
            trace=trace,
            generated_code=generated_code,
            usage=usage,
            llm_calls=llm_calls,
            turns=turns,
            reported_error=reported_error,
            termination_reason=termination,
            exception=exception,
            revisions=revisions,
        )

    try:
        asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, _async()).result(timeout=model_config.request_timeout_s * 6 + 60)
    except RuntimeError:
        return asyncio.run(_async())


async def _autogen_call_role(
    role: str,
    step: int,
    user_content: str,
    model_config: ModelConfig,
    seed: int,
    max_turns: int,
    dry_run: bool,
    trace: _NativeTrace,
    run_id: str,
) -> tuple[str, dict[str, int]]:
    trace.emit(role, step, "agent_activation", framework="autogen")
    trace.emit(role, step, "llm_invocation", framework="autogen")
    if dry_run:
        text = _dry_role_response(role, user_content)
        trace.emit(role, step, "sent_message", content_preview=text[:500])
        return text, _usage_dict()

    try:
        from autogen_agentchat.agents import AssistantAgent
        from autogen_core import CancellationToken
        from autogen_ext.models.openai import OpenAIChatCompletionClient
        from autogen_ext.models.openai._model_info import ModelInfo
    except ImportError as exc:
        raise ImportError("AutoGen native multi-agent requires autogen-agentchat and autogen-ext") from exc

    client = OpenAIChatCompletionClient(
        model=model_config.name,
        base_url=model_config.current_base_url,
        api_key=model_config.resolved_api_key(),
        temperature=model_config.temperature,
        max_tokens=model_config.max_tokens,
        timeout=model_config.request_timeout_s,
        max_retries=model_config.transport_retries,
        model_extra={"seed": seed},
        default_headers=_trace_headers(run_id, role, step),
        model_info=ModelInfo(
            vision=False,
            function_calling=True,
            json_output=True,
            family="unknown",
            structured_output=False,
        ),
    )
    agent = AssistantAgent(
        name=role,
        model_client=client,
        system_message=ROLE_PROMPTS[role],
    )
    try:
        with _role_env(run_id, role, step):
            result = await agent.run(task=user_content, cancellation_token=CancellationToken())
        text = ""
        for msg in getattr(result, "messages", []):
            if str(getattr(msg, "source", "")) not in {"", "user"}:
                text = _message_content(msg)
        usage_obj = client.total_usage()
        usage = _usage_dict(
            getattr(usage_obj, "prompt_tokens", 0),
            getattr(usage_obj, "completion_tokens", 0),
        )
        trace.emit(role, step, "sent_message", content_preview=text[:500])
        return text, usage
    finally:
        try:
            await client.close()
        except Exception:
            pass


def _run_langgraph_native(
    *,
    model_config: ModelConfig,
    topology: str,
    task: str,
    seed: int,
    max_turns: int,
    dry_run: bool,
    study_id: str,
    pair_id: str,
    run_id: str,
) -> AgentRunResult:
    t0 = time.time()
    trace = _NativeTrace(study_id=study_id, pair_id=pair_id, run_id=run_id)
    if dry_run:
        return _run_dry_native(model_config, topology, task, trace)
    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_openai import ChatOpenAI
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:
        raise ImportError("LangGraph native multi-agent requires langgraph and langchain-openai") from exc

    class _State(TypedDict, total=False):
        task: str
        plan: str
        code_text: str
        final_code: str
        review: str
        needs_revision: bool
        revision_round: int
        step: int
        usage: dict[str, int]
        llm_calls: int

    def call(role: str, step: int, content: str) -> tuple[str, dict[str, int]]:
        trace.emit(role, step, "agent_activation", framework="langgraph")
        trace.emit(role, step, "llm_invocation", framework="langgraph")
        llm = ChatOpenAI(
            model=model_config.name,
            base_url=model_config.current_base_url,
            api_key=model_config.resolved_api_key(),
            temperature=model_config.temperature,
            max_completion_tokens=model_config.max_tokens,
            timeout=model_config.request_timeout_s,
            max_retries=model_config.transport_retries,
            seed=seed,
            default_headers=_trace_headers(run_id, role, step),
        )
        with _role_env(run_id, role, step):
            msg = llm.invoke([SystemMessage(content=ROLE_PROMPTS[role]), HumanMessage(content=content)])
        text = _message_content(msg)
        meta = getattr(msg, "usage_metadata", None) or {}
        usage = _usage_dict(meta.get("input_tokens", 0), meta.get("output_tokens", 0))
        trace.emit(role, step, "sent_message", content_preview=text[:500])
        return text, usage

    def add_usage(state: _State, usage: dict[str, int]) -> None:
        state["usage"] = _add_usage(state.get("usage", _usage_dict()), usage)
        state["llm_calls"] = int(state.get("llm_calls", 0)) + 1

    def planner_node(state: _State) -> _State:
        step = int(state.get("step", 0))
        text, usage = call("planner", step, state["task"])
        state["plan"] = text
        add_usage(state, usage)
        trace.emit("planner", step, "handoff", to_role="coder")
        state["step"] = step + 1
        return state

    def coder_node(state: _State) -> _State:
        step = int(state.get("step", 1))
        prompt = (
            f"Task:\n{state['task']}\n\nPrior solution:\n{state.get('final_code', '')}\n\n"
            f"Reviewer request:\n{state.get('review', '')}"
            if state.get("revision_round", 0)
            else f"Task:\n{state['task']}\n\nPlan:\n{state.get('plan', '')}"
        )
        text, usage = call("coder", step, prompt)
        state["code_text"] = text
        state["final_code"] = extract_python_code(text)
        add_usage(state, usage)
        event = "attempted_recovery" if state.get("revision_round", 0) else "handoff"
        trace.emit("coder", step, event, to_role="reviewer", revision_round=state.get("revision_round", 0))
        state["step"] = step + 1
        return state

    def reviewer_node(state: _State) -> _State:
        step = int(state.get("step", 2))
        text, usage = call("reviewer", step, f"Task:\n{state['task']}\n\nSolution:\n{state.get('final_code', '')}")
        state["review"] = text
        needs = _review_requests_revision(text)
        state["needs_revision"] = needs
        if needs:
            state["revision_round"] = int(state.get("revision_round", 0)) + 1
        add_usage(state, usage)
        trace.emit("reviewer", step, "review_decision", accepted=not needs)
        if needs:
            trace.emit("reviewer", step, "revision_request", revision_round=state["revision_round"])
        state["step"] = step + 1
        return state

    def route_after_review(state: _State) -> str:
        if topology == CLOSED_LOOP and state.get("needs_revision") and int(state.get("revision_round", 0)) < MAX_REVISIONS:
            return "coder"
        return END

    reported_error = 0.0
    exception = ""
    termination = "completed"
    final: _State = {}
    try:
        graph = StateGraph(_State)
        graph.add_node("planner", planner_node)
        graph.add_node("coder", coder_node)
        graph.add_node("reviewer", reviewer_node)
        graph.add_edge(START, "planner")
        graph.add_edge("planner", "coder")
        graph.add_edge("coder", "reviewer")
        graph.add_conditional_edges("reviewer", route_after_review, {"coder": "coder", END: END})
        app = graph.compile()
        final = app.invoke(
            {
                "task": task,
                "step": 0,
                "revision_round": 0,
                "usage": _usage_dict(),
                "llm_calls": 0,
            },
            config={"recursion_limit": max(max_turns * 3, 10)},
        )
    except Exception as exc:  # noqa: BLE001
        reported_error = 1.0
        exception = repr(exc)
        termination = "agent_exception"
        trace.emit("coordinator", 0, "error_detection", exception=exception)
    return _finish_result(
        model_config=model_config,
        t0=t0,
        trace=trace,
        generated_code=str(final.get("final_code", "")),
        usage=final.get("usage", _usage_dict()),
        llm_calls=int(final.get("llm_calls", 0)),
        turns=int(final.get("llm_calls", 0)),
        reported_error=reported_error,
        termination_reason=termination,
        exception=exception,
        revisions=int(final.get("revision_round", 0)),
    )


def _run_crewai_native(
    *,
    model_config: ModelConfig,
    topology: str,
    task: str,
    seed: int,
    max_turns: int,
    dry_run: bool,
    study_id: str,
    pair_id: str,
    run_id: str,
) -> AgentRunResult:
    t0 = time.time()
    trace = _NativeTrace(study_id=study_id, pair_id=pair_id, run_id=run_id)
    if dry_run:
        return _run_dry_native(model_config, topology, task, trace)
    try:
        from crewai import Agent, Crew, Process, Task, LLM
    except ImportError as exc:
        raise ImportError("CrewAI native multi-agent requires crewai") from exc

    usage = _usage_dict()
    llm_calls = 0
    turns = 0
    revisions = 0
    generated_code = ""
    reported_error = 0.0
    exception = ""
    termination = "completed"

    def call(role: str, step: int, content: str) -> tuple[str, dict[str, int]]:
        nonlocal llm_calls, turns
        trace.emit(role, step, "agent_activation", framework="crewai")
        trace.emit(role, step, "llm_invocation", framework="crewai")
        kwargs = _crewai_llm_kwargs(LLM, model_config, api_key=model_config.resolved_api_key(), seed=seed)
        if "additional_params" in inspect.signature(LLM).parameters:
            kwargs["additional_params"] = {
                **dict(kwargs.get("additional_params") or {}),
                "extra_headers": _trace_headers(run_id, role, step),
            }
        llm = LLM(**kwargs)
        agent = Agent(
            role=role.title(),
            goal=ROLE_PROMPTS[role],
            backstory=f"You are the {role} in a three-role software engineering crew.",
            llm=llm,
            max_iter=max_turns,
            verbose=False,
            allow_delegation=False,
        )
        task_obj = Task(
            description=content,
            expected_output=(
                "A concise planning response."
                if role == "planner"
                else "A Python solution in a ```python ... ``` code block."
                if role == "coder"
                else "ACCEPT or REVISION_REQUEST with a short reason."
            ),
            agent=agent,
        )
        crew = Crew(agents=[agent], tasks=[task_obj], process=Process.sequential, verbose=False)
        with _role_env(run_id, role, step):
            crew_result = crew.kickoff()
        text = str(getattr(crew_result, "raw", None) or crew_result)
        token_usage = getattr(crew_result, "token_usage", None) or getattr(crew_result, "usage_metrics", None)
        role_usage = _crew_usage(token_usage)
        llm_calls += 1
        turns += 1
        trace.emit(role, step, "sent_message", content_preview=text[:500])
        return text, role_usage

    try:
        plan, u = call("planner", 0, task)
        usage = _add_usage(usage, u)
        trace.emit("planner", 0, "handoff", to_role="coder")
        code_text, u = call("coder", 1, f"Task:\n{task}\n\nPlan:\n{plan}")
        usage = _add_usage(usage, u)
        generated_code = extract_python_code(code_text)
        trace.emit("coder", 1, "handoff", to_role="reviewer")
        review, u = call("reviewer", 2, f"Task:\n{task}\n\nSolution:\n{generated_code}")
        usage = _add_usage(usage, u)
        needs_revision = _review_requests_revision(review)
        trace.emit("reviewer", 2, "review_decision", accepted=not needs_revision)
        step = 3
        while topology == CLOSED_LOOP and needs_revision and revisions < MAX_REVISIONS:
            revisions += 1
            trace.emit("reviewer", step - 1, "revision_request", revision_round=revisions)
            code_text, u = call(
                "coder",
                step,
                f"Task:\n{task}\n\nPrior solution:\n{generated_code}\n\nReviewer request:\n{review}",
            )
            usage = _add_usage(usage, u)
            generated_code = extract_python_code(code_text)
            trace.emit("coder", step, "attempted_recovery", revision_round=revisions)
            step += 1
            review, u = call("reviewer", step, f"Task:\n{task}\n\nSolution:\n{generated_code}")
            usage = _add_usage(usage, u)
            needs_revision = _review_requests_revision(review)
            trace.emit("reviewer", step, "review_decision", accepted=not needs_revision)
            step += 1
    except Exception as exc:  # noqa: BLE001
        reported_error = 1.0
        exception = repr(exc)
        termination = "agent_exception"
        trace.emit("coordinator", turns, "error_detection", exception=exception)

    return _finish_result(
        model_config=model_config,
        t0=t0,
        trace=trace,
        generated_code=generated_code,
        usage=usage,
        llm_calls=llm_calls,
        turns=turns,
        reported_error=reported_error,
        termination_reason=termination,
        exception=exception,
        revisions=revisions,
    )


def _run_dry_native(
    model_config: ModelConfig,
    topology: str,
    task: str,
    trace: _NativeTrace,
) -> AgentRunResult:
    t0 = time.time()
    trace.emit("planner", 0, "agent_activation", dry_run=True)
    plan = _dry_role_response("planner", task)
    trace.emit("planner", 0, "sent_message", content_preview=plan)
    trace.emit("planner", 0, "handoff", to_role="coder")
    trace.emit("coder", 1, "agent_activation", dry_run=True)
    code = _dry_role_response("coder", task)
    trace.emit("coder", 1, "sent_message", content_preview=code)
    trace.emit("coder", 1, "handoff", to_role="reviewer")
    trace.emit("reviewer", 2, "agent_activation", dry_run=True)
    review = _dry_role_response("reviewer", task)
    trace.emit("reviewer", 2, "review_decision", accepted=True, content_preview=review)
    return _finish_result(
        model_config=model_config,
        t0=t0,
        trace=trace,
        generated_code=extract_python_code(code),
        usage=_usage_dict(),
        llm_calls=0,
        turns=3,
        revisions=0 if topology in {LINEAR, CLOSED_LOOP} else 0,
    )


def _add_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    return _usage_dict(
        int(left.get("prompt_tokens", 0)) + int(right.get("prompt_tokens", 0)),
        int(left.get("completion_tokens", 0)) + int(right.get("completion_tokens", 0)),
    )


def _crew_usage(usage: Any) -> dict[str, int]:
    if not usage:
        return _usage_dict()
    if isinstance(usage, dict):
        return _usage_dict(
            usage.get("prompt_tokens") or usage.get("total_prompt_tokens") or 0,
            usage.get("completion_tokens") or usage.get("total_completion_tokens") or 0,
        )
    return _usage_dict(
        getattr(usage, "prompt_tokens", 0) or getattr(usage, "total_prompt_tokens", 0),
        getattr(usage, "completion_tokens", 0) or getattr(usage, "total_completion_tokens", 0),
    )
