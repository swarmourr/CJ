"""AI Gateway chaos faults.

Intercepts traffic between your application and the AI gateway
(LiteLLM, Portkey, OpenRouter, Kong, custom FastAPI gateway, etc.)
to inject routing failures, cache bugs, policy errors, and multi-tenant
isolation failures.

Every fault extends both :class:`~chaos_jungle.faults.base.Fault` (works
with ``ChaosRunner`` / ``Scenario``) and
:class:`~chaos_jungle.intercept.Behavior` (works directly with
``inject()``).  No subprocess, no proxy, no Linux required.

Quick start::

    from chaos_jungle.faults.gateway import GatewayRouteMisconfig, GatewayCacheStale
    from chaos_jungle import Scenario, ChaosRunner, LocalTarget

    runner = ChaosRunner(
        Scenario("gateway-chaos", [
            GatewayRouteMisconfig(from_model="gpt-4o", to_model="gpt-3.5-turbo"),
            GatewayCacheStale(stale_response="Paris is the capital of France (cached 7 days ago)."),
        ]),
        LocalTarget(),
    )
    runner.start()
    agent.run("What is the capital of France?")
    runner.stop()

Or with inject() directly::

    from chaos_jungle.intercept import inject
    from chaos_jungle.faults.gateway import GatewayRouteMisconfig

    with inject(GatewayRouteMisconfig(to_model="gpt-3.5-turbo")):
        agent.run("question")
"""

from __future__ import annotations

import copy
import json
import re
import threading
from typing import Any

from chaos_jungle.faults.base import Fault
from chaos_jungle.intercept import (
    Behavior,
    DEFAULT_LLM_HOSTS,
    _mock_response,
    _rebuild_request,
)

try:
    import httpx as _httpx
    _HAS_HTTPX = True
except ImportError:
    _httpx = None  # type: ignore[assignment]
    _HAS_HTTPX = False

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _requests = None  # type: ignore[assignment]
    _HAS_REQUESTS = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chat_body(content: str, model: str = "gateway-injected") -> bytes:
    """Minimal chat completion JSON body with *content* as the assistant text."""
    return json.dumps({
        "id": "chatcmpl-cj-gateway",
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }).encode()


def _make_response(status: int, body: bytes, headers: dict, original: Any) -> Any:
    if _HAS_HTTPX and isinstance(original, _httpx.Response):
        return _httpx.Response(status, headers=headers, content=body)
    if _HAS_REQUESTS and isinstance(original, _requests.Response):
        r = _requests.Response()
        r.status_code = status
        for k, v in headers.items():
            r.headers[k] = v
        r._content = body  # type: ignore[attr-defined]
        return r
    return original


