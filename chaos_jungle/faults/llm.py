"""LLM agent fault implementations.

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
