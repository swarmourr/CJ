"""LangGraph real agent adapter.

Uses the official ``langgraph`` and ``langchain-openai`` packages.  Requires:

    pip install "chaos-jungle[evaluation]"
    # or individually:
    pip install "langgraph>=0.2.0,<2.0.0" "langchain-openai>=0.1.0,<2.0.0"

Architecture
------------
A :func:`~langgraph.prebuilt.create_react_agent` graph with a single
``execute_python_code`` tool.  The ReAct loop:

  1. LLM receives the task description.
  2. LLM optionally emits a ``tool_calls`` response requesting code execution.
  3. LangGraph's :class:`~langgraph.prebuilt.ToolNode` executes the tool and
     returns a :class:`~langchain_core.messages.ToolMessage` (``role="tool"``),
     which crosses the CJ proxy and triggers
     :class:`~chaos_jungle.faults.llm.ToolFault` if active.
  4. LLM produces the final text answer.
  5. The adapter extracts the Python code block from the last AI message.

A fresh :class:`~langchain_openai.ChatOpenAI` instance is created inside
:meth:`run` on every call so the proxy URL is always current.

``max_retries=0`` is enforced on the OpenAI transport so CJ fault effects are
not silently masked.
"""

from __future__ import annotations

import time

from evaluation.agents.base import AgentRunResult, AgentSystem
from evaluation.model_config import ModelConfig


class LangGraphRealAgent(AgentSystem):
    """LangGraph ReAct coding agent using official langraph >= 0.2."""

    name = "langgraph-real"
    uses_model_config: bool = True

    def __init__(self, model_config: ModelConfig, max_turns: int = 10) -> None:
        self.client      = None
        self.max_turns   = max_turns
        self.dry_run     = False
        self._model_config = model_config
        self._model_name   = model_config.name

    # ------------------------------------------------------------------
    # AgentSystem interface
    # ------------------------------------------------------------------

    def run(self, task: str, seed: int = 0) -> AgentRunResult:
        try:
            from langchain_openai import ChatOpenAI
            from langgraph.prebuilt import create_react_agent
            from langchain_core.messages import HumanMessage, AIMessage, ToolMessage
            from langchain_core.tools import tool as lc_tool
        except ImportError as exc:
            raise ImportError(
                "langgraph and langchain-openai are required for LangGraphRealAgent. "
                "Install with: pip install 'chaos-jungle[evaluation]'"
            ) from exc

        cfg      = self._model_config
        base_url = cfg.current_base_url   # re-read AFTER fault.start()
        api_key  = cfg.resolved_api_key()

        if not base_url:
            raise RuntimeError(
                "CJ_EVAL_BASE_URL is not set.  Cannot run LangGraphRealAgent."
            )

        t0 = time.time()
        trace: list[dict] = []
        prompt_tokens = completion_tokens = 0
        tool_calls = 0
        termination_reason = "completed"
        reported_error = 0.0
        final_code = ""
        exception_str = ""

        # Fresh LLM client — binds to the current (possibly proxy) URL.
        # seed is passed to the OpenAI API for reproducible completions.
        llm = ChatOpenAI(
            model=cfg.name,
            base_url=base_url,
            api_key=api_key,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            timeout=cfg.request_timeout_s,
            max_retries=cfg.transport_retries,
            seed=seed,
        )

        @lc_tool
        def execute_python_code(code: str) -> str:
            """Execute Python code in an isolated sandbox. Returns exit code and output."""
            from evaluation.benchmarks.executor import sandbox_exec
            ok, output = sandbox_exec(code, timeout_s=10.0)
            return f"exit_code={'0' if ok else '1'}\n{output[:1500]}"

        system_prompt = (
            "You are an expert Python programmer. "
            "Solve the given coding problem. "
            "Use the execute_python_code tool to test your solution. "
            "Return the final solution in a ```python ... ``` block."
        )
        graph = create_react_agent(
            llm,
            tools=[execute_python_code],
            prompt=system_prompt,   # langgraph >= 1.0 uses 'prompt' (not state_modifier)
        )

        try:
            result = graph.invoke(
                {"messages": [HumanMessage(content=task)]},
                config={"recursion_limit": self.max_turns * 2},
            )

            messages = result.get("messages", [])
            llm_calls_count = 0

            for msg in messages:
                role    = getattr(msg, "type", type(msg).__name__)
                content = getattr(msg, "content", "")
                name    = getattr(msg, "name",    "")

                if isinstance(msg, AIMessage):
                    llm_calls_count += 1
                    # Collect token usage from each AI message
                    usage_meta = getattr(msg, "usage_metadata", None) or {}
                    prompt_tokens     += int(usage_meta.get("input_tokens",  0))
                    completion_tokens += int(usage_meta.get("output_tokens", 0))
                    if isinstance(content, str) and ("def " in content or "```" in content):
                        candidate = self._extract_code(content)
                        if candidate:
                            final_code = candidate

                elif isinstance(msg, ToolMessage):
                    tool_calls += 1
                    role = "tool"

                trace.append({
                    "role":    role,
                    "content": (str(content) if content else "")[:400],
                    "name":    name,
                })

            llm_calls = llm_calls_count or 1

        except Exception as exc:
            reported_error     = 1.0
            exception_str      = str(exc)
            termination_reason = "agent_exception"
            llm_calls          = 0
            trace.append({"role": "error", "content": exception_str[:400]})

        duration_s   = time.time() - t0
        total_tokens = prompt_tokens + completion_tokens
        cost         = cfg.compute_cost(prompt_tokens, completion_tokens)

        return AgentRunResult(
            success=0.0,
            duration_s=round(duration_s, 3),
            reported_error=reported_error,
            retries=0,
            llm_calls=llm_calls,
            tool_calls=tool_calls,
            turns=llm_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=cost if cost is not None else 0.0,
            generated_code=final_code,
            execution_trace=trace,
            termination_reason=termination_reason,
            exception=exception_str,
        )

    @staticmethod
    def framework_version() -> str:
        try:
            from importlib.metadata import version
            lg = version("langgraph")
            lco = version("langchain-openai")
            return f"langgraph {lg}, langchain-openai {lco}"
        except Exception:
            return "langgraph (version unknown)"
