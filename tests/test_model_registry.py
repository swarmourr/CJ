"""Tests for ModelConfig, ModelRegistry, and YAML models: integration."""
from __future__ import annotations

import os
import textwrap

import pytest

from chaos_jungle.models import ModelConfig, ModelRegistry


# ── ModelConfig ───────────────────────────────────────────────────────────────

class TestModelConfig:
    def test_defaults(self):
        cfg = ModelConfig(model="gpt-4o-mini")
        assert cfg.base_url == "https://api.openai.com/v1"
        assert cfg.credential_env == "OPENAI_API_KEY"
        assert cfg.provider == "openai"

    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "sk-test")
        cfg = ModelConfig(model="m", credential_env="MY_KEY")
        assert cfg.api_key == "sk-test"

    def test_api_key_missing_env_returns_empty(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cfg = ModelConfig(model="m")
        assert cfg.api_key == ""

    def test_api_key_null_credential_env_returns_empty(self):
        cfg = ModelConfig(model="m", credential_env=None)
        assert cfg.api_key == ""

    def test_fault_upstream_strips_v1(self):
        cfg = ModelConfig(model="m", base_url="https://api.openai.com/v1")
        assert cfg.fault_upstream == "https://api.openai.com"

    def test_fault_upstream_no_v1(self):
        cfg = ModelConfig(model="m", base_url="http://localhost:11434/v1")
        assert cfg.fault_upstream == "http://localhost:11434"

    def test_fault_upstream_without_slash_v1(self):
        cfg = ModelConfig(model="m", base_url="https://api.openai.com")
        assert cfg.fault_upstream == "https://api.openai.com"

    def test_fault_base_url_env_openai(self):
        cfg = ModelConfig(model="m", provider="openai")
        assert cfg.fault_base_url_env == "OPENAI_BASE_URL"

    def test_fault_base_url_env_anthropic(self):
        cfg = ModelConfig(model="m", provider="anthropic")
        assert cfg.fault_base_url_env == "ANTHROPIC_BASE_URL"

    def test_fault_base_url_env_ollama(self):
        cfg = ModelConfig(model="m", provider="ollama")
        assert cfg.fault_base_url_env == "OPENAI_BASE_URL"

    def test_fault_base_url_env_unknown_provider(self):
        cfg = ModelConfig(model="m", provider="custom")
        assert cfg.fault_base_url_env == "OPENAI_BASE_URL"

    def test_as_judge_kwargs(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        cfg = ModelConfig(model="gpt-4o", base_url="https://api.openai.com/v1")
        kw = cfg.as_judge_kwargs()
        assert kw["model"] == "gpt-4o"
        assert kw["base_url"] == "https://api.openai.com/v1"
        assert kw["api_key"] == "sk-x"

    def test_as_generator_kwargs(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cfg = ModelConfig(model="llama3.2", base_url="http://localhost:11434/v1",
                          credential_env=None, provider="ollama")
        kw = cfg.as_generator_kwargs()
        assert kw["model"] == "llama3.2"
        assert kw["api_key"] is None

    def test_as_judge_kwargs_null_key_gives_none(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cfg = ModelConfig(model="m")
        kw = cfg.as_judge_kwargs()
        assert kw["api_key"] is None

    def test_from_dict_minimal(self):
        cfg = ModelConfig.from_dict({"model": "gpt-4o-mini"})
        assert cfg.model == "gpt-4o-mini"
        assert cfg.base_url == "https://api.openai.com/v1"

    def test_from_dict_full(self):
        cfg = ModelConfig.from_dict({
            "model": "llama3.2",
            "provider": "ollama",
            "base_url": "http://localhost:11434/v1",
            "credential_env": None,
        })
        assert cfg.model == "llama3.2"
        assert cfg.provider == "ollama"
        assert cfg.credential_env is None

    def test_from_dict_missing_model_raises(self):
        with pytest.raises(ValueError, match="model"):
            ModelConfig.from_dict({"base_url": "http://x"})

    def test_to_dict_round_trip(self):
        cfg = ModelConfig(model="gpt-4o", provider="openai",
                          base_url="https://api.openai.com/v1",
                          credential_env="OPENAI_API_KEY")
        d = cfg.to_dict()
        cfg2 = ModelConfig.from_dict(d)
        assert cfg.model == cfg2.model
        assert cfg.base_url == cfg2.base_url


# ── ModelRegistry ─────────────────────────────────────────────────────────────

class TestModelRegistry:
    def _reg(self):
        return ModelRegistry.from_dict({
            "agent": {"model": "gpt-4o-mini"},
            "judge": {"model": "gpt-4o"},
            "fault_generator": {"model": "llama3.2",
                                 "base_url": "http://localhost:11434/v1",
                                 "credential_env": None,
                                 "provider": "ollama"},
        })

    def test_from_dict_creates_registry(self):
        reg = self._reg()
        assert "agent" in reg
        assert "judge" in reg
        assert "fault_generator" in reg

    def test_getitem(self):
        reg = self._reg()
        assert reg["agent"].model == "gpt-4o-mini"

    def test_getitem_missing_raises_keyerror(self):
        reg = self._reg()
        with pytest.raises(KeyError, match="missing"):
            _ = reg["missing"]

    def test_get_returns_none_for_missing(self):
        reg = self._reg()
        assert reg.get("missing") is None

    def test_contains(self):
        reg = self._reg()
        assert "agent" in reg
        assert "unknown" not in reg

    def test_roles_sorted(self):
        reg = self._reg()
        assert reg.roles == sorted(["agent", "judge", "fault_generator"])

    def test_len(self):
        reg = self._reg()
        assert len(reg) == 3

    def test_from_dict_not_a_mapping_raises(self):
        with pytest.raises(TypeError):
            ModelRegistry.from_dict(["agent"])

    def test_from_dict_role_not_mapping_raises(self):
        with pytest.raises(TypeError):
            ModelRegistry.from_dict({"agent": "gpt-4o-mini"})

    def test_to_dict_round_trip(self):
        reg = self._reg()
        d = reg.to_dict()
        reg2 = ModelRegistry.from_dict(d)
        assert reg2.roles == reg.roles

    def test_empty_registry(self):
        reg = ModelRegistry()
        assert len(reg) == 0
        assert reg.roles == []


# ── ModelRegistry.validate_dict ───────────────────────────────────────────────

class TestModelRegistryValidation:
    def test_valid_returns_no_errors(self):
        d = {
            "agent": {"model": "gpt-4o-mini", "provider": "openai",
                      "base_url": "https://api.openai.com/v1",
                      "credential_env": "OPENAI_API_KEY"},
        }
        assert ModelRegistry.validate_dict(d) == []

    def test_missing_model_is_error(self):
        d = {"agent": {"base_url": "https://api.openai.com/v1"}}
        errors = ModelRegistry.validate_dict(d)
        assert any("model" in e for e in errors)

    def test_unknown_key_is_error(self):
        d = {"agent": {"model": "m", "bogus": 1}}
        errors = ModelRegistry.validate_dict(d)
        assert any("unknown" in e for e in errors)

    def test_bad_base_url_is_error(self):
        d = {"agent": {"model": "m", "base_url": "ftp://bad"}}
        errors = ModelRegistry.validate_dict(d)
        assert any("base_url" in e for e in errors)

    def test_not_a_mapping_is_error(self):
        errors = ModelRegistry.validate_dict("not-a-dict")
        assert any("mapping" in e for e in errors)

    def test_role_not_mapping_is_error(self):
        errors = ModelRegistry.validate_dict({"agent": "gpt-4o-mini"})
        assert any("mapping" in e for e in errors)


# ── YAML suite integration ────────────────────────────────────────────────────

class TestYAMLModelsIntegration:
    def _write(self, tmp_path, content: str):
        p = tmp_path / "suite.yaml"
        p.write_text(textwrap.dedent(content))
        return str(p)

    def test_suite_without_models_loads(self, tmp_path):
        from chaos_jungle.config import ConfigLoader
        p = self._write(tmp_path, """
            apiVersion: cj.io/v1
            kind: ExperimentSuite
            experiments:
              - name: e1
                faults:
                  - kind: NetworkDelay
                    delay: "100ms"
        """)
        suite = ConfigLoader.load_suite(p)
        assert suite.models is None

    def test_suite_with_models_section_populates_registry(self, tmp_path):
        from chaos_jungle.config import ConfigLoader
        p = self._write(tmp_path, """
            apiVersion: cj.io/v1
            kind: ExperimentSuite
            models:
              agent:
                model: gpt-4o-mini
                base_url: https://api.openai.com/v1
                credential_env: OPENAI_API_KEY
              judge:
                model: gpt-4o
            experiments:
              - name: e1
                faults:
                  - kind: NetworkDelay
                    delay: "100ms"
        """)
        suite = ConfigLoader.load_suite(p)
        assert suite.models is not None
        assert "agent" in suite.models
        assert "judge" in suite.models
        assert suite.models["agent"].model == "gpt-4o-mini"

    def test_workload_model_sets_upstream_on_fault(self, tmp_path):
        from chaos_jungle.config import ConfigLoader
        p = self._write(tmp_path, """
            apiVersion: cj.io/v1
            kind: ExperimentSuite
            models:
              agent:
                model: gpt-4o-mini
                base_url: https://api.openai.com/v1
                credential_env: OPENAI_API_KEY
            experiments:
              - name: e1
                workload_model: agent
                faults:
                  - kind: LLMLatency
                    delay_s: 0.5
        """)
        suite = ConfigLoader.load_suite(p)
        # The LLMLatency fault should have upstream set from agent model config
        scenario = suite._experiments[0][0]
        fault = scenario.faults[0]
        assert fault.upstream == "https://api.openai.com"
        assert fault.base_url_env == "OPENAI_BASE_URL"

    def test_generator_role_resolves_to_fault_kwargs(self, tmp_path):
        from chaos_jungle.config import ConfigLoader
        p = self._write(tmp_path, """
            apiVersion: cj.io/v1
            kind: ExperimentSuite
            models:
              fault_generator:
                model: llama3.2
                base_url: http://localhost:11434/v1
                credential_env: null
                provider: ollama
            experiments:
              - name: e1
                faults:
                  - kind: LLMHallucination
                    generator: fault_generator
        """)
        suite = ConfigLoader.load_suite(p)
        scenario = suite._experiments[0][0]
        fault = scenario.faults[0]
        assert fault.generator_model == "llama3.2"
        assert fault.generator_url == "http://localhost:11434"

    def test_validate_file_rejects_unknown_workload_model(self, tmp_path):
        from chaos_jungle.config import ConfigLoader
        p = self._write(tmp_path, """
            apiVersion: cj.io/v1
            kind: ExperimentSuite
            models:
              agent:
                model: gpt-4o-mini
            experiments:
              - name: e1
                workload_model: nonexistent
                faults: []
        """)
        errors = ConfigLoader.validate_file(p)
        assert any("workload_model" in e or "nonexistent" in e for e in errors)

    def test_validate_file_rejects_unknown_generator_role(self, tmp_path):
        from chaos_jungle.config import ConfigLoader
        p = self._write(tmp_path, """
            apiVersion: cj.io/v1
            kind: ExperimentSuite
            models:
              agent:
                model: gpt-4o-mini
            experiments:
              - name: e1
                faults:
                  - kind: LLMHallucination
                    generator: bad_role
        """)
        errors = ConfigLoader.validate_file(p)
        assert any("generator" in e or "bad_role" in e for e in errors)

    def test_validate_file_valid_models_section(self, tmp_path):
        from chaos_jungle.config import ConfigLoader
        p = self._write(tmp_path, """
            apiVersion: cj.io/v1
            kind: ExperimentSuite
            models:
              agent:
                model: gpt-4o-mini
              judge:
                model: gpt-4o
            experiments:
              - name: e1
                workload_model: agent
                evaluator_model: judge
                faults:
                  - kind: NetworkDelay
                    delay: "50ms"
        """)
        errors = ConfigLoader.validate_file(p)
        assert errors == []


# ── Top-level exports ─────────────────────────────────────────────────────────

class TestTopLevelExports:
    def test_imported_from_top_level(self):
        from chaos_jungle import ModelConfig, ModelRegistry
        assert ModelConfig is not None
        assert ModelRegistry is not None

    def test_as_judge_constructs_judge(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        from chaos_jungle.models import ModelConfig
        from chaos_jungle.analysis.judge import LLMJudge
        cfg = ModelConfig(model="gpt-4o-mini")
        judge = cfg.as_judge()
        assert isinstance(judge, LLMJudge)
        assert judge.model == "gpt-4o-mini"

    def test_as_generator_constructs_generator(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        from chaos_jungle.models import ModelConfig
        from chaos_jungle.faults.skill_file import LLMSkillFaultGenerator
        cfg = ModelConfig(model="llama3.2", base_url="http://localhost:11434/v1",
                          credential_env=None, provider="ollama")
        gen = cfg.as_generator()
        assert isinstance(gen, LLMSkillFaultGenerator)
        assert gen.model == "llama3.2"