_CT_JSON = {"Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Base class: works as both a Fault (ChaosRunner) and a Behavior (inject())
# ---------------------------------------------------------------------------

class _GatewayFault(Fault, Behavior):
    """
    Base for all AI gateway faults.

    Subclasses override :meth:`~chaos_jungle.intercept.Behavior.modify_request`
    and/or :meth:`~chaos_jungle.intercept.Behavior.after` exactly like any
    intercept :class:`~chaos_jungle.intercept.Behavior`.  The ``start`` /
    ``stop`` methods install and remove the fault from the intercept stack so
    the same class works with ``ChaosRunner``.
    """

    danger_level: int = 0
    default_metrics: list[str] = ["error_rate", "success", "duration_s"]
    probability: float = 1.0
    urls: list[str] | None = None  # None → DEFAULT_LLM_HOSTS

    # ── Fault protocol ──────────────────────────────────────────────────────

    def start(self, target: Any) -> None:
        from chaos_jungle.intercept import _patch, _Stack
        _patch()
        _Stack.push([self], self.urls or DEFAULT_LLM_HOSTS)

    def stop(self, target: Any) -> None:
        from chaos_jungle.intercept import _unpatch, _Stack
        _Stack.pop()
        _unpatch()

    def revert(self, target: Any) -> None:
        pass

    def _parameters(self) -> dict:
        return {}


# ---------------------------------------------------------------------------
# 1. GatewayRouteMisconfig
# ---------------------------------------------------------------------------

class GatewayRouteMisconfig(_GatewayFault):
    """
    Rewrite the ``model`` field in outgoing requests to a different model.

    Simulates a gateway routing misconfiguration where requests meant for
    ``from_model`` are silently forwarded to ``to_model``.  The model
    responds normally — the agent never detects it's talking to the wrong
    model unless it validates the ``model`` field in the response.

    Parameters
    ----------
    from_model : str
        Model name to intercept.  Empty string (default) rewrites every
        request regardless of the targeted model.
    to_model : str
        Model name to substitute.  Default ``"gpt-3.5-turbo"``.
    probability : float
        Default ``1.0``.

    Default metrics: ``model_used``, ``error_rate``, ``success``, ``duration_s``
    """

    default_metrics: list[str] = ["model_used", "error_rate", "success", "duration_s"]

    def __init__(
        self,
        from_model: str = "",
        to_model: str = "gpt-3.5-turbo",
        probability: float = 1.0,
    ) -> None:
        self.from_model = from_model
        self.to_model = to_model
        self.probability = probability

    def modify_request(self, url: str, request: Any) -> Any:
        if request is None:
            return request
        try:
            body = getattr(request, "content", None) or b""
            if not body:
                return request
            rb = json.loads(body)
            current = rb.get("model", "")
            if self.from_model and self.from_model not in current:
                return request
            rb["model"] = self.to_model
            return _rebuild_request(request, json.dumps(rb).encode())
        except Exception:
            return request

    def _parameters(self) -> dict:
        return {"from_model": self.from_model, "to_model": self.to_model}

    def __repr__(self) -> str:
        return f"GatewayRouteMisconfig(from={self.from_model!r} → {self.to_model!r})"


# ---------------------------------------------------------------------------
# 2. GatewayFallbackBroken
# ---------------------------------------------------------------------------

class GatewayFallbackBroken(_GatewayFault):
    """
    Simulate a broken fallback chain — both the primary and fallback routes fail.

    The first ``primary_errors`` requests receive ``primary_status`` (503 by
    default).  All subsequent requests — which a healthy gateway would route
    to the fallback provider — receive ``fallback_status`` (502 by default),
    indicating the fallback is also down.

    Parameters
    ----------
    primary_errors : int
        How many requests get the primary error before the fault switches to
        the fallback error.  Default ``1``.
    primary_status : int
        HTTP status for the primary failure.  Default ``503``.
    fallback_status : int
        HTTP status for the fallback failure.  Default ``502``.
    probability : float
        Default ``1.0``.

    Default metrics: ``fallback_success_rate``, ``error_rate``, ``duration_s``
    """

    default_metrics: list[str] = ["fallback_success_rate", "error_rate", "duration_s"]

    def __init__(
        self,
        primary_errors: int = 1,
        primary_status: int = 503,
        fallback_status: int = 502,
        probability: float = 1.0,
    ) -> None:
        self.primary_errors = primary_errors
        self.primary_status = primary_status
        self.fallback_status = fallback_status
        self.probability = probability
        self._count = 0
        self._lock = threading.Lock()

    def after(self, url: str, response: Any) -> Any:
        with self._lock:
            self._count += 1
            n = self._count

        if n <= self.primary_errors:
            return _mock_response(self.primary_status, {
                "error": {
                    "message": "Primary provider unavailable — chaos-jungle gateway primary failure",
                    "type": "server_error",
                    "code": "service_unavailable",
                }
            }, response)

        return _mock_response(self.fallback_status, {
            "error": {
                "message": "Fallback provider also unavailable — chaos-jungle gateway fallback broken",
                "type": "server_error",
                "code": "fallback_unavailable",
            }
        }, response)

    def _parameters(self) -> dict:
        return {
            "primary_errors": self.primary_errors,
            "primary_status": self.primary_status,
            "fallback_status": self.fallback_status,
        }

    def __repr__(self) -> str:
        return (
            f"GatewayFallbackBroken(primary={self.primary_status}, "
            f"fallback={self.fallback_status})"
        )


# ---------------------------------------------------------------------------
# 3. GatewayPolicyBlock
# ---------------------------------------------------------------------------

_POLICY_BODIES: dict[str, bytes] = {
    "openai": json.dumps({
        "error": {
            "message": "The response was filtered due to the prompt triggering content management policy.",
            "type": "content_filter",
            "code": "content_filter",
            "innererror": {
                "code": "ResponsibleAIPolicyViolation",
                "content_filter_result": {
                    "hate": {"filtered": True, "severity": "safe"},
                    "self_harm": {"filtered": False, "severity": "safe"},
                    "sexual": {"filtered": False, "severity": "safe"},
                    "violence": {"filtered": False, "severity": "safe"},
                },
            },
        }
    }).encode(),
    "generic": json.dumps({
        "error": {
            "message": "Request blocked by content policy — chaos-jungle GatewayPolicyBlock",
            "type": "policy_violation",
            "code": "content_policy_violation",
        }
    }).encode(),
}


class GatewayPolicyBlock(_GatewayFault):
    """
    Block a legitimate request with a content-policy error (false positive).

    Simulates a gateway content filter (Azure OpenAI content management,
    Llama Guard, custom moderation engine) incorrectly blocking a safe
    request.  Tests whether your agent explains the block gracefully or
    falls back without crashing.

    Parameters
    ----------
    error_format : ``"openai"`` | ``"generic"``
        Error body shape.  ``"openai"`` mimics Azure OpenAI content-filter
        responses.  Default ``"openai"``.
    after_n : int
        Pass the first N requests unchanged before starting to block.
        Default ``0``.
    probability : float
        Default ``1.0``.

    Default metrics: ``false_block_rate``, ``error_rate``, ``duration_s``
    """

    default_metrics: list[str] = ["false_block_rate", "error_rate", "duration_s"]

    def __init__(
        self,
        error_format: str = "openai",
        after_n: int = 0,
        probability: float = 1.0,
    ) -> None:
        if error_format not in _POLICY_BODIES:
            raise ValueError(
                f"error_format must be one of {list(_POLICY_BODIES)}, got {error_format!r}"
            )
        self.error_format = error_format
        self.after_n = after_n
        self.probability = probability
        self._count = 0
        self._lock = threading.Lock()

    def after(self, url: str, response: Any) -> Any:
        with self._lock:
            self._count += 1
            blocked = self._count > self.after_n

        if not blocked:
            return response

        body = _POLICY_BODIES[self.error_format]
        return _make_response(400, body, _CT_JSON, response)

    def _parameters(self) -> dict:
        return {"error_format": self.error_format, "after_n": self.after_n}

    def __repr__(self) -> str:
        return f"GatewayPolicyBlock(format={self.error_format!r}, after_n={self.after_n})"


# ---------------------------------------------------------------------------
# 4. GatewayPolicyBypass
# ---------------------------------------------------------------------------

_REFUSAL_RE: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in [
        r"i cannot (assist|help|provide|support)",
        r"i('m| am) unable to",
        r"i('m| am) not able to",
        r"i won't (assist|help|do)",
        r"i ('ll )?(refuse|decline) to",
        r"(that|this) (request )?(goes |is )?(beyond|against|violates)",
        r"content.{0,30}(filter|policy|block)",
    ]
]


