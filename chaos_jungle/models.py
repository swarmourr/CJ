"""Centralized LLM model configuration for chaos-jungle.

Provides :class:`ModelConfig` (one named model endpoint) and
:class:`ModelRegistry` (a named collection of roles).

Quick start — YAML::

    # suite.yaml
    apiVersion: cj.io/v1
    kind: ExperimentSuite
    models:
      agent:
        provider: openai
        model: gpt-4o-mini
        base_url: https://api.openai.com/v1
        credential_env: OPENAI_API_KEY
      judge:
        provider: openai
        model: gpt-4o
        base_url: https://api.openai.com/v1
        credential_env: OPENAI_API_KEY
      fault_generator:
        provider: ollama
        model: llama3.2
        base_url: http://localhost:11434/v1
        credential_env: null
    experiments:
      - name: hallucination-test
        workload_model: agent       # sets proxy upstream + base_url_env for all faults
        evaluator_model: judge      # used to build LLMJudge
        faults:
          - kind: LLMHallucination
            generator: fault_generator  # sets generator_url + generator_model

Quick start — Python::

    from chaos_jungle.models import ModelConfig, ModelRegistry

    registry = ModelRegistry.from_dict({
        "agent": {"model": "gpt-4o-mini", "base_url": "https://api.openai.com/v1"},
        "judge": {"model": "gpt-4o"},
    })
    judge = registry["judge"].as_judge()
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chaos_jungle.analysis.judge import LLMJudge
    from chaos_jungle.faults.skill_file import LLMSkillFaultGenerator


# provider → env var that the LLM client uses as the base URL override
_PROVIDER_BASE_URL_ENV: dict[str, str] = {
    "openai": "OPENAI_BASE_URL",
    "anthropic": "ANTHROPIC_BASE_URL",
    "google": "GOOGLE_API_BASE",
    "ollama": "OPENAI_BASE_URL",   # Ollama is OpenAI-compatible
    "azure": "OPENAI_BASE_URL",
}

_MODEL_KNOWN_KEYS = frozenset({"provider", "model", "base_url", "credential_env"})


@dataclass
class ModelConfig:
    """Configuration for one named LLM endpoint.

    Parameters
    ----------
    model :
        Model name, e.g. ``"gpt-4o-mini"``, ``"llama3.2"``.
    base_url :
        OpenAI-compatible API base URL, **including** the ``/v1`` suffix
        expected by the SDK. Default: ``"https://api.openai.com/v1"``.
    credential_env :
        Name of the environment variable that holds the API key.
        Set to ``None`` for providers that do not need authentication
        (e.g. local Ollama). Default: ``"OPENAI_API_KEY"``.
    provider :
        Informational provider label (``"openai"``, ``"anthropic"``,
        ``"ollama"``, …). Used to pick the right ``base_url_env`` for the
        LLM proxy. Default: ``"openai"``.
    """

    model: str
    base_url: str = "https://api.openai.com/v1"
    credential_env: str | None = "OPENAI_API_KEY"
    provider: str = "openai"

    @property
    def api_key(self) -> str:
        """Resolve the API key from the environment at call time."""
        if not self.credential_env:
            return ""
        return os.environ.get(self.credential_env, "")

    @property
    def fault_upstream(self) -> str:
        """Base URL without the ``/v1`` suffix — used as ``upstream`` for LLM proxy faults."""
        url = self.base_url.rstrip("/")
        if url.endswith("/v1"):
            url = url[:-3]
        return url

    @property
    def fault_base_url_env(self) -> str:
        """Environment variable the LLM client reads for its base URL.

        Used by LLM proxy faults to redirect the agent's traffic through
        the proxy. Inferred from :attr:`provider`; defaults to
        ``"OPENAI_BASE_URL"`` for unknown providers.
        """
        return _PROVIDER_BASE_URL_ENV.get(self.provider, "OPENAI_BASE_URL")

    def as_judge_kwargs(self) -> dict:
        """Return kwargs suitable for ``LLMJudge(**...)``.

        Example::

            cfg = ModelConfig(model="gpt-4o", credential_env="OPENAI_API_KEY")
            judge = LLMJudge(**cfg.as_judge_kwargs())
        """
        return {
            "model": self.model,
            "base_url": self.base_url,
            "api_key": self.api_key or None,
        }

    def as_generator_kwargs(self) -> dict:
        """Return kwargs suitable for ``LLMSkillFaultGenerator(**...)``.

        Example::

            cfg = ModelConfig(model="llama3.2", base_url="http://localhost:11434/v1",
                              credential_env=None, provider="ollama")
            gen = LLMSkillFaultGenerator(**cfg.as_generator_kwargs())
        """
        return {
            "model": self.model,
            "base_url": self.base_url,
            "api_key": self.api_key or None,
        }

    def as_judge(self, **overrides) -> "LLMJudge":
        """Construct and return an :class:`~chaos_jungle.analysis.judge.LLMJudge`."""
        from chaos_jungle.analysis.judge import LLMJudge
        kwargs = self.as_judge_kwargs()
        kwargs.update(overrides)
        return LLMJudge(**kwargs)

    def as_generator(self, **overrides) -> "LLMSkillFaultGenerator":
        """Construct and return an :class:`~chaos_jungle.faults.skill_file.LLMSkillFaultGenerator`."""
        from chaos_jungle.faults.skill_file import LLMSkillFaultGenerator
        kwargs = self.as_generator_kwargs()
        kwargs.update(overrides)
        return LLMSkillFaultGenerator(**kwargs)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "credential_env": self.credential_env,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ModelConfig":
        """Parse a model config dict (from the ``models:`` YAML section).

        Only the keys in :data:`_MODEL_KNOWN_KEYS` are consumed.
        Unknown keys are silently ignored so that forward-compatible YAML
        does not break existing parsers.
        """
        if not d.get("model"):
            raise ValueError("ModelConfig requires a 'model' field.")
        return cls(
            model=d["model"],
            base_url=d.get("base_url", "https://api.openai.com/v1"),
            credential_env=d.get("credential_env", "OPENAI_API_KEY"),
            provider=d.get("provider", "openai"),
        )


class ModelRegistry:
    """Named collection of :class:`ModelConfig` objects indexed by role.

    Typical roles: ``"agent"``, ``"judge"``, ``"fault_generator"``.
    Any string is valid as a role name.

    Parameters
    ----------
    roles :
        Mapping of role name → :class:`ModelConfig`.

    Examples
    --------
    Build from a dict::

        registry = ModelRegistry.from_dict({
            "agent": {"model": "gpt-4o-mini"},
            "judge": {"model": "gpt-4o"},
        })
        judge = registry["judge"].as_judge()
        print(registry.roles)  # ["agent", "judge"]
    """

    def __init__(self, roles: dict[str, ModelConfig] | None = None) -> None:
        self._roles: dict[str, ModelConfig] = dict(roles or {})

    # ── Access ────────────────────────────────────────────────────────────────

    def get(self, role: str) -> "ModelConfig | None":
        """Return the :class:`ModelConfig` for *role*, or ``None``."""
        return self._roles.get(role)

    def __getitem__(self, role: str) -> ModelConfig:
        if role not in self._roles:
            raise KeyError(f"Model role {role!r} not found in registry. "
                           f"Available roles: {sorted(self._roles)}")
        return self._roles[role]

    def __contains__(self, role: str) -> bool:
        return role in self._roles

    def __len__(self) -> int:
        return len(self._roles)

    def __repr__(self) -> str:
        return f"ModelRegistry(roles={sorted(self._roles)})"

    @property
    def roles(self) -> list[str]:
        """Sorted list of registered role names."""
        return sorted(self._roles)

    # ── Construction ──────────────────────────────────────────────────────────

    @classmethod
    def from_dict(cls, d: dict) -> "ModelRegistry":
        """Parse the ``models:`` YAML section into a :class:`ModelRegistry`.

        Parameters
        ----------
        d :
            Mapping of role name → model config dict.

        Example::

            registry = ModelRegistry.from_dict({
                "agent": {
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "base_url": "https://api.openai.com/v1",
                    "credential_env": "OPENAI_API_KEY",
                },
            })
        """
        if not isinstance(d, dict):
            raise TypeError(f"models: section must be a mapping, got {type(d).__name__}")
        roles: dict[str, ModelConfig] = {}
        for role, cfg in d.items():
            if not isinstance(cfg, dict):
                raise TypeError(
                    f"models.{role}: must be a mapping, got {type(cfg).__name__}"
                )
            roles[role] = ModelConfig.from_dict(cfg)
        return cls(roles)

    def to_dict(self) -> dict:
        return {role: cfg.to_dict() for role, cfg in self._roles.items()}

    @staticmethod
    def validate_dict(d: dict) -> list[str]:
        """Validate a raw ``models:`` section dict. Return list of error strings."""
        errors: list[str] = []
        if not isinstance(d, dict):
            errors.append("models: must be a mapping")
            return errors
        for role, cfg in d.items():
            prefix = f"models.{role}"
            if not isinstance(cfg, dict):
                errors.append(f"{prefix}: must be a mapping")
                continue
            if not cfg.get("model"):
                errors.append(f"{prefix}: missing required field 'model'")
            unknown = set(cfg) - _MODEL_KNOWN_KEYS
            if unknown:
                errors.append(f"{prefix}: unknown keys {sorted(unknown)}")
            base = cfg.get("base_url", "")
            if base and not (base.startswith("http://") or base.startswith("https://")):
                errors.append(f"{prefix}.base_url: must start with http:// or https://")
        return errors
