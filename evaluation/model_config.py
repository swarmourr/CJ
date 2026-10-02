"""Shared model configuration for real-agent evaluation adapters.

All three real-agent frameworks (AutoGen, LangGraph, CrewAI) must use exactly
the same model parameters so that the primary experiment variable is only the
agent architecture.  This module provides the single source of truth for those
parameters.

Design rules
------------
- ``base_url`` is NOT stored in ModelConfig; it is always re-read from
  ``CJ_EVAL_BASE_URL`` at call time so CJ proxy redirects are transparent.
- The API key never appears in YAML; ``api_key_env`` names the env var.
- ``transport_retries`` MUST be 0 in fault-injection experiments so injected
  faults are not silently masked by SDK-level automatic retry logic.
- ``cost_usd`` is ``None`` when pricing fields are absent from the config;
  agents must not report 0.0 to avoid silent cost under-counting.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass, field


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _redact(value: str) -> str:
    """Return a log-safe version of a secret value."""
    if not value or value in ("dummy", ""):
        return value
    return value[:4] + "…[REDACTED]"


# ---------------------------------------------------------------------------
# ModelConfig
# ---------------------------------------------------------------------------

@dataclass
class ModelConfig:
    """All model parameters needed to instantiate real-framework clients.

    Attributes
    ----------
    provider : str
        Logical provider tag (informational only, e.g. "openai_compatible").
    name : str
        Model identifier sent to the API (e.g. ``"gpt-4o-mini"``).
    api_key : str
        Resolved API key.  Set by :func:`load_model_config` from the env var
        named in ``api_key_env``.  May be ``"dummy"`` for local endpoints.
    api_key_env : str
        Name of the environment variable that holds the real API key.
    temperature : float
        Sampling temperature.  0.0 is fully deterministic.
    max_tokens : int
        Maximum tokens in the completion.
    request_timeout_s : float
        Per-request timeout passed to the SDK.
    transport_retries : int
        SDK-level automatic retries.  Must be 0 for fault-injection experiments.
    input_price_per_1k_tokens : float
        USD cost per 1 000 prompt tokens (0.0 means pricing unknown).
    output_price_per_1k_tokens : float
        USD cost per 1 000 completion tokens (0.0 means pricing unknown).
    """

    provider:   str   = "openai_compatible"
    name:       str   = "gpt-4o-mini"
    api_key:    str   = "dummy"
    api_key_env: str  = ""
    temperature: float = 0.0
    max_tokens:  int   = 2048
    request_timeout_s:    float = 120.0
    transport_retries:    int   = 0
    input_price_per_1k_tokens:  float = 0.0
    output_price_per_1k_tokens: float = 0.0

    # ------------------------------------------------------------------
    # Dynamic properties — re-read env at access time
    # ------------------------------------------------------------------

    @property
    def current_base_url(self) -> str:
        """Re-read base URL from env on every access.

        CJ LLM proxy faults overwrite ``CJ_EVAL_BASE_URL`` on start and
        restore it on stop.  Framework clients created inside ``agent.run()``
        read this property AFTER ``fault.start()``, so they automatically
        target the proxy.  Clients created outside the fault window target
        the real endpoint.

        ``OPENAI_BASE_URL`` is checked as a fallback for non-CJ usage.
        A ``/v1`` suffix is appended when missing so both
        ``https://api.openai.com`` and ``https://api.openai.com/v1`` produce
        identical request URLs.
        """
        url = (
            os.environ.get("CJ_EVAL_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or ""
        ).rstrip("/")
        if url and not url.endswith("/v1"):
            url = url + "/v1"
        return url

    def resolved_api_key(self) -> str:
        """Return the current API key, re-reading the named env var each call."""
        if self.api_key_env:
            key = os.environ.get(self.api_key_env, "")
            if key:
                return key
        return self.api_key or "dummy"

    def endpoint_type(self) -> str:
        """Classify the current endpoint without exposing credentials."""
        url = self.current_base_url
        if not url:
            return "dry_run"
        if "127.0.0.1" in url or "localhost" in url:
            return "local"
        if "openai.com" in url:
            return "openai"
        return "openai_compat"

    # ------------------------------------------------------------------
    # Cost computation
    # ------------------------------------------------------------------

    def compute_cost(self, prompt_tokens: int, completion_tokens: int) -> float | None:
        """Return cost in USD, or None if pricing is not configured.

        Returns None rather than 0.0 when pricing fields are absent so that
        downstream analysis can distinguish "free/local" from "not measured".
        """
        if self.input_price_per_1k_tokens <= 0 and self.output_price_per_1k_tokens <= 0:
            return None
        return (
            prompt_tokens  * self.input_price_per_1k_tokens  / 1000.0
            + completion_tokens * self.output_price_per_1k_tokens / 1000.0
        )

    # ------------------------------------------------------------------
    # Provenance
    # ------------------------------------------------------------------

    def config_hash(self) -> str:
        """Stable 12-char hash of all model parameters (not the api_key)."""
        safe = {
            k: v for k, v in asdict(self).items() if k not in ("api_key",)
        }
        blob = str(sorted(safe.items())).encode()
        return hashlib.sha256(blob).hexdigest()[:12]

    def safe_repr(self) -> str:
        """Return a log-safe representation (redacts api_key)."""
        return (
            f"ModelConfig(name={self.name!r}, provider={self.provider!r}, "
            f"temperature={self.temperature}, max_tokens={self.max_tokens}, "
            f"timeout={self.request_timeout_s}s, retries={self.transport_retries}, "
            f"api_key_env={self.api_key_env!r}, api_key={_redact(self.api_key)!r})"
        )


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def load_model_config(cfg: dict) -> ModelConfig:
    """Build a :class:`ModelConfig` from a YAML ``model:`` section dict.

    Precedence for the API key (highest → lowest):
      1. env var named by ``api_key_env`` (already exported or set from .env)
      2. ``api_key`` literal in the config dict (discouraged)
      3. ``"dummy"`` (valid only for local / unauthenticated endpoints)

    The YAML ``base_url`` field is intentionally ignored here; the base URL is
    always resolved dynamically from ``CJ_EVAL_BASE_URL``.

    Parameters
    ----------
    cfg : dict
        The value of the ``model:`` key from the YAML config.  May be empty.
    """
    if not cfg:
        cfg = {}

    yaml_name = str(cfg.get("name", cfg.get("model", "gpt-4o-mini")))

    mc = ModelConfig(
        provider=str(cfg.get("provider", "openai_compatible")),
        name=yaml_name,
        api_key_env=str(cfg.get("api_key_env", "")),
        temperature=float(cfg.get("temperature", 0.0)),
        max_tokens=int(cfg.get("max_tokens", 2048)),
        request_timeout_s=float(cfg.get("request_timeout_s", 120.0)),
        transport_retries=int(cfg.get("transport_retries", 0)),
        input_price_per_1k_tokens=float(cfg.get("input_price_per_1k_tokens", 0.0)),
        output_price_per_1k_tokens=float(cfg.get("output_price_per_1k_tokens", 0.0)),
    )

    # Resolve the API key from the named env var
    if mc.api_key_env:
        key = os.environ.get(mc.api_key_env, "")
        if key:
            mc.api_key = key
        else:
            import warnings
            warnings.warn(
                f"[eval] model.api_key_env={mc.api_key_env!r} is not set. "
                "Using a local placeholder API key valid only for local endpoints.",
                UserWarning,
                stacklevel=2,
            )
    elif "api_key" in cfg:
        # Discouraged: literal key in the YAML file
        mc.api_key = str(cfg["api_key"])

    return mc


def require_api_key(mc: ModelConfig) -> None:
    """Raise RuntimeError when no valid API key is available for a remote endpoint.

    Local endpoints (``127.0.0.1`` / ``localhost``) and dry-run mode (no URL)
    are accepted without a key.  Remote endpoints require a non-empty, non-dummy
    key — ``"dummy"`` is explicitly rejected so mis-configured runs fail fast
    instead of silently wasting quota or producing invalid results.
    """
    key      = mc.resolved_api_key()
    endpoint = mc.current_base_url
    is_remote = (
        bool(endpoint)
        and "127.0.0.1" not in endpoint
        and "localhost" not in endpoint
    )
    if is_remote and key in ("", "dummy"):
        env_var = mc.api_key_env or "CJ_EVAL_API_KEY"
        raise RuntimeError(
            f"A real API key is required for non-local endpoint {endpoint!r}. "
            f"'dummy' is not accepted for remote endpoints. "
            f"Export {env_var} or add it to .env before running real-agent experiments."
        )
