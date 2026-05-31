"""LLM agent and MCP fault implementations.

These faults intercept HTTP traffic between an LLM agent and its API
backend (OpenAI, Anthropic, or any OpenAI-compatible endpoint) by
starting a lightweight local proxy.  The proxy is a stdlib-only Python
script bundled inside the package — no extra dependencies.

How it works
------------
1. ``start()`` launches the bundled proxy as a background subprocess on
   the machine where the *agent* is running.
2. The proxy listens on ``localhost:<port>`` and forwards requests to the
   real API while injecting the chosen fault.
3. The environment variable ``base_url_env`` (default ``OPENAI_BASE_URL``)
   is set so that the agent's LLM client automatically routes its calls
   through the proxy.
4. ``stop()`` terminates the proxy process and restores the original
   environment variable.

All fault classes work with :class:`~chaos_jungle.targets.local.LocalTarget`
(the agent and proxy share the same machine).  For remote deployments see
the SSH guide.

Available faults
----------------
.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Class
     - Behaviour
   * - :class:`LLMLatency`
     - Add artificial delay to every API call
   * - :class:`LLMRateLimit`
     - Reject calls with HTTP 429 after N successful requests
   * - :class:`LLMTimeout`
     - Hang every connection for *timeout_s* seconds then return 504
   * - :class:`LLMResponseCorrupt`
     - Forward calls but mangle the response (truncate / empty / invalid JSON)
   * - :class:`LLMUnavailable`
     - Always return HTTP 503 — simulate a completely down endpoint
   * - :class:`ToolFault`
     - Inject errors into tool-call requests (messages with role=tool)
   * - :class:`LLMHallucination`
     - Replace the assistant's answer with injected wrong text
   * - :class:`LLMStreamInterrupt`
     - Cut a streaming SSE response after N data events
   * - :class:`LLMTokenStarvation`
     - Rewrite max_tokens to a tiny value, forcing truncated responses
   * - :class:`MCPFault`
     - Inject failures into MCP server calls (tool/resource JSON-RPC)
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from importlib.resources import as_file, files
from typing import TYPE_CHECKING

from chaos_jungle.faults.base import Fault

if TYPE_CHECKING:
    from chaos_jungle.targets.base import Target

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_DEFAULT_ENV = "OPENAI_BASE_URL"
_DEFAULT_PORT = 18_000
_DEFAULT_UPSTREAM = "https://api.openai.com"


def _proxy_script_path() -> str:
    """Return the filesystem path to llm_proxy.py from the bundled package data."""
    pkg = files("chaos_jungle.scripts.llm_proxy")
    with as_file(pkg / "llm_proxy.py") as p:
        return str(p)


class _LLMProxyFault(Fault):
    """Internal base class that manages proxy lifecycle."""

    # Subclasses set these in __init__
    _fault_name: str = ""
    _extra_args: list[str]

    def __init__(
        self,
        port: int = _DEFAULT_PORT,
        upstream: str = _DEFAULT_UPSTREAM,
        base_url_env: str = _DEFAULT_ENV,
    ) -> None:
        self.port = port
        self.upstream = upstream
        self.base_url_env = base_url_env
        self._proc: subprocess.Popen | None = None
        self._saved_env: str | None = None
        self._extra_args = []

    # ------------------------------------------------------------------
    # Fault lifecycle
    # ------------------------------------------------------------------

    def start(self, target: "Target") -> None:  # noqa: ARG002  (target unused for local proxy)
        script = _proxy_script_path()
        cmd = [
            sys.executable,
            script,
            "--port", str(self.port),
            "--upstream", self.upstream,
            "--fault", self._fault_name,
        ] + self._extra_args

        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        # Give the proxy a moment to bind its port
        time.sleep(0.4)
        if self._proc.poll() is not None:
            out = self._proc.stdout.read().decode(errors="replace") if self._proc.stdout else ""
            raise RuntimeError(
                f"LLM proxy failed to start (fault={self._fault_name}).\n"
                f"Output: {out}"
            )

        # Redirect the LLM client to the proxy
        self._saved_env = os.environ.get(self.base_url_env)
        os.environ[self.base_url_env] = f"http://127.0.0.1:{self.port}"

    def stop(self, target: "Target") -> None:  # noqa: ARG002
        # Restore original env var
        if self._saved_env is None:
            os.environ.pop(self.base_url_env, None)
        else:
            os.environ[self.base_url_env] = self._saved_env
        self._saved_env = None

        # Kill proxy process
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def revert(self, target: "Target") -> None:  # noqa: ARG002
        pass  # stateless — stop() is sufficient

    def _parameters(self) -> dict:
        return {
            "fault": self._fault_name,
            "port": self.port,
            "upstream": self.upstream,
            "base_url_env": self.base_url_env,
        }


# ---------------------------------------------------------------------------
# Public fault classes
# ---------------------------------------------------------------------------


class LLMLatency(_LLMProxyFault):
    """Inject artificial latency into every LLM API call.

    The proxy adds ``delay_s`` seconds of sleep before forwarding each
    request to the real API.  Use this to test how your agent handles
    slow model responses — retry logic, timeout budgets, UX degradation.

    Parameters
    ----------
    delay_s : float
        Seconds of extra latency per request. Default ``2.0``.
    port : int, optional
        Local proxy port. Default ``18000``.
    upstream : str, optional
        Real LLM API base URL. Default ``"https://api.openai.com"``.
    base_url_env : str, optional
        Environment variable that your LLM client reads for the base URL.
        Default ``"OPENAI_BASE_URL"``.

    Examples
    --------
    >>> from chaos_jungle import Scenario, ChaosRunner
    >>> from chaos_jungle.faults.llm import LLMLatency
    >>> from chaos_jungle.targets import LocalTarget
    >>> fault = LLMLatency(delay_s=3.0)
    >>> runner = ChaosRunner(Scenario("slow-llm", [fault]), LocalTarget())
    >>> runner.start()
    >>> # run your agent here
    >>> runner.stop()
    """

    _fault_name = "latency"

    def __init__(
        self,
        delay_s: float = 2.0,
        port: int = _DEFAULT_PORT,
        upstream: str = _DEFAULT_UPSTREAM,
        base_url_env: str = _DEFAULT_ENV,
    ) -> None:
        if delay_s < 0:
            raise ValueError(f"LLMLatency 'delay_s' must be >= 0, got {delay_s}.")
        super().__init__(port=port, upstream=upstream, base_url_env=base_url_env)
        self.delay_s = delay_s
        self._extra_args = ["--latency-s", str(delay_s)]

    def _parameters(self) -> dict:
        return {**super()._parameters(), "delay_s": self.delay_s}


class LLMRateLimit(_LLMProxyFault):
    """Simulate API rate limiting — return HTTP 429 after N requests.

    The first ``n`` requests are forwarded normally; every subsequent
    request receives a 429 ``rate_limit_exceeded`` response.  Use this
    to verify that your agent backs off, respects ``Retry-After``, or
    degrades gracefully.

    Parameters
    ----------
    n : int
        Number of successful requests allowed before rate-limiting kicks
        in. Default ``5``.
    port : int, optional
        Local proxy port. Default ``18000``.
    upstream : str, optional
        Real LLM API base URL. Default ``"https://api.openai.com"``.
    base_url_env : str, optional
        Environment variable for the LLM client base URL.
        Default ``"OPENAI_BASE_URL"``.

    Examples
    --------
    >>> fault = LLMRateLimit(n=3)
    """

    _fault_name = "rate_limit"

    def __init__(
        self,
        n: int = 5,
        port: int = _DEFAULT_PORT,
        upstream: str = _DEFAULT_UPSTREAM,
        base_url_env: str = _DEFAULT_ENV,
    ) -> None:
        if n < 0:
            raise ValueError(f"LLMRateLimit 'n' must be >= 0, got {n}.")
        super().__init__(port=port, upstream=upstream, base_url_env=base_url_env)
        self.n = n
        self._extra_args = ["--rate-limit-n", str(n)]

    def _parameters(self) -> dict:
        return {**super()._parameters(), "n": self.n}


class LLMTimeout(_LLMProxyFault):
    """Simulate a hanging LLM API — hold the connection for timeout_s seconds.

    Each request is intentionally delayed for ``timeout_s`` seconds and
    then answered with HTTP 504.  No request is ever forwarded to the real
    API.  Use this to test agent timeout handling, task cancellation, and
    circuit-breaker patterns.

    Parameters
    ----------
    timeout_s : float
        Seconds to hold each connection before returning 504. Default ``30.0``.
    port : int, optional
        Local proxy port. Default ``18000``.
    upstream : str, optional
        Real LLM API base URL (unused — no requests are forwarded).
        Default ``"https://api.openai.com"``.
    base_url_env : str, optional
        Environment variable for the LLM client base URL.
        Default ``"OPENAI_BASE_URL"``.

    Examples
    --------
    >>> fault = LLMTimeout(timeout_s=10.0)
    """

    _fault_name = "timeout"

    def __init__(
        self,
        timeout_s: float = 30.0,
        port: int = _DEFAULT_PORT,
        upstream: str = _DEFAULT_UPSTREAM,
        base_url_env: str = _DEFAULT_ENV,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError(f"LLMTimeout 'timeout_s' must be > 0, got {timeout_s}.")
        super().__init__(port=port, upstream=upstream, base_url_env=base_url_env)
        self.timeout_s = timeout_s
        self._extra_args = ["--timeout-s", str(timeout_s)]

    def _parameters(self) -> dict:
        return {**super()._parameters(), "timeout_s": self.timeout_s}


_CORRUPT_MODES = ("truncate", "empty", "invalid_json")


class LLMResponseCorrupt(_LLMProxyFault):
    """Forward LLM calls but return a corrupted response.

    The real API call is made normally; the proxy then mangles the response
    before returning it to the agent.  Use this to test JSON-parse error
    handling, partial-response recovery, and retry logic.

    Parameters
    ----------
    mode : ``"truncate"`` | ``"empty"`` | ``"invalid_json"``
        How to corrupt the response:

        ``"truncate"``
            Cut the body to half its original length (partial JSON).
        ``"empty"``
            Replace the body with ``{}``.
        ``"invalid_json"``
            Replace the body with a non-JSON string.

        Default ``"truncate"``.
    port : int, optional
        Local proxy port. Default ``18000``.
    upstream : str, optional
        Real LLM API base URL. Default ``"https://api.openai.com"``.
    base_url_env : str, optional
        Environment variable for the LLM client base URL.
        Default ``"OPENAI_BASE_URL"``.

    Examples
    --------
    >>> fault = LLMResponseCorrupt(mode="invalid_json")
    """

    _fault_name = "corrupt"

    def __init__(
        self,
        mode: str = "truncate",
        port: int = _DEFAULT_PORT,
        upstream: str = _DEFAULT_UPSTREAM,
        base_url_env: str = _DEFAULT_ENV,
    ) -> None:
        if mode not in _CORRUPT_MODES:
            raise ValueError(
                f"LLMResponseCorrupt 'mode' must be one of {_CORRUPT_MODES}, got {mode!r}."
            )
        super().__init__(port=port, upstream=upstream, base_url_env=base_url_env)
        self.mode = mode
        self._extra_args = ["--corrupt-mode", mode]

    def _parameters(self) -> dict:
        return {**super()._parameters(), "mode": self.mode}


class LLMUnavailable(_LLMProxyFault):
    """Make the LLM API completely unavailable — always return HTTP 503.

    Every request receives a 503 ``service_unavailable`` response without
    being forwarded.  Use this to test full-service-outage handling: graceful
    degradation, fallback models, user-facing error messages.

    Parameters
    ----------
    port : int, optional
        Local proxy port. Default ``18000``.
    upstream : str, optional
        Real LLM API base URL (unused). Default ``"https://api.openai.com"``.
    base_url_env : str, optional
        Environment variable for the LLM client base URL.
        Default ``"OPENAI_BASE_URL"``.

    Examples
    --------
    >>> fault = LLMUnavailable()
    """

    _fault_name = "unavailable"

    def __init__(
        self,
        port: int = _DEFAULT_PORT,
        upstream: str = _DEFAULT_UPSTREAM,
        base_url_env: str = _DEFAULT_ENV,
    ) -> None:
        super().__init__(port=port, upstream=upstream, base_url_env=base_url_env)


# ---------------------------------------------------------------------------
# Extended faults — tool calls, hallucination, streaming, token budget, MCP
# ---------------------------------------------------------------------------


class ToolFault(_LLMProxyFault):
    """Inject failures into tool-call messages sent to the LLM API.

    When the agent submits a request that contains a ``role: tool`` message
    (i.e. it is returning a tool execution result to the model), the proxy
    intercepts it and returns an error instead of forwarding it.

    This simulates a tool that crashes, times out, or returns garbage — and
    tests whether the agent handles tool failures gracefully.

    Parameters
    ----------
    tool_name : str, optional
        Only intercept tool calls with this exact name.  If empty (default),
        *all* tool calls are intercepted.
    port : int, optional
        Local proxy port. Default ``18000``.
    upstream : str, optional
        Real LLM API base URL. Default ``"https://api.openai.com"``.
    base_url_env : str, optional
        Environment variable for the LLM client base URL.
        Default ``"OPENAI_BASE_URL"``.

    Examples
    --------
    >>> # Fail every tool call
    >>> fault = ToolFault()

    >>> # Fail only the "search" tool
    >>> fault = ToolFault(tool_name="search")
    """

    _fault_name = "tool_fault"

    def __init__(
        self,
        tool_name: str = "",
        port: int = _DEFAULT_PORT,
        upstream: str = _DEFAULT_UPSTREAM,
        base_url_env: str = _DEFAULT_ENV,
    ) -> None:
        super().__init__(port=port, upstream=upstream, base_url_env=base_url_env)
        self.tool_name = tool_name
        self._extra_args = ["--tool-name", tool_name] if tool_name else []

    def _parameters(self) -> dict:
        return {**super()._parameters(), "tool_name": self.tool_name}


class LLMHallucination(_LLMProxyFault):
    """Replace the assistant's response with injected wrong text.

    The request is forwarded normally to the real LLM API.  The proxy
    then replaces ``choices[0].message.content`` in the response with
    ``inject_text`` before returning it to the agent.

    Use this to test how your agent or downstream system handles factually
    wrong, misleading, or nonsensical model outputs — without waiting for
    the real model to hallucinate naturally.

    Parameters
    ----------
    inject_text : str
        The text to inject as the assistant's response.
    port : int, optional
        Local proxy port. Default ``18000``.
    upstream : str, optional
        Real LLM API base URL. Default ``"https://api.openai.com"``.
    base_url_env : str, optional
        Environment variable for the LLM client base URL.
        Default ``"OPENAI_BASE_URL"``.

    Examples
    --------
    >>> fault = LLMHallucination("The capital of France is Berlin.")
    >>> fault = LLMHallucination("I don't know anything about that topic.")
    """

    _fault_name = "hallucinate"

    def __init__(
        self,
        inject_text: str = "WRONG ANSWER (injected by chaos-jungle)",
        port: int = _DEFAULT_PORT,
        upstream: str = _DEFAULT_UPSTREAM,
        base_url_env: str = _DEFAULT_ENV,
    ) -> None:
        if not inject_text or not inject_text.strip():
            raise ValueError("LLMHallucination 'inject_text' must be a non-empty string.")
        super().__init__(port=port, upstream=upstream, base_url_env=base_url_env)
        self.inject_text = inject_text
        self._extra_args = ["--hallucination-text", inject_text]

    def _parameters(self) -> dict:
        return {**super()._parameters(), "inject_text": self.inject_text}


class LLMStreamInterrupt(_LLMProxyFault):
    """Cut a streaming SSE response after N data events.

    When the agent sends a request with ``"stream": true``, the proxy
    forwards it to the real API and pipes the SSE stream back — but
    abruptly closes the connection after ``interrupt_after`` data events.

    This simulates a network drop mid-stream or a gateway that cuts long
    responses.  Use it to test partial-response handling, streaming error
    recovery, and incomplete tool-call detection.

    Parameters
    ----------
    interrupt_after : int
        Number of SSE ``data:`` events to forward before cutting the stream.
        Default ``3``.
    port : int, optional
        Local proxy port. Default ``18000``.
    upstream : str, optional
        Real LLM API base URL. Default ``"https://api.openai.com"``.
    base_url_env : str, optional
        Environment variable for the LLM client base URL.
        Default ``"OPENAI_BASE_URL"``.

    Notes
    -----
    Only affects requests that include ``"stream": true`` in the body.
    Non-streaming requests are forwarded unmodified.

    Examples
    --------
    >>> fault = LLMStreamInterrupt(interrupt_after=2)
    """

    _fault_name = "stream_interrupt"

    def __init__(
        self,
        interrupt_after: int = 3,
        port: int = _DEFAULT_PORT,
        upstream: str = _DEFAULT_UPSTREAM,
        base_url_env: str = _DEFAULT_ENV,
    ) -> None:
        if interrupt_after < 1:
            raise ValueError(
                f"LLMStreamInterrupt 'interrupt_after' must be >= 1, got {interrupt_after}."
            )
        super().__init__(port=port, upstream=upstream, base_url_env=base_url_env)
        self.interrupt_after = interrupt_after
        self._extra_args = ["--stream-interrupt-after", str(interrupt_after)]

    def _parameters(self) -> dict:
        return {**super()._parameters(), "interrupt_after": self.interrupt_after}


class LLMTokenStarvation(_LLMProxyFault):
    """Force the model to return a truncated response by capping max_tokens.

    Every request is rewritten to set ``max_tokens`` to ``max_tokens``
    before being forwarded.  The model will hit the limit and return a
    partial response with ``finish_reason: "length"``.

    Use this to test how the agent handles incomplete answers, cut-off
    tool calls, and context-window pressure.

    Parameters
    ----------
    max_tokens : int
        The ``max_tokens`` value to inject into every request. Default ``5``.
    port : int, optional
        Local proxy port. Default ``18000``.
    upstream : str, optional
        Real LLM API base URL. Default ``"https://api.openai.com"``.
    base_url_env : str, optional
        Environment variable for the LLM client base URL.
        Default ``"OPENAI_BASE_URL"``.

    Examples
    --------
    >>> fault = LLMTokenStarvation(max_tokens=10)
    """

    _fault_name = "token_starve"

    def __init__(
        self,
        max_tokens: int = 5,
        port: int = _DEFAULT_PORT,
        upstream: str = _DEFAULT_UPSTREAM,
        base_url_env: str = _DEFAULT_ENV,
    ) -> None:
        if max_tokens < 1:
            raise ValueError(
                f"LLMTokenStarvation 'max_tokens' must be >= 1, got {max_tokens}."
            )
        super().__init__(port=port, upstream=upstream, base_url_env=base_url_env)
        self.max_tokens = max_tokens
        self._extra_args = ["--token-starve-max", str(max_tokens)]

    def _parameters(self) -> dict:
        return {**super()._parameters(), "max_tokens": self.max_tokens}


_MCP_FAULT_MODES = ("tool_error", "unavailable", "timeout")


class MCPFault(_LLMProxyFault):
    """Inject failures into MCP (Model Context Protocol) server calls.

    MCP servers expose tools and resources to LLM agents over HTTP using
    JSON-RPC.  This fault starts a proxy that sits between the agent and
    the MCP server and injects failures into every call.

    Unlike the LLM faults, ``upstream`` should point to your MCP server
    (e.g. ``http://localhost:3000``) and ``base_url_env`` should be the
    environment variable your agent reads for the MCP server URL.

    Parameters
    ----------
    mode : ``"tool_error"`` | ``"unavailable"`` | ``"timeout"``
        How to fail MCP calls:

        ``"tool_error"``
            Return a JSON-RPC error response (code -32000) for every call.
        ``"unavailable"``
            Return HTTP 503 for every call.
        ``"timeout"``
            Hang every call for ``timeout_s`` seconds then return 504.

    timeout_s : float, optional
        Seconds to hang (only used when ``mode="timeout"``). Default ``10.0``.
    port : int, optional
        Local proxy port. Default ``18100``.
    upstream : str, optional
        Real MCP server base URL. Default ``"http://localhost:3000"``.
    base_url_env : str, optional
        Environment variable that the agent reads for the MCP server URL.
        Default ``"MCP_SERVER_URL"``.

    Examples
    --------
    >>> # Make every MCP tool call fail with a JSON-RPC error
    >>> fault = MCPFault(mode="tool_error")

    >>> # Make the MCP server completely unavailable
    >>> fault = MCPFault(mode="unavailable")

    >>> # Hang every MCP call for 10 s
    >>> fault = MCPFault(mode="timeout", timeout_s=10.0)

    Notes
    -----
    Also intercepts MCP-over-SSE (server-sent events) and MCP-over-HTTP
    streams used by modern MCP clients.
    """

    def __init__(
        self,
        mode: str = "tool_error",
        timeout_s: float = 10.0,
        port: int = 18100,
        upstream: str = "http://localhost:3000",
        base_url_env: str = "MCP_SERVER_URL",
    ) -> None:
        if mode not in _MCP_FAULT_MODES:
            raise ValueError(
                f"MCPFault 'mode' must be one of {_MCP_FAULT_MODES}, got {mode!r}."
            )
        if timeout_s <= 0:
            raise ValueError(f"MCPFault 'timeout_s' must be > 0, got {timeout_s}.")

        # Map to proxy fault names
        proxy_fault = {
            "tool_error":   "mcp_tool_error",
            "unavailable":  "mcp_unavailable",
            "timeout":      "mcp_timeout",
        }[mode]

        super().__init__(port=port, upstream=upstream, base_url_env=base_url_env)
        self.mode = mode
        self.timeout_s = timeout_s
        self._fault_name = proxy_fault
        self._extra_args = ["--timeout-s", str(timeout_s)] if mode == "timeout" else []

    def _parameters(self) -> dict:
        return {**super()._parameters(), "mode": self.mode, "timeout_s": self.timeout_s}