class GatewayPolicyBypass(_GatewayFault):
    """
    Strip safety-filter refusals and return a normal-looking 200 response.

    Simulates a gateway with its content-safety layer disabled or
    misconfigured — unsafe requests pass through as though accepted.  Use
    this to verify that your application does not rely solely on gateway-level
    safety and validates responses itself.

    Refusals are detected by matching common refusal phrases in the 200-body
    *and* by HTTP 400 responses with a ``content_filter`` error code.

    Parameters
    ----------
    allowed_response : str
        Text to return in place of any detected refusal.
        Default: ``"I can help with that."``
    probability : float
        Default ``1.0``.

    Default metrics: ``safety_violation_rate``, ``error_rate``, ``duration_s``
    """

    default_metrics: list[str] = ["safety_violation_rate", "error_rate", "duration_s"]

    def __init__(
        self,
        allowed_response: str = "I can help with that.",
        probability: float = 1.0,
    ) -> None:
        self.allowed_response = allowed_response
        self.probability = probability

    def _is_refusal(self, response: Any) -> bool:
        try:
            body = getattr(response, "content", b"") or b""
            status = getattr(response, "status_code", 200)
            if status == 400:
                rb = json.loads(body)
                code = (rb.get("error") or {}).get("code", "")
                return "content_filter" in code or "policy" in code
            if status == 200:
                rb = json.loads(body)
                choices = rb.get("choices", [])
                if choices:
                    text = str(choices[0].get("message", {}).get("content", ""))
                    return any(p.search(text) for p in _REFUSAL_RE)
        except Exception:
            pass
        return False

    def after(self, url: str, response: Any) -> Any:
        if not self._is_refusal(response):
            return response
        return _make_response(200, _chat_body(self.allowed_response), _CT_JSON, response)

    def _parameters(self) -> dict:
        return {"allowed_response": self.allowed_response[:50]}

    def __repr__(self) -> str:
        return "GatewayPolicyBypass()"


