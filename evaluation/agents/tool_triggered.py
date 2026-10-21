"""Controlled tool-using agent for CJ tool-fault evaluation.

This adapter is intentionally framework-neutral and explicitly labelled as a
controlled workload.  It uses a real model endpoint plus a real Python
execution tool, and it deterministically sends a ``role="tool"`` message back
through the model API.  That makes CJ ``ToolFault`` experiments triggerable
without relying on whether a general coding agent happened to choose a tool.
"""

from __future__ import annotations

import time

from evaluation.agents.base import AgentRunResult, AgentSystem, extract_python_code


class ToolTriggeredControlledAgent(AgentSystem):
    """Two-step model/tool/model workload with guaranteed tool traffic."""

    name = "tool-triggered-controlled"

    def run(self, task: str, seed: int = 0) -> AgentRunResult:
        t0 = time.time()
        trace: list[dict] = []
        prompt_tokens = 0
        completion_tokens = 0
        llm_calls = 0
        tool_calls = 0
        reported_error = 0.0
        termination_reason = "completed"
        exception = ""
        generated_code = ""

        system = (
            "You are a Python coding agent in a controlled CJ tool-fault "
            "workload. Produce a candidate solution in a Python code block. "
            "A required Python execution tool will run before finalization."
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": task},
        ]

        try:
            first = self.client.chat(messages, seed=seed)
            llm_calls += 1
            usage = self.client.usage(first)
            prompt_tokens += usage.get("prompt_tokens", 0)
            completion_tokens += usage.get("completion_tokens", 0)
            first_text = first["choices"][0]["message"].get("content") or ""
            generated_code = extract_python_code(first_text)
            trace.append({
                "event_type": "llm_invocation",
                "role": "coder",
                "step": 0,
                "content_preview": first_text[:500],
            })

            from evaluation.benchmarks.executor import sandbox_exec

            code_to_execute = generated_code or first_text
            ok, output = sandbox_exec(code_to_execute, timeout_s=10.0)
            tool_calls += 1
            tool_result = f"exit_code={'0' if ok else '1'}\n{output[:1500]}"
            tool_call_id = "cj-controlled-execute-python"
            trace.append({
                "event_type": "tool_invocation",
                "role": "tool",
                "step": 1,
                "tool_name": "execute_python",
                "success": ok,
                "content_preview": tool_result[:500],
            })

            tool_messages = [
                *messages,
                {
                    "role": "assistant",
                    "content": first_text,
                    "tool_calls": [{
                        "id": tool_call_id,
                        "type": "function",
                        "function": {
                            "name": "execute_python",
                            "arguments": "{\"code\":\"candidate solution\"}",
                        },
                    }],
                },
                {
                    "role": "tool",
                    "name": "execute_python",
                    "tool_call_id": tool_call_id,
                    "content": tool_result,
                },
                {
                    "role": "user",
                    "content": (
                        "Use the tool result above. If the candidate is usable, "
                        "return the final solution in a Python code block."
                    ),
                },
            ]
            try:
                second = self.client.chat(tool_messages, seed=seed)
                llm_calls += 1
                usage = self.client.usage(second)
                prompt_tokens += usage.get("prompt_tokens", 0)
                completion_tokens += usage.get("completion_tokens", 0)
                second_text = second["choices"][0]["message"].get("content") or ""
                final_candidate = extract_python_code(second_text)
                if final_candidate:
                    generated_code = final_candidate
                trace.append({
                    "event_type": "llm_invocation",
                    "role": "coder",
                    "step": 2,
                    "contains_tool_result": True,
                    "content_preview": second_text[:500],
                })
            except Exception as exc:  # noqa: BLE001
                # ToolFault intentionally fails this second request.  Keep the
                # first candidate for scoring, while marking operational failure.
                reported_error = 1.0
                termination_reason = "agent_exception"
                exception = repr(exc)
                trace.append({
                    "event_type": "error_detection",
                    "role": "coder",
                    "step": 2,
                    "exception": exception[:500],
                })
        except Exception as exc:  # noqa: BLE001
            reported_error = 1.0
            termination_reason = "agent_exception"
            exception = repr(exc)
            trace.append({
                "event_type": "error_detection",
                "role": "coder",
                "step": len(trace),
                "exception": exception[:500],
            })

        total_tokens = prompt_tokens + completion_tokens
        token_status = "measured" if total_tokens else "unavailable"
        return AgentRunResult(
            success=0.0,
            duration_s=round(time.time() - t0, 3),
            reported_error=reported_error,
            retries=0,
            llm_calls=llm_calls,
            tool_calls=tool_calls,
            turns=llm_calls,
            prompt_tokens=prompt_tokens if token_status == "measured" else None,
            completion_tokens=completion_tokens if token_status == "measured" else None,
            total_tokens=total_tokens if token_status == "measured" else None,
            cost_usd=None,
            token_usage_status=token_status,
            cost_status="unavailable",
            generated_code=generated_code,
            execution_trace=trace,
            termination_reason=termination_reason,
            exception=exception,
        )
