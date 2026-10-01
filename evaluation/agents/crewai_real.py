"""CrewAI real agent adapter.

Uses the official ``crewai`` package.  Requires:

    pip install "chaos-jungle[evaluation]"
    # or individually:
    pip install "crewai>=0.100.0,<2.0.0"

Architecture
------------
A single-agent :class:`~crewai.Crew` (sequential process) with a Python
coder agent and a code-execution tool.  The crew:

  1. Coder agent receives the task description.
  2. Agent optionally calls the ``execute_python`` tool to test a solution.
  3. The tool result triggers ``role="tool"`` messages in the underlying LLM
     request, which the CJ proxy intercepts for
     :class:`~chaos_jungle.faults.llm.ToolFault` if active.
  4. Agent produces the final answer.
  5. The adapter extracts the Python code block from the raw crew output.

A fresh :class:`~crewai.LLM` instance is created inside :meth:`run` on every
call, binding to the current ``CJ_EVAL_BASE_URL``.

``max_retries=0`` is passed to the underlying LiteLLM transport so CJ fault
effects are not silently masked.

Tool-call support note
----------------------
CrewAI uses LiteLLM for model calls.  LiteLLM routes tool-result messages as
``role="tool"`` in OpenAI-compatible requests.  ToolFault is therefore
supported for this adapter.
"""

from __future__ import annotations

import time

from evaluation.agents.base import AgentRunResult, AgentSystem
from evaluation.model_config import ModelConfig


class CrewAIRealAgent(AgentSystem):
    """CrewAI coding crew using official crewai >= 0.100.0."""

    name = "crewai-real"
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
            from crewai import Agent, Task, Crew, Process, LLM
            from crewai.tools import BaseTool
        except ImportError as exc:
            raise ImportError(
                "crewai is required for CrewAIRealAgent. "
                "Install with: pip install 'chaos-jungle[evaluation]'"
            ) from exc

        cfg      = self._model_config
        base_url = cfg.current_base_url   # re-read AFTER fault.start()
        api_key  = cfg.resolved_api_key()

        if not base_url:
            raise RuntimeError(
                "CJ_EVAL_BASE_URL is not set.  Cannot run CrewAIRealAgent."
            )

        t0 = time.time()
        trace: list[dict] = []
        prompt_tokens = completion_tokens = 0
        tool_calls = 0
        termination_reason = "completed"
        reported_error = 0.0
        final_code = ""
        exception_str = ""

        # CrewAI LLM uses LiteLLM; "openai/" prefix selects the OpenAI provider.
        # Strip /v1 from base_url for LiteLLM (it appends the path itself).
        litellm_base_url = base_url.removesuffix("/v1") if base_url.endswith("/v1") else base_url

        llm = LLM(
            model=f"openai/{cfg.name}",
            base_url=litellm_base_url,
            api_key=api_key,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            timeout=cfg.request_timeout_s,
            max_retries=cfg.transport_retries,
        )

        # Code-execution tool using CrewAI's BaseTool protocol
        class _ExecuteCodeTool(BaseTool):
            name: str = "execute_python"
            description: str = (
                "Execute Python code in an isolated sandbox. "
                "Input: Python source code string. "
                "Returns: exit code and captured output."
            )

            def _run(self_, code: str) -> str:  # noqa: N805
                from evaluation.benchmarks.executor import sandbox_exec
                ok, output = sandbox_exec(code, timeout_s=10.0)
                nonlocal tool_calls
                tool_calls += 1
                return f"exit_code={'0' if ok else '1'}\n{output[:1500]}"

        exec_tool = _ExecuteCodeTool()

        coder = Agent(
            role="Python Coder",
            goal="Write a correct Python function that solves the given problem",
            backstory=(
                "You are an expert Python programmer specializing in algorithm design "
                "and competitive coding. You test solutions before submitting them."
            ),
            tools=[exec_tool],
            llm=llm,
            max_iter=self.max_turns,
            verbose=False,
            allow_delegation=False,
        )

        coding_task = Task(
            description=(
                f"{task}\n\n"
                "Return the final solution in a ```python ... ``` code block."
            ),
            expected_output=(
                "A working Python function in a ```python ... ``` code block "
                "that satisfies all test cases."
            ),
            agent=coder,
        )

        crew = Crew(
            agents=[coder],
            tasks=[coding_task],
            process=Process.sequential,
            verbose=False,
        )

        try:
            crew_result = crew.kickoff()

            raw_output = (
                getattr(crew_result, "raw", None)
                or str(crew_result)
            )

            if isinstance(raw_output, str):
                final_code = self._extract_code(raw_output)

            # Token usage — available as usage_metrics in recent CrewAI
            usage = getattr(crew_result, "token_usage", None) or getattr(crew_result, "usage_metrics", None)
            if usage:
                # CrewAI stores usage in various formats; handle both
                if hasattr(usage, "prompt_tokens"):
                    prompt_tokens     = int(usage.prompt_tokens)
                    completion_tokens = int(getattr(usage, "completion_tokens", 0))
                elif isinstance(usage, dict):
                    prompt_tokens     = int(usage.get("prompt_tokens", 0))
                    completion_tokens = int(usage.get("completion_tokens", 0))

            trace.append({
                "role":    "crew",
                "content": (raw_output or "")[:800],
            })

        except Exception as exc:
            reported_error     = 1.0
            exception_str      = str(exc)
            termination_reason = "agent_exception"
            trace.append({"role": "error", "content": exception_str[:400]})

        duration_s   = time.time() - t0
        total_tokens = prompt_tokens + completion_tokens
        cost         = cfg.compute_cost(prompt_tokens, completion_tokens)

        return AgentRunResult(
            success=0.0,
            duration_s=round(duration_s, 3),
            reported_error=reported_error,
            retries=0,
            llm_calls=1,                        # crew abstracts LLM call count
            tool_calls=tool_calls,
            turns=1,
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
            return f"crewai {version('crewai')}"
        except Exception:
            return "crewai (version unknown)"