# ---------------------------------------------------------------------------
# 5. GatewayCacheStale
# ---------------------------------------------------------------------------

class GatewayCacheStale(_GatewayFault):
    """
    Return an outdated cached response instead of a fresh LLM answer.

    Simulates a gateway cache that has not been invalidated.  The response
    is semantically correct but stale — for time-sensitive queries (stock
    prices, weather, news) it silently returns wrong information without
    raising an error.

    Parameters
    ----------
    stale_response : str
        Outdated text to return as the assistant message.
    stale_age : str
        Human-readable age label embedded in the response, e.g. ``"7d"``
        or ``"2 hours"``.  Also set in the ``X-Cache`` header.
    probability : float
        Default ``1.0``.

    Default metrics: ``stale_answer_rate``, ``error_rate``, ``duration_s``
    """

    default_metrics: list[str] = ["stale_answer_rate", "error_rate", "duration_s"]

    def __init__(
        self,
        stale_response: str,
        stale_age: str = "unknown",
        probability: float = 1.0,
    ) -> None:
        self.stale_response = stale_response
        self.stale_age = stale_age
        self.probability = probability

    def after(self, url: str, response: Any) -> Any:
        content = f"{self.stale_response} [CACHED — age: {self.stale_age}]"
        body = _chat_body(content)
        headers = dict(_CT_JSON)
        headers["X-Cache"] = f"HIT, age={self.stale_age}"
        return _make_response(200, body, headers, response)

    def _parameters(self) -> dict:
        return {"stale_response": self.stale_response[:60], "stale_age": self.stale_age}

    def __repr__(self) -> str:
        return f"GatewayCacheStale(age={self.stale_age!r})"


# ---------------------------------------------------------------------------
# 6. GatewayCachePoison
# ---------------------------------------------------------------------------

class GatewayCachePoison(_GatewayFault):
    """
    Return the wrong cached response from a different query.

    Simulates a semantic cache with a loose similarity threshold — a
    different user's query matches the cache key and their response is
    returned to the current user.  Tests whether your agent validates the
    answer against its own question context.

    Parameters
    ----------
    poison_response : str | dict
        Wrong cached content to return.  Strings become the assistant
        message text; dicts are serialised to JSON.
    probability : float
        Default ``1.0``.

    Default metrics: ``hallucination``, ``faithfulness``, ``error_rate``, ``duration_s``

    Use with :class:`~chaos_jungle.judge.LLMJudge` to measure the
    faithfulness drop caused by the poisoned cache hit.
    """

    default_metrics: list[str] = ["hallucination", "faithfulness", "error_rate", "duration_s"]

    def __init__(
        self,
        poison_response: "str | dict",
        probability: float = 1.0,
    ) -> None:
        self.poison_response = (
            poison_response if isinstance(poison_response, str)
            else json.dumps(poison_response)
        )
        self.probability = probability

    def after(self, url: str, response: Any) -> Any:
        body = _chat_body(self.poison_response)
        headers = dict(_CT_JSON)
        headers["X-Cache"] = "HIT-POISONED"
        return _make_response(200, body, headers, response)

    def _parameters(self) -> dict:
        return {"poison_response": self.poison_response[:60]}

    def __repr__(self) -> str:
        preview = self.poison_response[:40]
        return f"GatewayCachePoison({preview!r}…)"


# ---------------------------------------------------------------------------
# 7. GatewayTenantLeak
# ---------------------------------------------------------------------------

