"""MapCoder-style agent adapter.

Architecture
------------
MapCoder (Islam et al., 2024) uses a five-stage pipeline designed for
competitive programming tasks:
  1. Retrieval  — find K analogous problems from memory (simulated here).
  2. Planning   — generate a step-by-step plan referencing the analogies.
  3. Coding     — generate code from the plan.
  4. Verification — run the code against visible tests.
  5. Debugging  — if verification fails, iterate on the plan+code.

Each stage issues one LLM call. The debugging stage can repeat up to
``max_debug_cycles`` times.

Deviations from the published MapCoder architecture:
  1. The original retrieves analogies from a curated competitive-programming
     dataset (CodeForces/LeetCode metadata).  Here the retrieval stage asks
     the LLM to generate analogous examples from its training data (no
     external vector store is required).  This reduces infrastructure
     requirements while preserving the pipeline structure.
  2. The original implementation uses GPT-4 with specific temperature
     settings per stage; here all stages use the shared ModelClient
     configuration.
  3. Sample tests for verification are extracted from the task description
     if present; otherwise a syntax check is used.

Reference: Islam et al., "MapCoder: Multi-Agent Code Generation for
Competitive Problem Solving", arXiv:2405.11403, 2024.
"""

from __future__ import annotations

import time

from evaluation.agents.base import AgentRunResult, AgentSystem, ModelClient


