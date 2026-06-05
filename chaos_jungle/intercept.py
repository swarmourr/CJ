"""
HTTP transport-level fault injection — provider-agnostic LLM chaos.

Patches ``httpx`` and ``requests`` at the transport layer so faults apply to
**every** LLM SDK call without any per-SDK integration code.

Works out of the box with:

* OpenAI Python SDK (uses httpx)
* Anthropic SDK (uses httpx)
* LiteLLM (uses httpx / requests)
* LangChain / LangGraph (wraps OpenAI / Anthropic)
* LlamaIndex (wraps OpenAI / Anthropic)
* Any other library built on httpx or requests

Usage::

    from chaos_jungle.intercept import inject, Latency, RateLimit, Unavailable

    with inject(Latency(3.0)):
        openai_client.chat.completions.create(...)   # affected
        anthropic_client.messages.create(...)         # affected
        litellm.completion(...)                        # affected

    # Multiple faults at once
    with inject(Latency(1.0), RateLimit(after_n=3)):
        agent.run("Summarise this document")

    # Restrict to specific hosts
    with inject(Latency(2.0), urls=["api.openai.com"]):
        ...
"""

from __future__ import annotations

import json
import random
import time
import threading
from contextlib import contextmanager
from typing import Any, Callable, Generator

try:
    import httpx as _httpx
    _HAS_HTTPX = True
except ImportError:  # pragma: no cover
    _httpx = None      # type: ignore[assignment]
    _HAS_HTTPX = False

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:  # pragma: no cover
    _requests = None   # type: ignore[assignment]
    _HAS_REQUESTS = False


# ── Default LLM API host patterns ─────────────────────────────────────────────

DEFAULT_LLM_HOSTS: list[str] = [
    "api.openai.com",
    "api.anthropic.com",
    "api.cohere.ai",
    "api.mistral.ai",
    "generativelanguage.googleapis.com",  # Google Gemini
    "api.together.xyz",
    "api.groq.com",
    "openrouter.ai",
    "api.perplexity.ai",
    "127.0.0.1",   # local models (Ollama, LM Studio, vLLM …)
    "localhost",
]


def _matches(url: str, patterns: list[str]) -> bool:
    u = url.lower()
    return any(p in u for p in patterns)


# ── Response helpers ──────────────────────────────────────────────────────────

def _httpx_response(status: int, body: bytes) -> "_httpx.Response":
    return _httpx.Response(
        status,
        headers={"Content-Type": "application/json"},
        content=body,
    )


def _requests_response(status: int, body: bytes) -> "_requests.Response":
    r = _requests.Response()
    r.status_code = status
    r.headers["Content-Type"] = "application/json"
    r._content = body  # type: ignore[attr-defined]
    return r


def _mock_response(status: int, payload: dict, response: Any) -> Any:
    body = json.dumps(payload).encode()
    if _HAS_HTTPX and isinstance(response, _httpx.Response):
        return _httpx_response(status, body)
    if _HAS_REQUESTS and isinstance(response, _requests.Response):
        return _requests_response(status, body)
    return response


# ── Behavior base class ───────────────────────────────────────────────────────

class Behavior:
    """
    Base class for intercept-level fault behaviors.

    Subclass and override ``before`` and/or ``after`` to define custom faults.
    """

    def before(self, url: str) -> None:
        """
        Called before the request is sent.

        Raise an exception here to simulate a network-level error (e.g. timeout).
        Sleep here to add latency.
        """

    def after(self, url: str, response: Any) -> Any:
        """
        Called after the real response is received.

        Return a replacement response to simulate error codes or corrupt payloads.
        Return ``response`` unchanged to pass through.
        """
        return response


# ── Built-in behaviors ────────────────────────────────────────────────────────