class GatewayTenantLeak(_GatewayFault):
    """
    Inject another tenant's data into the response body.

    Simulates a multi-tenant gateway isolation failure where response data
    from tenant A leaks into tenant B's session.  This is the most
    safety-critical gateway fault.

    The foreign data is injected in two places:
    1. As a ``_cj_leaked_tenant_data`` key in the raw response JSON.
    2. Appended to the assistant message content so that agents and
       downstream parsers actually encounter it.

    Pair with :class:`~chaos_jungle.oracles.TenantIsolationOracle` to assert
    that your application never exposes foreign data::

        oracle = TenantIsolationOracle(
            forbidden_values=["foreign-user-id", "other@corp.com"]
        )

    Parameters
    ----------
    foreign_data : dict
        Key-value pairs belonging to the "other" tenant.
    foreign_tenant_id : str
        Label for the leaking tenant — appears in the ``X-Tenant-ID``
        response header.  Default ``"tenant-b"``.
    probability : float
        Default ``1.0``.

    Default metrics: ``error_rate``, ``success``, ``duration_s``
    """

    default_metrics: list[str] = ["error_rate", "success", "duration_s"]

    def __init__(
        self,
        foreign_data: dict,
        foreign_tenant_id: str = "tenant-b",
        probability: float = 1.0,
    ) -> None:
        self.foreign_data = foreign_data
        self.foreign_tenant_id = foreign_tenant_id
        self.probability = probability

    def after(self, url: str, response: Any) -> Any:
        try:
            raw = getattr(response, "content", b"") or b""
            rb = json.loads(raw)
            rb["_cj_leaked_tenant_data"] = self.foreign_data
            rb["_cj_leaked_tenant_id"] = self.foreign_tenant_id
            choices = rb.get("choices", [])
            if choices and isinstance(choices[0].get("message", {}).get("content"), str):
                leak_text = f"\n\n[GATEWAY METADATA] {json.dumps(self.foreign_data)}"
                choices[0]["message"]["content"] += leak_text
            body = json.dumps(rb).encode()
            headers = dict(_CT_JSON)
            headers["X-Tenant-ID"] = self.foreign_tenant_id
            return _make_response(200, body, headers, response)
        except Exception:
            return response

    def _parameters(self) -> dict:
        return {"foreign_tenant_id": self.foreign_tenant_id}

    def __repr__(self) -> str:
        return f"GatewayTenantLeak(tenant={self.foreign_tenant_id!r})"


# ---------------------------------------------------------------------------
# 8. GatewayHeaderStrip
# ---------------------------------------------------------------------------

class GatewayHeaderStrip(_GatewayFault):
    """
    Remove one or more headers from outgoing requests.

    Simulates a misconfigured gateway middleware that strips authentication,
    organisation, or model-routing headers before forwarding the request.
    The request may reach the wrong auth scope or model tier.

    Parameters
    ----------
    headers : list[str]
        Header names to remove (case-insensitive).
        E.g. ``["Authorization", "X-Organization", "X-Model-Tier"]``.
    probability : float
        Default ``1.0``.

    Default metrics: ``auth_error_rate``, ``error_rate``, ``duration_s``
    """

    default_metrics: list[str] = ["auth_error_rate", "error_rate", "duration_s"]

    def __init__(
        self,
        headers: list[str],
        probability: float = 1.0,
    ) -> None:
        self.headers = [h.lower() for h in headers]
        self.probability = probability

    def modify_request(self, url: str, request: Any) -> Any:
        if request is None or not self.headers:
            return request
        try:
            if _HAS_HTTPX and isinstance(request, _httpx.Request):
                new_headers = {
                    k: v for k, v in request.headers.items()
                    if k.lower() not in self.headers
                }
                return _httpx.Request(
                    request.method,
                    request.url,
                    headers=new_headers,
                    content=request.content,
                )
            if _HAS_REQUESTS and hasattr(request, "headers"):
                r = copy.copy(request)
                r.headers = {
                    k: v for k, v in request.headers.items()
                    if k.lower() not in self.headers
                }
                return r
        except Exception:
            pass
        return request

    def _parameters(self) -> dict:
        return {"headers": self.headers}

    def __repr__(self) -> str:
        return f"GatewayHeaderStrip({self.headers!r})"


# ---------------------------------------------------------------------------
# 9. GatewayToolSchemaDrop
# ---------------------------------------------------------------------------

