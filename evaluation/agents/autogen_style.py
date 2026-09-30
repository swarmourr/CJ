"""AutoGen-style agent adapter.

Architecture
------------
AutoGen (Microsoft, 2023) uses a conversational multi-agent pattern where
a UserProxyAgent and an AssistantAgent exchange messages iteratively.  The
UserProxy executes code produced by the Assistant and sends results back;
the loop continues until the task is solved or max_turns is reached.

This adapter implements the same two-agent conversation pattern using a
direct OpenAI-compatible API client.  It does NOT import the autogen or
pyautogen library to avoid transitive dependency conflicts.

Deviations from the published AutoGen architecture:
  1. Code execution happens in the evaluation sandbox (executor.py) rather
     than AutoGen's built-in LocalCommandLineCodeExecutor/DockerExecutor.
  2. The termination condition uses "TERMINATE" sentinel text in the
     assistant response (same as AutoGen) plus an explicit max_turns cap.
  3. GroupChat is not implemented — only the two-agent UserProxy+Assistant
     pair is used, which covers the published AutoGen coding benchmark.
  4. Human-in-the-loop is disabled (human_input_mode="NEVER").

Reference: Wu et al., "AutoGen: Enabling Next-Gen LLM Applications via
Multi-Agent Conversation", arXiv:2308.08155, 2023.
"""

from __future__ import annotations

import time

from evaluation.agents.base import AgentRunResult, AgentSystem, ModelClient


class AutoGenStyleAgent(AgentSystem):
    """AutoGen-style two-agent (UserProxy + Assistant) coding agent.

    Parameters
    ----------
    client : ModelClient, optional
        Pre-configured model client. Created from env-vars if not provided.
    max_turns : int
        Maximum conversation turns before giving up. Default 10.
    dry_run : bool
        If True no real API calls are made (stub responses).
    executor : callable, optional
        ``executor(code: str) -> (success: bool, output: str)``
        Injected for testing; defaults to the sandbox executor.
    """

    name = "autogen-style"

    # System prompts matching AutoGen's published coding agent prompts.
    _ASSISTANT_SYSTEM = (
        "You are a helpful AI assistant. "
        "Solve coding problems step by step. "
        "When you have a final solution, wrap it in a Python code block. "
        "Reply TERMINATE when the task is done."
    )
    _PROXY_INTRO = (
        "I am a user proxy. I will execute any Python code you provide and "
        "report the result back to you."
    )

    def __init__(
        self,
        client: ModelClient | None = None,
        max_turns: int = 10,
        dry_run: bool = False,
        executor=None,
    ) -> None:
        super().__init__(client=client, max_turns=max_turns, dry_run=dry_run)
        self._executor = executor  # injected executor(code) -> (bool, str)

    def run(self, task: str, seed: int = 0) -> AgentRunResult:
        from evaluation.benchmarks.executor import sandbox_exec

        executor = self._executor or sandbox_exec
        t0 = time.time()

        messages: list[dict] = [
            {"role": "system", "content": self._ASSISTANT_SYSTEM},
            {"role": "user",   "content": task},
        ]

        trace: list[dict] = []
        prompt_tokens = completion_tokens = 0
        llm_calls = tool_calls = retries = turns = 0
        final_code = ""
        termination_reason = "max_turns"
        reported_error = 0.0
        exec_success = False
        tests_passed = tests_total = 0

        for turn in range(self.max_turns):
            turns = turn + 1

            # ── Assistant turn ────────────────────────────────────────────────
            try:
                resp = self.client.chat(messages, seed=seed)
                llm_calls += 1
            except RuntimeError as exc:
                reported_error = 1.0
                termination_reason = "api_error"
                trace.append({"turn": turns, "role": "api_error", "content": str(exc)})
                retries += 1
                if retries > 3:
                    break
                time.sleep(0.5)
                continue

            u = self.client.usage(resp)
            prompt_tokens     += u["prompt_tokens"]
            completion_tokens += u["completion_tokens"]

            assistant_msg = resp["choices"][0]["message"]["content"] or ""
            messages.append({"role": "assistant", "content": assistant_msg})
            trace.append({"turn": turns, "role": "assistant", "content": assistant_msg})

            # ── Termination check ─────────────────────────────────────────────
            if "TERMINATE" in assistant_msg:
                termination_reason = "success"
                break

            # ── Extract and execute code ───────────────────────────────────────
            code = self._extract_code(assistant_msg)
            if code:
                final_code = code
                tool_calls += 1
                ok, output = executor(code)
                exec_success = ok
                feedback = output[:2000]  # cap proxy message size

                proxy_msg = (
                    f"exitcode: {'0' if ok else '1'}\n"
                    f"(execution {'succeeded' if ok else 'failed'})\n"
                    f"Code output:\n{feedback}"
                )
                if ok:
                    proxy_msg += "\nTERMINATE"
                    termination_reason = "success"
                    messages.append({"role": "user", "content": proxy_msg})
                    trace.append({"turn": turns, "role": "user_proxy", "content": proxy_msg})
                    break
                else:
                    messages.append({"role": "user", "content": proxy_msg})
                    trace.append({"turn": turns, "role": "user_proxy", "content": proxy_msg})
            else:
                # No code found — keep conversing
                messages.append({
                    "role": "user",
                    "content": "Please provide a Python code block with your solution.",
                })

        duration_s = time.time() - t0
        total_tokens = prompt_tokens + completion_tokens

        return AgentRunResult(
            success=1.0 if exec_success else 0.0,
            duration_s=round(duration_s, 3),
            reported_error=reported_error,
            retries=retries,
            llm_calls=llm_calls,
            tool_calls=tool_calls,
            turns=turns,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            tests_passed=tests_passed,
            tests_total=tests_total,
            generated_code=final_code,
            execution_trace=trace,
            termination_reason=termination_reason,
        )
