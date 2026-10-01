"""AutoGen real agent adapter.

Uses the official ``autogen-agentchat`` and ``autogen-ext[openai]`` packages
(AG2, Microsoft).  Requires:

    pip install "chaos-jungle[evaluation]"
    # or individually:
    pip install "autogen-agentchat>=0.4.0,<0.8.0" "autogen-ext[openai]>=0.4.0,<0.8.0"

Architecture
------------
A single :class:`~autogen_agentchat.agents.AssistantAgent` with an
``execute_code`` tool.  The agent loop:

  1. LLM receives the task description.
  2. LLM optionally calls ``execute_code`` to test a candidate solution.
  3. The tool result is returned as a ``role="tool"`` message (triggering
     CJ :class:`~chaos_jungle.faults.llm.ToolFault` if active).
  4. LLM produces a final text answer.
  5. The adapter extracts the Python code block from the final message.

A fresh :class:`~autogen_ext.models.openai.OpenAIChatCompletionClient` is
created inside :meth:`run` on every call, reading ``CJ_EVAL_BASE_URL`` at
that instant.  This guarantees the correct proxy URL is used regardless of
when the CJ fault was activated relative to agent construction.

SDK retries are set to 0 (``max_retries=0``) so CJ fault effects are never
silently masked.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any

from evaluation.agents.base import AgentRunResult, AgentSystem
from evaluation.model_config import ModelConfig


class AutoGenRealAgent(AgentSystem):
    """AutoGen coding agent using official autogen-agentchat >= 0.4.0."""

    name = "autogen-real"
    uses_model_config: bool = True  # signals run.py to pass ModelConfig

    def __init__(self, model_config: ModelConfig, max_turns: int = 10) -> None:
        # Do NOT call super().__init__() with a ModelClient — this adapter uses
        # the autogen SDK directly.  The model_name property is satisfied below.
        self.client      = None           # not used; satisfies AgentSystem contract
        self.max_turns   = max_turns
        self.dry_run     = False
        self._model_config = model_config
        self._model_name   = model_config.name

    # ------------------------------------------------------------------
    # AgentSystem interface
    # ------------------------------------------------------------------

    def run(self, task: str, seed: int = 0) -> AgentRunResult:
        """Execute the AutoGen agent synchronously.

        Works both in bare contexts and when called from within a running
        event loop (e.g. Jupyter, pytest-anyio) by detecting the loop first.
        """
        import concurrent.futures

        try:
            asyncio.get_running_loop()
            # A loop is already running — submit to a fresh thread so asyncio.run() is safe.
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, self._run_async(task, seed))
                return future.result(timeout=self._model_config.request_timeout_s + 30)
        except RuntimeError:
            # No running loop — asyncio.run() is safe.
            return asyncio.run(self._run_async(task, seed))

    # ------------------------------------------------------------------
    # Async implementation
    # ------------------------------------------------------------------

    async def _run_async(self, task: str, seed: int) -> AgentRunResult:
        try:
            from autogen_agentchat.agents import AssistantAgent
            from autogen_ext.models.openai import OpenAIChatCompletionClient
        except ImportError as exc:
            raise ImportError(
                "autogen-agentchat and autogen-ext[openai] are required for "
                "AutoGenRealAgent.  Install with: pip install 'chaos-jungle[evaluation]'"
            ) from exc

        cfg      = self._model_config
        base_url = cfg.current_base_url   # re-read AFTER fault.start() changes env
        api_key  = cfg.resolved_api_key()

        if not base_url:
            raise RuntimeError(
                "CJ_EVAL_BASE_URL is not set.  Cannot run AutoGenRealAgent."
            )

        t0 = time.time()
        trace: list[dict] = []
        prompt_tokens = completion_tokens = 0
        llm_calls = tool_calls = 0
        termination_reason = "max_turns"
        reported_error = 0.0
        final_code = ""
        exception_str = ""

        # model_info is required for non-standard model names in autogen-ext 0.4+.
        # Provide a permissive info dict so any OpenAI-compatible model works.
        try:
            from autogen_ext.models.openai._model_info import ModelInfo
            _model_info: ModelInfo | None = ModelInfo(
                vision=False,
                function_calling=True,
                json_output=True,
                family="unknown",
                structured_output=False,
            )
        except Exception:
            _model_info = None  # older version; no model_info param

        # seed is passed as model-specific extra arg for reproducible completions.
        client_kwargs: dict = dict(
            model=cfg.name,
            base_url=base_url,
            api_key=api_key,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            timeout=cfg.request_timeout_s,
            max_retries=cfg.transport_retries,
            model_extra={"seed": seed},
        )
        if _model_info is not None:
            client_kwargs["model_info"] = _model_info

        # Fresh SDK client — picks up the proxy URL that was set by fault.start()
        model_client = OpenAIChatCompletionClient(**client_kwargs)

        # Code-execution tool — exposed to the LLM via function calling
        def execute_code(code: str) -> str:
            """Execute Python code in a sandbox. Returns exit_code and output."""
            from evaluation.benchmarks.executor import sandbox_exec
            ok, output = sandbox_exec(code, timeout_s=10.0)
            return f"exit_code={'0' if ok else '1'}\n{output[:1500]}"

        try:
            from autogen_core.tools import FunctionTool
            exec_tool = FunctionTool(execute_code, description=(
                "Execute Python code in an isolated sandbox. "
                "Use this to test candidate solutions."
            ))
        except ImportError:
            exec_tool = None  # framework too old or not installed

        tools = [exec_tool] if exec_tool is not None else []

        agent = AssistantAgent(
            name="coder",
            model_client=model_client,
            tools=tools,
            system_message=(
                "You are an expert Python programmer. "
                "Solve the given coding problem. "
                "Use the execute_code tool to test your solution if available. "
                "Return the final solution in a ```python ... ``` block."
            ),
            # reflect_on_tool_use=True: after executing a tool the agent asks the
            # LLM again with the tool result as a role="tool" message.  This is
            # required for ToolFault to activate (it intercepts that second request).
            reflect_on_tool_use=True,
            # Allow up to max_turns tool-use iterations.
            max_tool_iterations=self.max_turns,
        )

        try:
            from autogen_core import CancellationToken
            try:
                from autogen_agentchat.messages import TextMessage
                task_msg = TextMessage(content=task, source="user")
            except ImportError:
                task_msg = task  # older versions accept plain strings

            result = await agent.run(
                task=task_msg,
                cancellation_token=CancellationToken(),
            )

            # --- Parse result messages ---
            for msg in result.messages:
                type_name = type(msg).__name__
                source    = str(getattr(msg, "source", ""))
                content   = getattr(msg, "content", "")

                # Tool execution result messages
                if any(kw in type_name for kw in ("Execution", "Result", "Summary")):
                    if "Tool" in type_name or "Function" in type_name:
                        tool_calls += 1
                # Tool call requests (LLM turn)
                elif "ToolCall" in type_name and "Request" in type_name:
                    llm_calls += 1
                # Regular text messages from the agent
                elif isinstance(content, str) and content and source not in ("user", ""):
                    llm_calls += 1
                    if "def " in content or "```" in content:
                        code_candidate = self._extract_code(content)
                        if code_candidate:
                            final_code = code_candidate

                trace.append({
                    "role":    source or type_name,
                    "content": str(content)[:400] if content else "",
                })

            # --- Token usage ---
            try:
                usage = model_client.total_usage()
                prompt_tokens     = int(getattr(usage, "prompt_tokens",     0))
                completion_tokens = int(getattr(usage, "completion_tokens", 0))
            except Exception:
                pass

            stop_reason = getattr(result, "stop_reason", None)
            termination_reason = str(stop_reason) if stop_reason else "completed"

        except Exception as exc:
            reported_error   = 1.0
            exception_str    = str(exc)
            termination_reason = "agent_exception"
            trace.append({"role": "error", "content": exception_str[:400]})

        finally:
            try:
                await model_client.close()
            except Exception:
                pass

        duration_s   = time.time() - t0
        total_tokens = prompt_tokens + completion_tokens
        cost         = cfg.compute_cost(prompt_tokens, completion_tokens)

        return AgentRunResult(
            success=0.0,          # evaluated independently by EvalPlus
            duration_s=round(duration_s, 3),
            reported_error=reported_error,
            retries=0,
            llm_calls=max(llm_calls, 1) if not exception_str else llm_calls,
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

    # ------------------------------------------------------------------
    # Framework version introspection
    # ------------------------------------------------------------------

    @staticmethod
    def framework_version() -> str:
        """Return installed autogen-agentchat version string."""
        try:
            from importlib.metadata import version
            return f"autogen-agentchat {version('autogen-agentchat')}"
        except Exception:
            return "autogen-agentchat (version unknown)"