class GatewayToolSchemaDrop(_GatewayFault):
    """
    Remove the ``tools`` / ``functions`` arrays from outgoing LLM requests.

    Simulates a gateway that does not forward tool/function definitions —
    the model receives the user's message but has no callable tools available.
    The agent must fall back to a text-only path or raise an error rather
    than silently skipping tool calls.

    Parameters
    ----------
    after_n : int
        Pass the first N requests unchanged, then start dropping tool
        schemas.  Default ``0`` (drop immediately).
    probability : float
        Default ``1.0``.

    Default metrics: ``tool_call_success_rate``, ``error_rate``, ``duration_s``
    """

    default_metrics: list[str] = ["tool_call_success_rate", "error_rate", "duration_s"]

    def __init__(self, after_n: int = 0, probability: float = 1.0) -> None:
        self.after_n = after_n
        self.probability = probability
        self._count = 0
        self._lock = threading.Lock()

    def modify_request(self, url: str, request: Any) -> Any:
        if request is None:
            return request
        with self._lock:
            self._count += 1
            active = self._count > self.after_n
        if not active:
            return request
        try:
            body = getattr(request, "content", None) or b""
            if not body:
                return request
            rb = json.loads(body)
            if "tools" not in rb and "functions" not in rb:
                return request
            rb.pop("tools", None)
            rb.pop("tool_choice", None)
            rb.pop("functions", None)
            rb.pop("function_call", None)
            return _rebuild_request(request, json.dumps(rb).encode())
        except Exception:
            return request

    def _parameters(self) -> dict:
        return {"after_n": self.after_n}

    def __repr__(self) -> str:
        return f"GatewayToolSchemaDrop(after_n={self.after_n})"


# ---------------------------------------------------------------------------
# 10. GatewayResponseRewrite
# ---------------------------------------------------------------------------

def _set_path(obj: Any, path: str, value: Any) -> None:
    """Set a dot-path field in a nested dict/list.
    E.g. ``choices.0.message.content`` navigates into a list index."""
    parts = path.split(".")
    for part in parts[:-1]:
        key: Any = int(part) if part.isdigit() else part
        obj = obj[key]
    last = parts[-1]
    key = int(last) if last.isdigit() else last
    obj[key] = value


class GatewayResponseRewrite(_GatewayFault):
    """
    Overwrite specific fields in the LLM response body.

    Simulates a gateway that transforms model output — applying content
    annotations, redacting PII, or injecting metadata.  When misconfigured
    it silently corrupts the response or changes the model's answer without
    returning an error.

    Parameters
    ----------
    rewrites : dict[str, any]
        Dot-path field → new value.
        E.g. ``{"choices.0.message.content": "Rewritten by gateway."}``.
    probability : float
        Default ``1.0``.

    Default metrics: ``error_rate``, ``success``, ``duration_s``
    """

    default_metrics: list[str] = ["error_rate", "success", "duration_s"]

    def __init__(self, rewrites: dict, probability: float = 1.0) -> None:
        self.rewrites = rewrites
        self.probability = probability

    def after(self, url: str, response: Any) -> Any:
        if not self.rewrites:
            return response
        try:
            raw = getattr(response, "content", b"") or b""
            rb = json.loads(raw)
            for path, value in self.rewrites.items():
                try:
                    _set_path(rb, path, value)
                except Exception:
                    pass
            body = json.dumps(rb).encode()
            return _make_response(200, body, _CT_JSON, response)
        except Exception:
            return response

    def _parameters(self) -> dict:
        return {"rewrites": {k: str(v)[:40] for k, v in self.rewrites.items()}}

    def __repr__(self) -> str:
        return f"GatewayResponseRewrite({list(self.rewrites)!r})"


# ---------------------------------------------------------------------------
# 11. GatewayBudgetDesync
# ---------------------------------------------------------------------------

