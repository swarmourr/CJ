"""MAD-style (Multi-Agent Debate) agent adapter.

Architecture
------------
MAD (Du et al., 2023) runs N independent agent instances that each produce
a candidate solution, then engage in multiple debate rounds where each agent
sees the other agents' answers and can revise its own.  The final answer is
selected by majority vote.

This adapter implements the published MAD architecture faithfully for coding
tasks:
  - Round 0: each agent independently generates a solution.
  - Rounds 1..debate_rounds: each agent sees all other solutions and
    produces a revised answer.
  - Final: the solution with the most syntactically identical responses is
    selected (majority vote); ties are broken by the first agent's answer.

Deviations from the published MAD architecture:
  1. The original paper uses separate OpenAI client instances per agent.
     Here all agents share one ModelClient (same endpoint/model) but use
     independent message histories — functionally equivalent for a single
     model endpoint.
  2. The original paper uses GPT-3.5-turbo with specific prompting; prompt
     wording here is adapted for general coding tasks rather than NLP tasks.
  3. Judge selection in the original uses a separate LLM call; here majority
     vote is used for determinism and independence from a second model.

Reference: Du et al., "Improving Factuality and Reasoning in Language Models
through Multiagent Debate", arXiv:2305.14325, 2023.
"""

from __future__ import annotations

import time
from collections import Counter

from evaluation.agents.base import AgentRunResult, AgentSystem, ModelClient


class MADStyleAgent(AgentSystem):
    """Multi-Agent Debate style agent.

    Parameters
    ----------
    client : ModelClient, optional
    n_agents : int
        Number of debating agents. Default 3.
    debate_rounds : int
        Number of debate rounds after the initial independent proposals.
        Default 2.
    max_turns : int
        Alias for total rounds (n_agents × (debate_rounds + 1) LLM calls
        maximum). Default 10 kept for interface parity.
    dry_run : bool
    executor : callable, optional
        ``(code: str) -> (bool, str)`` for testing.
    """

    name = "mad-style"

    _SYSTEM = (
        "You are a skilled Python programmer. "
        "Solve the given coding problem. "
        "Always output your complete solution inside a ```python ... ``` block."
    )

    def __init__(
        self,
        client: ModelClient | None = None,
        max_turns: int = 10,
        dry_run: bool = False,
        n_agents: int = 3,
        debate_rounds: int = 2,
        executor=None,
    ) -> None:
        super().__init__(client=client, max_turns=max_turns, dry_run=dry_run)
        self.n_agents      = n_agents
        self.debate_rounds = debate_rounds
        self._executor     = executor

    def run(self, task: str, seed: int = 0) -> AgentRunResult:
        from evaluation.benchmarks.executor import sandbox_exec

        executor = self._executor or sandbox_exec
        t0 = time.time()

        prompt_tokens = completion_tokens = 0
        llm_calls = retries = 0
        trace: list[dict] = []
        termination_reason = "max_turns"
        reported_error = 0.0

        # Each agent maintains its own conversation history
        histories: list[list[dict]] = [
            [
                {"role": "system", "content": self._SYSTEM},
                {"role": "user",   "content": task},
            ]
            for _ in range(self.n_agents)
        ]
        proposals: list[str] = [""] * self.n_agents

        # ── Round 0: independent proposals ────────────────────────────────────
        for i in range(self.n_agents):
            try:
                resp = self.client.chat(histories[i])
                llm_calls += 1
            except RuntimeError as exc:
                reported_error = 1.0
                retries += 1
                proposals[i] = ""
                trace.append({"round": 0, "agent": i, "role": "api_error", "content": str(exc)})
                continue
            u = self.client.usage(resp)
            prompt_tokens     += u["prompt_tokens"]
            completion_tokens += u["completion_tokens"]
            content = resp["choices"][0]["message"]["content"] or ""
            proposals[i] = content
            histories[i].append({"role": "assistant", "content": content})
            trace.append({"round": 0, "agent": i, "role": "proposal", "content": content})

        # ── Debate rounds ─────────────────────────────────────────────────────
        for rnd in range(1, self.debate_rounds + 1):
            for i in range(self.n_agents):
                others = [proposals[j] for j in range(self.n_agents) if j != i and proposals[j]]
                if not others:
                    continue
                other_text = "\n\n".join(
                    f"Agent {j+1} proposed:\n{proposals[j]}"
                    for j in range(self.n_agents) if j != i and proposals[j]
                )
                debate_prompt = (
                    f"Other agents have proposed the following solutions:\n\n"
                    f"{other_text}\n\n"
                    f"Considering these proposals, provide your revised solution "
                    f"to the original task. Output your final answer in a ```python ... ``` block."
                )
                histories[i].append({"role": "user", "content": debate_prompt})
                try:
                    resp = self.client.chat(histories[i])
                    llm_calls += 1
                except RuntimeError as exc:
                    reported_error = 1.0
                    retries += 1
                    trace.append({"round": rnd, "agent": i, "role": "api_error", "content": str(exc)})
                    continue
                u = self.client.usage(resp)
                prompt_tokens     += u["prompt_tokens"]
                completion_tokens += u["completion_tokens"]
                content = resp["choices"][0]["message"]["content"] or ""
                proposals[i] = content
                histories[i].append({"role": "assistant", "content": content})
                trace.append({"round": rnd, "agent": i, "role": "revised", "content": content})

        # ── Majority vote ─────────────────────────────────────────────────────
        # Use extracted code blocks as the voting key
        code_proposals = [self._extract_code(p) for p in proposals if p]
        if not code_proposals:
            duration_s = time.time() - t0
            return AgentRunResult(
                success=0.0,
                duration_s=round(duration_s, 3),
                reported_error=1.0,
                retries=retries,
                llm_calls=llm_calls,
                turns=self.debate_rounds + 1,
                termination_reason="all_agents_failed",
                exception="No proposals received from any agent",
            )

        counts = Counter(code_proposals)
        final_code = counts.most_common(1)[0][0]
        trace.append({"role": "vote", "winner": final_code[:200]})

        # ── Execute winning solution ───────────────────────────────────────────
        exec_success, output = executor(final_code)
        if exec_success:
            termination_reason = "success"
        else:
            termination_reason = "execution_failed"

        duration_s = time.time() - t0
        return AgentRunResult(
            success=1.0 if exec_success else 0.0,
            duration_s=round(duration_s, 3),
            reported_error=reported_error,
            retries=retries,
            llm_calls=llm_calls,
            tool_calls=1,  # one execution call
            turns=self.debate_rounds + 1,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            generated_code=final_code,
            execution_trace=trace,
            termination_reason=termination_reason,
        )
