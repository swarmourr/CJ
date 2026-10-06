"""Opt-in tests for a real OpenAI-compatible LLM gateway.

These tests are intentionally excluded from the default pytest run by the
``real_eval`` marker and by the ``CJ_EVAL_RUN_GATEWAY_TESTS=1`` gate.  They
validate the provider/gateway before launching paid or quota-consuming
publication experiments:

* the configured ``/models`` endpoint is reachable;
* the selected model appears in the model list;
* a minimal chat completion succeeds;
* the evaluation client preserves the effective ``/v1`` endpoint path;
* secrets are redacted from diagnostics and artifacts.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from evaluation.agents.base import ModelClient
from evaluation.docker_runner import redact_secrets


pytestmark = [pytest.mark.real_eval, pytest.mark.gateway]


@pytest.fixture(scope="session")
def fake_server():
    """Override the global fake server fixture for gateway tests.

    The project-level conftest starts a local fake model server for ordinary
    tests.  Gateway tests either skip before network use or contact the real
    configured endpoint, so requiring the local server here only adds unrelated
    socket permissions and port-collision failure modes.
    """
    return None


@pytest.fixture(autouse=True)
def set_fake_env():
    """Override the project autouse fake-env fixture for gateway tests."""
    return None


@dataclass(frozen=True)
class GatewayConfig:
    base_url: str
    model: str
    api_key: str
    timeout_s: float


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _is_placeholder(value: str | None) -> bool:
    return not value or value in {"dummy", "fake", "test", "changeme"}


def _base_with_v1(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def _url(base_url: str, path: str) -> str:
    return f"{_base_with_v1(base_url)}{path}"


def _gateway_config(monkeypatch: pytest.MonkeyPatch) -> GatewayConfig:
    if os.environ.get("CJ_EVAL_RUN_GATEWAY_TESTS") != "1":
        pytest.skip("set CJ_EVAL_RUN_GATEWAY_TESTS=1 to run real gateway tests")

    env_path = Path(os.environ.get("CJ_EVAL_ENV_FILE") or ".env")
    file_env = _read_env_file(env_path)

    base_url = (
        os.environ.get("CJ_GATEWAY_BASE_URL")
        or file_env.get("LLM_BASE_URL")
        or file_env.get("CJ_EVAL_BASE_URL")
        or os.environ.get("LLM_BASE_URL")
        or os.environ.get("CJ_EVAL_BASE_URL")
        or ""
    )
    model = (
        os.environ.get("CJ_GATEWAY_MODEL")
        or file_env.get("LLM_MODEL")
        or file_env.get("CJ_EVAL_MODEL")
        or os.environ.get("LLM_MODEL")
        or os.environ.get("CJ_EVAL_MODEL")
        or ""
    )
    api_key = (
        os.environ.get("CJ_GATEWAY_API_KEY")
        or file_env.get("LLM_API_KEY")
        or file_env.get("CJ_EVAL_API_KEY")
        or os.environ.get("LLM_API_KEY")
        or os.environ.get("CJ_EVAL_API_KEY")
        or ""
    )
    timeout_s = float(os.environ.get("CJ_GATEWAY_TEST_TIMEOUT_S", "30"))

    if not base_url:
        pytest.skip("missing LLM_BASE_URL/CJ_EVAL_BASE_URL for gateway tests")
    if not model:
        pytest.skip("missing LLM_MODEL/CJ_EVAL_MODEL for gateway tests")
    if _is_placeholder(api_key):
        pytest.skip("missing non-placeholder LLM_API_KEY/CJ_EVAL_API_KEY for gateway tests")

    monkeypatch.setenv("CJ_EVAL_BASE_URL", _base_with_v1(base_url))
    monkeypatch.setenv("CJ_EVAL_MODEL", model)
    monkeypatch.setenv("CJ_EVAL_API_KEY", api_key)
    monkeypatch.setenv("CJ_EVAL_TEMPERATURE", "0.0")

    return GatewayConfig(
        base_url=_base_with_v1(base_url),
        model=model,
        api_key=api_key,
        timeout_s=timeout_s,
    )


def _request_json(
    url: str,
    *,
    api_key: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            assert 200 <= resp.status < 300
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        sanitized = redact_secrets({"url": url, "status": exc.code, "body": body_text})
        raise AssertionError(f"gateway HTTP error: {sanitized}") from exc


def test_gateway_models_endpoint_reachable(monkeypatch):
    cfg = _gateway_config(monkeypatch)

    payload = _request_json(
        _url(cfg.base_url, "/models"),
        api_key=cfg.api_key,
        timeout_s=cfg.timeout_s,
    )
    model_ids = {
        str(item.get("id"))
        for item in payload.get("data", [])
        if isinstance(item, dict) and item.get("id")
    }

    assert model_ids, "gateway returned no model IDs"
    assert cfg.model in model_ids


def test_gateway_chat_completion_minimal(monkeypatch):
    cfg = _gateway_config(monkeypatch)

    payload = _request_json(
        _url(cfg.base_url, "/chat/completions"),
        method="POST",
        api_key=cfg.api_key,
        timeout_s=cfg.timeout_s,
        body={
            "model": cfg.model,
            "messages": [
                {
                    "role": "user",
                    "content": "Reply with the single word: ok",
                }
            ],
            "temperature": 0,
            "max_tokens": 16,
        },
    )

    choices = payload.get("choices")
    assert isinstance(choices, list) and choices
    first_choice = choices[0]
    message = first_choice.get("message")
    assert isinstance(message, dict)
    assert message.get("role", "assistant") == "assistant"
    response_payload_present = any(
        message.get(key) not in (None, "", [])
        for key in ("content", "reasoning_content", "tool_calls")
    )
    finish_reason = first_choice.get("finish_reason")
    assert response_payload_present or finish_reason in {"stop", "length", "tool_calls"}
    usage = payload.get("usage") or {}
    if usage:
        assert int(usage.get("total_tokens", 0)) >= 0


def test_model_client_preserves_gateway_v1_base_url(monkeypatch):
    seen: dict[str, str] = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self) -> bytes:
            return json.dumps({
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {},
            }).encode("utf-8")

    def fake_urlopen(req, timeout=120):
        seen["url"] = req.full_url
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("CJ_EVAL_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("CJ_EVAL_API_KEY", "sk-test-token")
    monkeypatch.setenv("CJ_EVAL_MODEL", "gateway-model")

    response = ModelClient().chat([{"role": "user", "content": "hello"}])

    assert response["choices"][0]["message"]["content"] == "ok"
    assert seen["url"] == "https://gateway.example/v1/chat/completions"


def test_model_client_appends_gateway_v1_when_missing(monkeypatch):
    seen: dict[str, str] = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self) -> bytes:
            return json.dumps({
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {},
            }).encode("utf-8")

    def fake_urlopen(req, timeout=120):
        seen["url"] = req.full_url
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("CJ_EVAL_BASE_URL", "https://gateway.example")
    monkeypatch.setenv("CJ_EVAL_API_KEY", "sk-test-token")
    monkeypatch.setenv("CJ_EVAL_MODEL", "gateway-model")

    ModelClient().chat([{"role": "user", "content": "hello"}])

    assert seen["url"] == "https://gateway.example/v1/chat/completions"


def test_gateway_secret_redaction_for_http_errors():
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    sanitized = redact_secrets({
        "authorization": f"Bearer {secret}",
        "body": f"provider rejected Authorization: Bearer {secret}",
        "safe": "ok",
    })

    encoded = json.dumps(sanitized)
    assert secret not in encoded
    assert "[REDACTED]" in encoded
    assert sanitized["safe"] == "ok"


def test_gateway_model_not_found_reports_clean_error(monkeypatch):
    secret = "sk-abcdefghijklmnopqrstuvwxyz"

    def fake_urlopen(req, timeout=120):
        raise urllib.error.HTTPError(
            req.full_url,
            404,
            "Not Found",
            hdrs=None,
            fp=_BytesReader(b'{"error":{"message":"model not found"}}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("CJ_EVAL_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("CJ_EVAL_API_KEY", secret)
    monkeypatch.setenv("CJ_EVAL_MODEL", "missing-model")

    with pytest.raises(RuntimeError) as excinfo:
        ModelClient().chat([{"role": "user", "content": "hello"}], max_tokens=4)

    message = str(excinfo.value)
    assert "404" in message
    assert "model not found" in message
    assert secret not in message


class _BytesReader:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, *_args, **_kwargs) -> bytes:
        return self._data

    def close(self) -> None:
        return None