class GatewayBudgetDesync(_GatewayFault):
    """
    Simulate a desynchronised budget state in the gateway.

    When ``exhausted=True`` (default) the gateway believes the budget is
    consumed and returns HTTP 402 after ``after_n`` successful calls —
    even though the actual cost has not been reached.  Use this to verify
    that your agent handles budget-exceeded errors without entering an
    infinite retry loop.

    Parameters
    ----------
    exhausted : bool
        ``True`` — budget appears exhausted; return 402.  Default.
    after_n : int
        Number of requests to pass through before the desync kicks in.
        Default ``0``.
    probability : float
        Default ``1.0``.

    Default metrics: ``denied_rate``, ``error_rate``, ``duration_s``
    """

    default_metrics: list[str] = ["denied_rate", "error_rate", "duration_s"]

    def __init__(
        self,
        exhausted: bool = True,
        after_n: int = 0,
        probability: float = 1.0,
    ) -> None:
        self.exhausted = exhausted
        self.after_n = after_n
        self.probability = probability
        self._count = 0
        self._lock = threading.Lock()

    def after(self, url: str, response: Any) -> Any:
        if not self.exhausted:
            return response
        with self._lock:
            self._count += 1
            active = self._count > self.after_n
        if not active:
            return response
        return _mock_response(402, {
            "error": {
                "message": "Monthly budget limit reached — chaos-jungle GatewayBudgetDesync",
                "type": "budget_exceeded",
                "code": "budget_limit_reached",
            }
        }, response)

    def _parameters(self) -> dict:
        return {"exhausted": self.exhausted, "after_n": self.after_n}

    def __repr__(self) -> str:
        return f"GatewayBudgetDesync(exhausted={self.exhausted}, after_n={self.after_n})"


# ---------------------------------------------------------------------------
# 12. GatewayRetryStorm
# ---------------------------------------------------------------------------

class GatewayRetryStorm(_GatewayFault):
    """
    Provoke retry storms by returning 429 for ``storm_calls`` requests
    before finally passing through.

    A misconfigured gateway retry policy multiplies backend calls and cost.
    Use this to verify that your agent respects a maximum retry budget and
    does not hammer the upstream provider.

    After the session ends, ``request_count`` records the total number of
    requests received (real calls + all retries triggered by the storm).

    Parameters
    ----------
    storm_calls : int
        Number of 429 responses to return before passing through.
        Default ``3``.
    retry_after_s : int
        Value for the ``Retry-After`` response header (seconds).
        Default ``1``.
    probability : float
        Default ``1.0``.

    Default metrics: ``p99_latency``, ``cost_usd``, ``error_rate``, ``duration_s``

    Example
    -------
    ::

        fault = GatewayRetryStorm(storm_calls=5)
        with inject(fault):
            agent.run("question")
        print("Total requests (incl. retries):", fault.request_count)
    """

    default_metrics: list[str] = ["p99_latency", "cost_usd", "error_rate", "duration_s"]

    def __init__(
        self,
        storm_calls: int = 3,
        retry_after_s: int = 1,
        probability: float = 1.0,
    ) -> None:
        self.storm_calls = storm_calls
        self.retry_after_s = retry_after_s
        self.probability = probability
        self._count = 0
        self._lock = threading.Lock()

    @property
    def request_count(self) -> int:
        """Total requests seen so far, including retries."""
        return self._count

    def after(self, url: str, response: Any) -> Any:
        with self._lock:
            self._count += 1
            n = self._count

        if n > self.storm_calls:
            return response

        body = json.dumps({
            "error": {
                "message": f"Rate limit exceeded — chaos-jungle retry storm ({n}/{self.storm_calls})",
                "type": "rate_limit_error",
                "code": "rate_limit_exceeded",
            }
        }).encode()
        if _HAS_HTTPX and isinstance(response, _httpx.Response):
            return _httpx.Response(
                429,
                headers={"Content-Type": "application/json", "Retry-After": str(self.retry_after_s)},
                content=body,
            )
        if _HAS_REQUESTS and isinstance(response, _requests.Response):
            r = _requests.Response()
            r.status_code = 429
            r.headers["Retry-After"] = str(self.retry_after_s)
            r._content = body  # type: ignore[attr-defined]
            return r
        return response

    def _parameters(self) -> dict:
        return {"storm_calls": self.storm_calls, "retry_after_s": self.retry_after_s}

    def __repr__(self) -> str:
        return f"GatewayRetryStorm(storm_calls={self.storm_calls})"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "GatewayRouteMisconfig",
    "GatewayFallbackBroken",
    "GatewayPolicyBlock",
    "GatewayPolicyBypass",
    "GatewayCacheStale",
    "GatewayCachePoison",
    "GatewayTenantLeak",
    "GatewayHeaderStrip",
    "GatewayToolSchemaDrop",
    "GatewayResponseRewrite",
    "GatewayBudgetDesync",
    "GatewayRetryStorm",
]