class Latency(Behavior):
    """
    Add fixed latency before every matching request.

    Parameters
    ----------
    seconds:
        How long to sleep before forwarding the request.

    Example
    -------
    ::

        with inject(Latency(3.0)):
            client.chat.completions.create(...)
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def before(self, url: str) -> None:
        time.sleep(self.seconds)

    def __repr__(self) -> str:
        return f"Latency({self.seconds}s)"


class Jitter(Behavior):
    """
    Add random latency uniformly sampled between ``min_s`` and ``max_s``.

    Example
    -------
    ::

        with inject(Jitter(0.5, 4.0)):
            client.chat.completions.create(...)
    """

    def __init__(self, min_s: float, max_s: float) -> None:
        self.min_s = min_s
        self.max_s = max_s

    def before(self, url: str) -> None:
        time.sleep(random.uniform(self.min_s, self.max_s))

    def __repr__(self) -> str:
        return f"Jitter({self.min_s}-{self.max_s}s)"


class RateLimit(Behavior):
    """
    Return HTTP 429 after the first ``after_n`` requests succeed.

    Parameters
    ----------
    after_n:
        Number of requests to let through before starting to return 429.
        Default ``0`` means every request gets a 429 immediately.
    retry_after_s:
        Value for the ``Retry-After`` response header (seconds).

    Example
    -------
    ::

        with inject(RateLimit(after_n=5)):
            # first 5 calls succeed; from the 6th onward → 429
            for _ in range(10):
                client.chat.completions.create(...)
    """

    def __init__(self, after_n: int = 0, retry_after_s: int = 60) -> None:
        self.after_n = after_n
        self.retry_after_s = retry_after_s
        self._count = 0
        self._lock = threading.Lock()

    def after(self, url: str, response: Any) -> Any:
        with self._lock:
            self._count += 1
            limited = self._count > self.after_n

        if not limited:
            return response

        body = json.dumps({
            "error": {
                "message": "Rate limit exceeded — injected by chaos-jungle",
                "type": "rate_limit_error",
                "code": "rate_limit_exceeded",
            }
        }).encode()

        if _HAS_HTTPX and isinstance(response, _httpx.Response):
            return _httpx.Response(
                429,
                headers={
                    "Content-Type": "application/json",
                    "Retry-After": str(self.retry_after_s),
                },
                content=body,
            )
        if _HAS_REQUESTS and isinstance(response, _requests.Response):
            r = _requests_response(429, body)
            r.headers["Retry-After"] = str(self.retry_after_s)
            return r
        return response

    def __repr__(self) -> str:
        return f"RateLimit(after_n={self.after_n})"


class Unavailable(Behavior):
    """
    Return HTTP 503 Service Unavailable for every matching request.

    Simulates a total LLM API outage.

    Example
    -------
    ::

        with inject(Unavailable()):
            result = agent.run("question")   # should raise or retry
    """

    def after(self, url: str, response: Any) -> Any:
        payload = {
            "error": {
                "message": "Service temporarily unavailable — injected by chaos-jungle",
                "type": "server_error",
            }
        }
        return _mock_response(503, payload, response)

    def __repr__(self) -> str:
        return "Unavailable()"


class Timeout(Behavior):
    """
    Raise a timeout exception instead of sending the request.

    Raises ``httpx.TimeoutException`` for httpx-based SDKs,
    ``requests.exceptions.Timeout`` for requests-based SDKs.

    Example
    -------
    ::

        with inject(Timeout()):
            client.chat.completions.create(...)  # raises TimeoutError
    """

    def before(self, url: str) -> None:
        if _HAS_HTTPX:
            raise _httpx.TimeoutException(
                f"chaos-jungle: injected timeout for {url}"
            )
        if _HAS_REQUESTS:
            raise _requests.exceptions.Timeout(
                f"chaos-jungle: injected timeout for {url}"
            )
        raise TimeoutError(f"chaos-jungle: injected timeout for {url}")

    def __repr__(self) -> str:
        return "Timeout()"


class CorruptResponse(Behavior):
    """
    Replace the response content with a garbled payload.

    The response keeps its HTTP 200 status but the body contains
    corrupted JSON so the SDK's response parser will fail or return
    nonsense to the caller.

    Example
    -------
    ::

        with inject(CorruptResponse()):
            reply = client.chat.completions.create(...)
            # reply.choices[0].message.content == "<<CORRUPTED>>"
    """

    CORRUPT_BODY = json.dumps({
        "id": "chaos-corrupt",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "<<CORRUPTED>>"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }).encode()

    def after(self, url: str, response: Any) -> Any:
        if _HAS_HTTPX and isinstance(response, _httpx.Response):
            return _httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=self.CORRUPT_BODY,
            )
        if _HAS_REQUESTS and isinstance(response, _requests.Response):
            r = _requests.Response()
            r.status_code = 200
            r._content = self.CORRUPT_BODY  # type: ignore[attr-defined]
            return r
        return response

    def __repr__(self) -> str:
        return "CorruptResponse()"


# ── Thread-local intercept stack ──────────────────────────────────────────────

class _Stack:
    """Thread-local stack of active (behaviors, patterns) layers."""

    _local: threading.local = threading.local()

    @classmethod
    def _layers(cls) -> list[tuple[list[Behavior], list[str]]]:
        if not hasattr(cls._local, "layers"):
            cls._local.layers = []
        return cls._local.layers

    @classmethod
    def push(cls, behaviors: list[Behavior], patterns: list[str]) -> None:
        cls._layers().append((behaviors, patterns))

    @classmethod
    def pop(cls) -> None:
        cls._layers().pop()

    @classmethod
    def matching(cls, url: str) -> list[list[Behavior]]:
        """Return all behavior layers whose URL patterns match *url*."""
        return [bs for bs, ps in cls._layers() if _matches(url, ps)]


# ── Core apply logic ──────────────────────────────────────────────────────────

def _run(url: str, call: Callable[[], Any]) -> Any:
    layers = _Stack.matching(url)
    if not layers:
        return call()

    # before hooks — any can raise to abort the request
    for layer in layers:
        for b in layer:
            b.before(url)

    response = call()

    # after hooks — can replace the response
    for layer in layers:
        for b in layer:
            response = b.after(url, response)

    return response


async def _run_async(url: str, coro_factory: Callable) -> Any:
    layers = _Stack.matching(url)
    if not layers:
        return await coro_factory()

    for layer in layers:
        for b in layer:
            b.before(url)

    response = await coro_factory()

    for layer in layers:
        for b in layer:
            response = b.after(url, response)

    return response


# ── Transport patches ─────────────────────────────────────────────────────────

_orig_httpx_send: Callable | None = None
_orig_httpx_asend: Callable | None = None
_orig_req_send: Callable | None = None
_patch_depth = 0
_patch_lock = threading.Lock()


def _httpx_send(self: Any, request: Any, **kw: Any) -> Any:
    return _run(str(request.url), lambda: _orig_httpx_send(self, request, **kw))  # type: ignore[misc]


async def _httpx_asend(self: Any, request: Any, **kw: Any) -> Any:
    return await _run_async(str(request.url), lambda: _orig_httpx_asend(self, request, **kw))  # type: ignore[misc]


def _req_send(self: Any, request: Any, **kw: Any) -> Any:
    return _run(str(request.url), lambda: _orig_req_send(self, request, **kw))  # type: ignore[misc]


def _patch() -> None:
    global _orig_httpx_send, _orig_httpx_asend, _orig_req_send, _patch_depth
    with _patch_lock:
        _patch_depth += 1
        if _patch_depth > 1:
            return
        if _HAS_HTTPX:
            _orig_httpx_send = _httpx.Client.send
            _orig_httpx_asend = _httpx.AsyncClient.send
            _httpx.Client.send = _httpx_send          # type: ignore[method-assign]
            _httpx.AsyncClient.send = _httpx_asend    # type: ignore[method-assign]
        if _HAS_REQUESTS:
            _orig_req_send = _requests.Session.send
            _requests.Session.send = _req_send        # type: ignore[method-assign]


def _unpatch() -> None:
    global _orig_httpx_send, _orig_httpx_asend, _orig_req_send, _patch_depth
    with _patch_lock:
        _patch_depth -= 1
        if _patch_depth > 0:
            return
        if _HAS_HTTPX and _orig_httpx_send:
            _httpx.Client.send = _orig_httpx_send          # type: ignore[method-assign]
            _httpx.AsyncClient.send = _orig_httpx_asend    # type: ignore[method-assign]
        if _HAS_REQUESTS and _orig_req_send:
            _requests.Session.send = _orig_req_send        # type: ignore[method-assign]


# ── Public API ────────────────────────────────────────────────────────────────

@contextmanager
def inject(
    *behaviors: Behavior,
    urls: list[str] | None = None,
) -> Generator[None, None, None]:
    """
    Context manager that injects HTTP-level faults into all LLM SDK calls.

    Patches ``httpx.Client.send``, ``httpx.AsyncClient.send``, and
    ``requests.Session.send`` at the transport layer.  Every SDK that uses
    httpx or requests under the hood is affected — no per-SDK code needed.

    Parameters
    ----------
    *behaviors:
        One or more :class:`Behavior` instances
        (``Latency``, ``RateLimit``, ``Unavailable``, ``Timeout``, …).
    urls:
        List of URL substrings to match.  Only requests whose URL contains
        at least one of these substrings are affected.
        Defaults to :data:`DEFAULT_LLM_HOSTS` (all common LLM API endpoints).

    Examples
    --------
    Single fault::

        from chaos_jungle.intercept import inject, Latency

        with inject(Latency(3.0)):
            openai_client.chat.completions.create(...)
            anthropic_client.messages.create(...)
            litellm.completion(...)

    Multiple faults::

        with inject(Latency(1.0), RateLimit(after_n=5)):
            agent.run("Summarise this document")

    Scoped to one provider::

        with inject(Unavailable(), urls=["api.openai.com"]):
            run_pipeline()   # only OpenAI calls fail; Anthropic unaffected

    Async code works the same way::

        async def test():
            with inject(Latency(2.0)):
                response = await async_client.chat.completions.create(...)
    """
    patterns = urls if urls is not None else DEFAULT_LLM_HOSTS
    _patch()
    _Stack.push(list(behaviors), patterns)
    try:
        yield
    finally:
        _Stack.pop()
        _unpatch()


__all__ = [
    "inject",
    "DEFAULT_LLM_HOSTS",
    "Behavior",
    "Latency",
    "Jitter",
    "RateLimit",
    "Unavailable",
    "Timeout",
    "CorruptResponse",
]