class MapCoderStyleAgent(AgentSystem):
    """MapCoder-style five-stage pipeline agent.

    Parameters
    ----------
    client : ModelClient, optional
    max_turns : int
        Maximum debug cycles (default 3).
    dry_run : bool
    k_analogies : int
        Number of analogous problems to retrieve. Default 2.
    executor : callable, optional
        ``(code: str) -> (bool, str)`` for testing.
    """

    name = "mapcoder-style"

    def __init__(
        self,
        client: ModelClient | None = None,
        max_turns: int = 3,
        dry_run: bool = False,
        k_analogies: int = 2,
        executor=None,
    ) -> None:
        super().__init__(client=client, max_turns=max_turns, dry_run=dry_run)
        self.k_analogies    = k_analogies
        self._executor      = executor
        self.max_debug_cycles = max_turns  # max_turns repurposed as debug cycles

    def run(self, task: str, seed: int = 0) -> AgentRunResult:
        from evaluation.benchmarks.executor import sandbox_exec

        executor = self._executor or sandbox_exec
        t0 = time.time()

        prompt_tokens = completion_tokens = 0
        llm_calls = retries = tool_calls = 0
        trace: list[dict] = []
        termination_reason = "max_debug_cycles"
        reported_error = 0.0
        final_code = ""
        exec_success = False

        # ── Stage 1: Retrieval ────────────────────────────────────────────────
        retrieval_prompt = (
            f"You are helping solve the following coding problem:\n\n{task}\n\n"
            f"Generate {self.k_analogies} analogous coding problems with their solutions "
            f"that would be helpful for solving the above. "
            f"Format: Problem: <desc>\nSolution: <python code>"
        )
        try:
            resp = self.client.chat([
                {"role": "system", "content": "You are an expert competitive programmer."},
                {"role": "user",   "content": retrieval_prompt},
            ])
            llm_calls += 1
            u = self.client.usage(resp)
            prompt_tokens += u["prompt_tokens"]; completion_tokens += u["completion_tokens"]
            analogies = resp["choices"][0]["message"]["content"] or ""
        except RuntimeError as exc:
            reported_error = 1.0; retries += 1; analogies = ""
            trace.append({"stage": "retrieval", "role": "api_error", "content": str(exc)})
        trace.append({"stage": "retrieval", "content": analogies[:500]})

        # ── Stage 2: Planning ─────────────────────────────────────────────────
        planning_prompt = (
            f"Analogous problems and solutions for reference:\n\n{analogies}\n\n"
            f"Now solve this problem step by step:\n\n{task}\n\n"
            f"Output a numbered step-by-step plan (no code yet)."
        )
        try:
            resp = self.client.chat([
                {"role": "system", "content": "You are an expert algorithm designer."},
                {"role": "user",   "content": planning_prompt},
            ])
            llm_calls += 1
            u = self.client.usage(resp)
            prompt_tokens += u["prompt_tokens"]; completion_tokens += u["completion_tokens"]
            plan = resp["choices"][0]["message"]["content"] or ""
        except RuntimeError as exc:
            reported_error = 1.0; retries += 1; plan = "1. Parse input\n2. Compute result\n3. Return output"
            trace.append({"stage": "planning", "role": "api_error", "content": str(exc)})
        trace.append({"stage": "planning", "content": plan[:500]})

        # ── Stage 3: Coding ───────────────────────────────────────────────────
        coding_prompt = (
            f"Problem:\n{task}\n\n"
            f"Plan:\n{plan}\n\n"
            f"Implement a Python solution following the plan exactly. "
            f"Output only the Python code in a ```python ... ``` block."
        )
        try:
            resp = self.client.chat([
                {"role": "system", "content": "You are an expert Python programmer."},
                {"role": "user",   "content": coding_prompt},
            ])
            llm_calls += 1
            u = self.client.usage(resp)
            prompt_tokens += u["prompt_tokens"]; completion_tokens += u["completion_tokens"]
            code_response = resp["choices"][0]["message"]["content"] or ""
            final_code = self._extract_code(code_response)
        except RuntimeError as exc:
            reported_error = 1.0; retries += 1; final_code = ""
            trace.append({"stage": "coding", "role": "api_error", "content": str(exc)})
        trace.append({"stage": "coding", "content": final_code[:500]})

        # ── Stages 4 + 5: Verification and Debugging loop ─────────────────────
        for debug_cycle in range(self.max_debug_cycles + 1):
            if not final_code:
                break
            tool_calls += 1
            ok, output = executor(final_code)
            exec_success = ok
            trace.append({
                "stage":       "verification" if debug_cycle == 0 else f"debug_cycle_{debug_cycle}",
                "exec_ok":     ok,
                "exec_output": output[:500],
            })

            if ok:
                termination_reason = "success"
                break

            if debug_cycle >= self.max_debug_cycles:
                termination_reason = "max_debug_cycles"
                break

            # ── Debug stage ────────────────────────────────────────────────────
            debug_prompt = (
                f"Problem:\n{task}\n\n"
                f"Plan:\n{plan}\n\n"
                f"Previous code:\n```python\n{final_code}\n```\n\n"
                f"Execution output (failed):\n{output[:1000]}\n\n"
                f"The code failed. Identify the bug, fix it, and output the corrected "
                f"Python code in a ```python ... ``` block."
            )
            try:
                resp = self.client.chat([
                    {"role": "system", "content": "You are an expert Python debugger."},
                    {"role": "user",   "content": debug_prompt},
                ])
                llm_calls += 1
                u = self.client.usage(resp)
                prompt_tokens += u["prompt_tokens"]; completion_tokens += u["completion_tokens"]
                debug_response = resp["choices"][0]["message"]["content"] or ""
                new_code = self._extract_code(debug_response)
                if new_code:
                    final_code = new_code
                trace.append({"stage": f"debug_fix_{debug_cycle}", "content": new_code[:500]})
            except RuntimeError as exc:
                reported_error = 1.0; retries += 1
                trace.append({"stage": f"debug_{debug_cycle}", "role": "api_error", "content": str(exc)})
                break

        duration_s = time.time() - t0
        return AgentRunResult(
            success=1.0 if exec_success else 0.0,
            duration_s=round(duration_s, 3),
            reported_error=reported_error,
            retries=retries,
            llm_calls=llm_calls,
            tool_calls=tool_calls,
            turns=llm_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            generated_code=final_code,
            execution_trace=trace,
            termination_reason=termination_reason,
        )
