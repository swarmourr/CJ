"""Common interface for all agent system adapters."""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentRunResult:
    """Result of a single agent run on one benchmark task.

    All adapters must return this dataclass. Fields used as CJ workload
    metrics are: success, duration_s, reported_error, retries.
    """

    # ── Primary outcome ───────────────────────────────────────────
    success: float               # 1.0 = all deterministic tests pass, 0.0 = failed
    duration_s: float            # wall-clock seconds from task start to completion
    reported_error: float        # 1.0 = agent explicitly reported failure, 0.0 = didn't

    # ── Effort metrics ────────────────────────────────────────────
    retries: int = 0             # number of retry attempts the agent made
    llm_calls: int = 0          # total LLM API calls made
    tool_calls: int = 0         # total tool calls (code execution etc.) made
    turns: int = 0              # conversation turns (multi-agent round count)

    # ── Token usage ───────────────────────────────────────────────
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0

    # ── Partial correctness ───────────────────────────────────────
    tests_passed: int = 0       # number of test cases that passed
    tests_total: int = 0        # total test cases attempted

    # ── Artifact / trace ──────────────────────────────────────────
    generated_code: str = ""    # final generated solution
    execution_trace: list[dict] = field(default_factory=list)  # per-turn trace

    # ── Error info ────────────────────────────────────────────────
    termination_reason: str = ""  # "success", "max_turns", "api_error", "timeout", etc.
    exception: str = ""           # exception repr if one occurred

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict suitable for CJ workload metrics and JSONL output."""
        return {
            "success":           self.success,
            "duration_s":        self.duration_s,
            "reported_error":    self.reported_error,
            "retries":           self.retries,
            "llm_calls":         self.llm_calls,
            "tool_calls":        self.tool_calls,
            "turns":             self.turns,
            "prompt_tokens":     self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens":      self.total_tokens,
            "cost_usd":          self.cost_usd,
            "tests_passed":      self.tests_passed,
            "tests_total":       self.tests_total,
            "generated_code":    self.generated_code,
            "execution_trace":   self.execution_trace,
            "termination_reason":self.termination_reason,
            "exception":         self.exception,
        }


class ModelClient:
    """Thin OpenAI-compatible HTTP client driven by environment variables.

    Reads:
      CJ_EVAL_BASE_URL    — endpoint base URL (required for real runs)
      CJ_EVAL_API_KEY     — API key (default: "dummy")
      CJ_EVAL_MODEL       — model id (default: "gpt-4o-mini")
      CJ_EVAL_TEMPERATURE — float temperature (default: 0.0)

    When CJ_EVAL_BASE_URL is not set the client raises RuntimeError unless
    ``dry_run=True`` is passed to the constructor, in which case it returns
    a hard-coded stub response so that CLI dry-runs need no credentials.
    """

    def __init__(self, dry_run: bool = False) -> None:
        # Snapshot construction-time URL only for the startup check; the actual
        # URL used per request is re-read at call time so CJ proxy redirects work.
        _init_url = os.environ.get("CJ_EVAL_BASE_URL", "").rstrip("/")
        self.api_key     = os.environ.get("CJ_EVAL_API_KEY", "dummy")
        self.model       = os.environ.get("CJ_EVAL_MODEL", "gpt-4o-mini")
        self.temperature = float(os.environ.get("CJ_EVAL_TEMPERATURE", "0.0"))
        self.dry_run     = dry_run

        if not _init_url and not dry_run:
            raise RuntimeError(
                "CJ_EVAL_BASE_URL is not set. "
                "Point it to an OpenAI-compatible endpoint, a local Ollama server, "
                "or the evaluation fake model server. "
                "Use --dry-run to run without any model server."
            )

    def _current_base_url(self) -> str:
        """Return the live endpoint URL, re-read on every call.

        CJ LLM faults are configured with ``base_url_env="CJ_EVAL_BASE_URL"``,
        so when a fault starts the proxy URL is written into CJ_EVAL_BASE_URL
        and the original value is saved.  Reading the variable fresh on every
        request means ModelClient automatically routes through the proxy during
        the fault phase, and back to the real API once the fault stops.

        OPENAI_BASE_URL is checked as a fallback for non-CJ usage.

        A ``/v1`` suffix is appended when missing so that both
        ``https://api.openai.com`` and ``https://api.openai.com/v1`` produce
        identical request URLs (``…/v1/chat/completions``).  The CJ proxy
        already writes ``http://127.0.0.1:PORT/v1`` into the env var so this
        normalisation is idempotent for proxy-routed calls.
        """
        url = (
            os.environ.get("CJ_EVAL_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or ""
        ).rstrip("/")
        if url and not url.endswith("/v1"):
            url = url + "/v1"
        return url

    def chat(
        self,
        messages: list[dict],
        *,
        max_tokens: int = 2048,
        stop: list[str] | None = None,
        seed: int | None = None,
    ) -> dict:
        """Send a chat completion request; return the API response dict.

        The endpoint URL is re-read from the environment on every call so that
        CJ proxy redirects (OPENAI_BASE_URL) are picked up automatically.

        Raises:
            RuntimeError: on non-2xx HTTP status.
        """
        if self.dry_run:
            return self._stub_response(messages)

        import json
        import urllib.error
        import urllib.request

        base_url = self._current_base_url()
        payload: dict = {
            "model":       self.model,
            "messages":    messages,
            "temperature": self.temperature,
            "max_tokens":  max_tokens,
        }
        if stop:
            payload["stop"] = stop
        if seed is not None:
            payload["seed"] = seed

        data = json.dumps(payload).encode()
        headers = {
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        for env_name, header_name in (
            ("CJ_RUN_ID", "X-CJ-Run-ID"),
            ("CJ_AGENT_ROLE", "X-CJ-Agent-Role"),
            ("CJ_STEP", "X-CJ-Step"),
        ):
            value = os.environ.get(env_name)
            if value:
                headers[header_name] = value
        req  = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise RuntimeError(
                f"LLM API error {exc.code}: {body}"
            ) from exc

    def complete(self, messages: list[dict], seed: int | None = None, **kwargs) -> str:
        """Return the assistant message content string."""
        resp = self.chat(messages, seed=seed, **kwargs)
        try:
            return resp["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            return ""

    def usage(self, resp: dict) -> dict[str, int]:
        """Extract token usage from a response dict."""
        u = resp.get("usage") or {}
        return {
            "prompt_tokens":     int(u.get("prompt_tokens", 0)),
            "completion_tokens": int(u.get("completion_tokens", 0)),
            "total_tokens":      int(u.get("total_tokens", 0)),
        }

    # ── Internal stub for dry-run ──────────────────────────────────────────────

    def _stub_response(self, messages: list[dict]) -> dict:
        content = (
            "```python\n"
            "def solution(*args, **kwargs):\n"
            "    # dry-run stub — replace with a real model\n"
            "    return None\n"
            "```"
        )
        return {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        }


class AgentSystem(ABC):
    """Abstract base class for all agent system adapters.

    Subclasses must implement :meth:`run`.  The ``name`` class attribute
    determines the system identifier used in output records and must be
    a string like ``"autogen-style"``.

    Real-framework adapters (AutoGen, LangGraph, CrewAI) set ``client=None``
    and use SDK-native clients created inside :meth:`run`.  They set the class
    attribute ``uses_model_config = True`` so the experiment runner knows to
    pass a :class:`~evaluation.model_config.ModelConfig` instead of a
    :class:`ModelClient`.
    """

    name: str = "base"
    uses_model_config: bool = False  # True for real-framework adapters

    def __init__(
        self,
        client: ModelClient | None = None,
        max_turns: int = 10,
        dry_run: bool = False,
    ) -> None:
        self.client   = client or ModelClient(dry_run=dry_run)
        self.max_turns = max_turns
        self.dry_run  = dry_run

    @property
    def model_name(self) -> str:
        """Return the model identifier for this agent.

        Style adapters: reads from the underlying :class:`ModelClient`.
        Real-framework adapters: reads the ``_model_name`` attribute set in
        their own ``__init__``.
        """
        if self.client is not None:
            return self.client.model
        return getattr(self, "_model_name", "unknown")

    @abstractmethod
    def run(self, task: str, seed: int = 0) -> AgentRunResult:
        """Execute the agent on *task* and return a populated AgentRunResult.

        Parameters
        ----------
        task : str
            The benchmark task description / problem statement.
        seed : int
            Random seed for any stochastic decisions (debate order, etc.).

        Returns
        -------
        AgentRunResult
        """

    # ── Shared utility ─────────────────────────────────────────────────────────

    def _extract_code(self, text: str) -> str:
        """Extract the first Python code block from a markdown-fenced response."""
        import re
        # Try ```python ... ``` first, then plain ``` ... ```
        m = re.search(r"```(?:python)?\n(.*?)```", text, re.DOTALL)
        if m:
            return m.group(1).strip()
        # Fallback: return whole text if it looks like code
        if "def " in text or "return " in text:
            return text.strip()
        return text.strip()
