"""Tests for the real-agent evaluation layer.

Test categories
---------------
Unit tests (no framework packages required):
  - ModelConfig construction and validation
  - .env loading and dotenv precedence
  - load_model_config() from YAML dicts
  - API key resolution and missing-credential failure
  - API key / authorization header redaction
  - Shared model configuration across frameworks
  - Config hash consistency
  - Provenance fields in RunRecord

Adapter unit tests (mock framework imports):
  - AutoGenRealAgent construction
  - LangGraphRealAgent construction
  - CrewAIRealAgent construction
  - model_name property on real adapters
  - uses_model_config flag

Integration tests (require evaluation package deps — marked real_eval):
  - framework → CJ proxy → FakeModelServer routing (per framework)
  - native tool-call traversal (role="tool" in follow-up request)
  - ToolFault activation where supported
  - pair_id matching across baseline and fault records
  - fresh framework state per run

Run only unit tests (core test suite):
    pytest -m "not real_eval"

Run integration tests (requires pip install -e ".[evaluation]"):
    pytest evaluation/tests/test_real_agents.py -m real_eval
"""

from __future__ import annotations

import os
import sys
import types
import uuid
import warnings

import pytest


# ---------------------------------------------------------------------------
# ── ModelConfig unit tests ────────────────────────────────────────────────
# ---------------------------------------------------------------------------

class TestModelConfig:

    def test_defaults(self):
        from evaluation.model_config import ModelConfig
        mc = ModelConfig()
        assert mc.name == "gpt-4o-mini"
        assert mc.temperature == 0.0
        assert mc.transport_retries == 0
        assert mc.api_key == "dummy"

    def test_current_base_url_reads_env(self, monkeypatch):
        from evaluation.model_config import ModelConfig
        monkeypatch.setenv("CJ_EVAL_BASE_URL", "http://127.0.0.1:9999")
        mc = ModelConfig()
        assert "9999" in mc.current_base_url

    def test_current_base_url_appends_v1(self, monkeypatch):
        from evaluation.model_config import ModelConfig
        monkeypatch.setenv("CJ_EVAL_BASE_URL", "https://api.openai.com")
        mc = ModelConfig()
        assert mc.current_base_url.endswith("/v1")

    def test_current_base_url_v1_idempotent(self, monkeypatch):
        from evaluation.model_config import ModelConfig
        monkeypatch.setenv("CJ_EVAL_BASE_URL", "http://localhost:11434/v1")
        mc = ModelConfig()
        assert mc.current_base_url == "http://localhost:11434/v1"

    def test_current_base_url_updates_after_env_change(self, monkeypatch):
        from evaluation.model_config import ModelConfig
        monkeypatch.setenv("CJ_EVAL_BASE_URL", "http://127.0.0.1:8000")
        mc = ModelConfig()
        assert "8000" in mc.current_base_url
        monkeypatch.setenv("CJ_EVAL_BASE_URL", "http://127.0.0.1:9999")
        assert "9999" in mc.current_base_url  # re-read on access

    def test_resolved_api_key_uses_env_var(self, monkeypatch):
        from evaluation.model_config import ModelConfig
        monkeypatch.setenv("MY_TEST_KEY", "sk-test-abc123")
        mc = ModelConfig(api_key_env="MY_TEST_KEY")
        assert mc.resolved_api_key() == "sk-test-abc123"

    def test_resolved_api_key_fallback_to_stored(self, monkeypatch):
        from evaluation.model_config import ModelConfig
        monkeypatch.delenv("MISSING_KEY", raising=False)
        mc = ModelConfig(api_key_env="MISSING_KEY", api_key="stored-key")
        assert mc.resolved_api_key() == "stored-key"

    def test_resolved_api_key_dummy_default(self, monkeypatch):
        from evaluation.model_config import ModelConfig
        monkeypatch.delenv("CJ_EVAL_API_KEY", raising=False)
        mc = ModelConfig()
        assert mc.resolved_api_key() == "dummy"

    def test_compute_cost_none_when_no_pricing(self):
        from evaluation.model_config import ModelConfig
        mc = ModelConfig()
        assert mc.compute_cost(1000, 500) is None

    def test_compute_cost_with_pricing(self):
        from evaluation.model_config import ModelConfig
        mc = ModelConfig(
            input_price_per_1k_tokens=0.15,
            output_price_per_1k_tokens=0.60,
        )
        cost = mc.compute_cost(1000, 1000)
        assert cost is not None
        assert abs(cost - 0.75) < 1e-9

    def test_config_hash_stable(self):
        from evaluation.model_config import ModelConfig
        mc = ModelConfig(name="gpt-4o-mini", temperature=0.0)
        assert mc.config_hash() == mc.config_hash()

    def test_config_hash_changes_with_params(self):
        from evaluation.model_config import ModelConfig
        h1 = ModelConfig(name="gpt-4o-mini").config_hash()
        h2 = ModelConfig(name="gpt-4o").config_hash()
        assert h1 != h2

    def test_config_hash_excludes_api_key(self):
        from evaluation.model_config import ModelConfig
        h1 = ModelConfig(name="gpt-4o-mini", api_key="key-a").config_hash()
        h2 = ModelConfig(name="gpt-4o-mini", api_key="key-b").config_hash()
        assert h1 == h2  # api_key must not affect config hash

    def test_safe_repr_does_not_contain_key(self):
        from evaluation.model_config import ModelConfig
        mc = ModelConfig(api_key="sk-secret-value-12345")
        r  = mc.safe_repr()
        assert "sk-secret-value-12345" not in r
        assert "REDACTED" in r or "sk-s" in r  # partial or redacted

    def test_endpoint_type_local(self, monkeypatch):
        from evaluation.model_config import ModelConfig
        monkeypatch.setenv("CJ_EVAL_BASE_URL", "http://127.0.0.1:11434")
        mc = ModelConfig()
        assert mc.endpoint_type() == "local"

    def test_endpoint_type_openai(self, monkeypatch):
        from evaluation.model_config import ModelConfig
        monkeypatch.setenv("CJ_EVAL_BASE_URL", "https://api.openai.com")
        mc = ModelConfig()
        assert mc.endpoint_type() == "openai"

    def test_endpoint_type_openai_compat(self, monkeypatch):
        from evaluation.model_config import ModelConfig
        monkeypatch.setenv("CJ_EVAL_BASE_URL", "https://myprovider.example.com")
        mc = ModelConfig()
        assert mc.endpoint_type() == "openai_compat"


# ---------------------------------------------------------------------------
# ── .env loading and credential precedence ────────────────────────────────
# ---------------------------------------------------------------------------

class TestDotenvLoading:

    def test_load_dotenv_does_not_override_exported_vars(self, tmp_path, monkeypatch):
        """Exported env var wins over .env value (override=False)."""
        env_file = tmp_path / ".env"
        env_file.write_text("CJ_EVAL_API_KEY=from-dotenv\n")

        monkeypatch.setenv("CJ_EVAL_API_KEY", "from-shell")
        # Simulate dotenv load
        from dotenv import load_dotenv
        load_dotenv(str(env_file), override=False)
        assert os.environ["CJ_EVAL_API_KEY"] == "from-shell"

    def test_load_dotenv_sets_unset_vars(self, tmp_path, monkeypatch):
        """dotenv sets a var that was not already exported."""
        env_file = tmp_path / ".env"
        env_file.write_text("CJ_EVAL_API_KEY=from-dotenv\n")

        monkeypatch.delenv("CJ_EVAL_API_KEY", raising=False)
        from dotenv import load_dotenv
        load_dotenv(str(env_file), override=False)
        assert os.environ.get("CJ_EVAL_API_KEY") == "from-dotenv"
        monkeypatch.delenv("CJ_EVAL_API_KEY", raising=False)  # cleanup

    def test_missing_dotenv_is_silent(self, tmp_path):
        """Absent .env file is silently ignored."""
        from dotenv import load_dotenv
        # Should not raise
        load_dotenv(str(tmp_path / ".env"), override=False)

    def test_run_module_dotenv_loads_on_import(self):
        """evaluation.run imports without error (dotenv may silently skip)."""
        import evaluation.run  # noqa: F401  — just verifying importability


# ---------------------------------------------------------------------------
# ── load_model_config ─────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

class TestLoadModelConfig:

    def test_empty_dict_returns_defaults(self):
        from evaluation.model_config import load_model_config
        mc = load_model_config({})
        assert mc.name == "gpt-4o-mini"
        assert mc.temperature == 0.0
        assert mc.transport_retries == 0

    def test_name_from_model_key(self):
        from evaluation.model_config import load_model_config
        mc = load_model_config({"model": "gpt-4o"})
        assert mc.name == "gpt-4o"

    def test_name_from_name_key(self):
        from evaluation.model_config import load_model_config
        mc = load_model_config({"name": "claude-3-5-haiku-latest"})
        assert mc.name == "claude-3-5-haiku-latest"

    def test_api_key_env_resolution(self, monkeypatch):
        from evaluation.model_config import load_model_config
        monkeypatch.setenv("MY_SECRET_KEY", "sk-real-key-abc")
        mc = load_model_config({"api_key_env": "MY_SECRET_KEY"})
        assert mc.api_key == "sk-real-key-abc"

    def test_missing_api_key_env_warns(self, monkeypatch):
        from evaluation.model_config import load_model_config
        monkeypatch.delenv("NONEXISTENT_KEY_XYZ", raising=False)
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            load_model_config({"api_key_env": "NONEXISTENT_KEY_XYZ"})
        assert any("local placeholder API key" in str(x.message) for x in w)

    def test_full_config_parsed(self):
        from evaluation.model_config import load_model_config
        cfg = {
            "provider":           "openai_compatible",
            "name":               "gpt-4o-mini",
            "temperature":        0.0,
            "max_tokens":         1024,
            "request_timeout_s":  60.0,
            "transport_retries":  0,
            "input_price_per_1k_tokens":  0.15,
            "output_price_per_1k_tokens": 0.60,
        }
        mc = load_model_config(cfg)
        assert mc.max_tokens == 1024
        assert mc.request_timeout_s == 60.0
        assert mc.input_price_per_1k_tokens == 0.15

    def test_all_frameworks_get_same_model_name(self):
        """Shared ModelConfig ensures all adapters use the same model."""
        from evaluation.model_config import load_model_config
        cfg = {"name": "gpt-4o-mini", "temperature": 0.0}
        mc  = load_model_config(cfg)
        # All three real adapters share the same ModelConfig instance;
        # verify the name attribute is identical.
        from evaluation.agents.autogen_real   import AutoGenRealAgent
        from evaluation.agents.langgraph_real import LangGraphRealAgent
        from evaluation.agents.crewai_real    import CrewAIRealAgent
        a  = AutoGenRealAgent(mc)
        lg = LangGraphRealAgent(mc)
        cr = CrewAIRealAgent(mc)
        assert a.model_name == lg.model_name == cr.model_name == "gpt-4o-mini"


# ---------------------------------------------------------------------------
# ── Adapter construction unit tests ───────────────────────────────────────
# ---------------------------------------------------------------------------

class TestAdapterConstruction:

    def _make_mc(self, monkeypatch):
        from evaluation.model_config import ModelConfig
        monkeypatch.setenv("CJ_EVAL_BASE_URL", "http://127.0.0.1:9999")
        return ModelConfig(name="gpt-4o-mini")

    def test_autogen_real_construction(self, monkeypatch):
        from evaluation.agents.autogen_real import AutoGenRealAgent
        mc = self._make_mc(monkeypatch)
        agent = AutoGenRealAgent(model_config=mc, max_turns=5)
        assert agent.name == "autogen-real"
        assert agent.model_name == "gpt-4o-mini"
        assert agent.uses_model_config is True
        assert agent.client is None       # no ModelClient
        assert agent.max_turns == 5

    def test_langgraph_real_construction(self, monkeypatch):
        from evaluation.agents.langgraph_real import LangGraphRealAgent
        mc = self._make_mc(monkeypatch)
        agent = LangGraphRealAgent(model_config=mc, max_turns=8)
        assert agent.name == "langgraph-real"
        assert agent.model_name == "gpt-4o-mini"
        assert agent.uses_model_config is True
        assert agent.client is None
        assert agent.max_turns == 8

    def test_crewai_real_construction(self, monkeypatch):
        from evaluation.agents.crewai_real import CrewAIRealAgent
        mc = self._make_mc(monkeypatch)
        agent = CrewAIRealAgent(model_config=mc, max_turns=4)
        assert agent.name == "crewai-real"
        assert agent.model_name == "gpt-4o-mini"
        assert agent.uses_model_config is True
        assert agent.client is None
        assert agent.max_turns == 4

    def test_real_adapters_in_registry(self):
        from evaluation.agents import REGISTRY
        assert "autogen-real"   in REGISTRY
        assert "langgraph-real" in REGISTRY
        assert "crewai-real"    in REGISTRY

    def test_style_agents_have_no_uses_model_config(self):
        from evaluation.agents import REGISTRY
        for name in ("autogen", "mad", "mapcoder"):
            assert not getattr(REGISTRY[name], "uses_model_config", False)

    def test_model_name_from_model_client(self, monkeypatch):
        """Style agents use client.model for model_name."""
        from evaluation.agents.autogen_style import AutoGenStyleAgent
        from evaluation.agents.base import ModelClient
        monkeypatch.setenv("CJ_EVAL_BASE_URL", "http://127.0.0.1:9000")
        monkeypatch.setenv("CJ_EVAL_MODEL", "my-model-id")
        client = ModelClient()
        agent  = AutoGenStyleAgent(client=client)
        assert agent.model_name == "my-model-id"


# ---------------------------------------------------------------------------
# ── Require API key ────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

class TestRequireApiKey:

    def test_local_endpoint_does_not_require_key(self, monkeypatch):
        from evaluation.model_config import ModelConfig, require_api_key
        monkeypatch.setenv("CJ_EVAL_BASE_URL", "http://127.0.0.1:11434")
        mc = ModelConfig(api_key="dummy")
        require_api_key(mc)  # should not raise

    def test_empty_key_passes_for_local(self, monkeypatch):
        from evaluation.model_config import ModelConfig, require_api_key
        monkeypatch.setenv("CJ_EVAL_BASE_URL", "http://localhost:8000")
        mc = ModelConfig(api_key="")
        require_api_key(mc)  # local endpoint; no key required

    def test_non_local_with_key_passes(self, monkeypatch):
        from evaluation.model_config import ModelConfig, require_api_key
        monkeypatch.setenv("CJ_EVAL_BASE_URL", "https://api.openai.com")
        mc = ModelConfig(api_key="sk-real-key")
        require_api_key(mc)  # has key — no raise

    def test_non_local_dummy_key_raises(self, monkeypatch):
        from evaluation.model_config import ModelConfig, require_api_key
        monkeypatch.setenv("CJ_EVAL_BASE_URL", "https://api.openai.com")
        mc = ModelConfig(api_key="dummy")
        # dummy key must be rejected for remote endpoints to prevent silent
        # mis-configured runs.
        with pytest.raises(RuntimeError, match="dummy"):
            require_api_key(mc)

    def test_non_local_empty_key_raises(self, monkeypatch):
        from evaluation.model_config import ModelConfig, require_api_key
        monkeypatch.setenv("CJ_EVAL_BASE_URL", "https://api.openai.com")
        mc = ModelConfig(api_key="")
        with pytest.raises(RuntimeError):
            require_api_key(mc)


# ---------------------------------------------------------------------------
# ── Key redaction ─────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------

class TestKeyRedaction:

    def test_redact_hides_secret(self):
        from evaluation.model_config import _redact
        assert "secret" not in _redact("sk-secretvalue123")
        assert "REDACTED" in _redact("sk-secretvalue123") or "sk-s" in _redact("sk-secretvalue123")

    def test_redact_leaves_dummy(self):
        from evaluation.model_config import _redact
        assert _redact("dummy") == "dummy"

    def test_safe_repr_no_secret(self):
        from evaluation.model_config import ModelConfig
        mc = ModelConfig(api_key="sk-my-super-secret-key")
        r  = mc.safe_repr()
        assert "sk-my-super-secret-key" not in r

    def test_run_record_no_api_key_field(self):
        """RunRecord to_dict() must not contain the API key."""
        from evaluation.experiments.protocol import RunRecord, LifecycleEvidence
        rec = RunRecord(
            run_id="x", timestamp="t", cj_commit="c",
            agent_system="autogen-real", benchmark="humanevalplus",
            task_id="HumanEval/0", model="gpt-4o-mini",
            endpoint_type="openai", seed=0, fault_type="none",
            fault_parameters={}, target="local", phase="baseline",
            success=1.0, duration_s=1.0, reported_error=0.0, retries=0,
            llm_calls=1, tool_calls=0, turns=1,
            prompt_tokens=10, completion_tokens=5, total_tokens=15,
            cost_usd=0.0, tests_passed=1, tests_total=1,
            generated_code_hash="abc", termination_reason="success",
            exception="",
        )
        d = rec.to_dict()
        # API key must never appear in the serialised record
        d_str = str(d)
        assert "api_key" not in d_str.lower() or "api_key_env" in d_str.lower()


# ---------------------------------------------------------------------------
# ── Provenance fields in RunRecord ────────────────────────────────────────
# ---------------------------------------------------------------------------

class TestProvenanceFields:

    def _make_record(self):
        from evaluation.experiments.protocol import RunRecord
        return RunRecord(
            run_id=str(uuid.uuid4()),
            timestamp="2026-01-01T00:00:00Z",
            cj_commit="abc123",
            agent_system="autogen-real",
            benchmark="humanevalplus",
            task_id="HumanEval/0",
            model="gpt-4o-mini",
            endpoint_type="openai",
            seed=42,
            fault_type="none",
            fault_parameters={},
            target="local",
            phase="baseline",
            success=1.0,
            duration_s=1.5,
            reported_error=0.0,
            retries=0,
            llm_calls=2,
            tool_calls=1,
            turns=2,
            prompt_tokens=100,
            completion_tokens=80,
            total_tokens=180,
            cost_usd=0.01,
            tests_passed=5,
            tests_total=5,
            generated_code_hash="deadbeef1234",
            termination_reason="success",
            exception="",
            framework_version="autogen-agentchat 0.4.9",
            python_version="3.13.1",
            config_hash="cafebabe0123",
        )

    def test_framework_version_stored(self):
        rec = self._make_record()
        assert rec.framework_version == "autogen-agentchat 0.4.9"

    def test_python_version_stored(self):
        rec = self._make_record()
        assert rec.python_version == "3.13.1"

    def test_config_hash_stored(self):
        rec = self._make_record()
        assert rec.config_hash == "cafebabe0123"

    def test_to_dict_includes_provenance(self):
        rec = self._make_record()
        d   = rec.to_dict()
        assert "framework_version" in d
        assert "python_version"    in d
        assert "config_hash"       in d

    def test_pair_id_present(self):
        rec = self._make_record()
        d   = rec.to_dict()
        assert "pair_id" in d  # may be empty string for baseline-only

    def test_campaign_id_present(self):
        from evaluation.experiments.protocol import RunRecord
        rec = self._make_record()
        d   = rec.to_dict()
        assert "campaign_id" in d

    def test_campaign_id_shared_within_protocol(self, monkeypatch, tmp_path):
        """All records from one ExperimentProtocol share the same campaign_id."""
        from evaluation.agents.autogen_style import AutoGenStyleAgent
        from evaluation.agents.base import ModelClient
        from evaluation.experiments.protocol import ExperimentProtocol
        from evaluation.benchmarks.humanevalplus import _bundled_tasks

        monkeypatch.setenv("CJ_EVAL_BASE_URL", "http://127.0.0.1:1")

        def factory():
            return AutoGenStyleAgent(
                client=ModelClient(dry_run=True), dry_run=True, max_turns=1
            )

        proto = ExperimentProtocol(
            agent_factory=factory,
            fault_name="none",
            output_dir=str(tmp_path),
            dry_run=True,
        )
        tasks = _bundled_tasks()[:2]
        recs  = proto.run_campaign(tasks, seed=0)
        campaign_ids = {r.campaign_id for r in recs}
        assert len(campaign_ids) == 1, "All records must share one campaign_id"
        assert list(campaign_ids)[0] != "", "campaign_id must not be empty"

    def test_cj_commit_is_not_hardcoded(self):
        from evaluation.experiments.protocol import _CJ_COMMIT
        assert _CJ_COMMIT
        assert len(_CJ_COMMIT) >= 7


# ---------------------------------------------------------------------------
# ── agent_factory in ExperimentProtocol ───────────────────────────────────
# ---------------------------------------------------------------------------

class TestAgentFactory:

    def test_protocol_accepts_agent_factory(self, monkeypatch, tmp_path):
        """agent_factory creates a fresh agent for each run."""
        monkeypatch.setenv("CJ_EVAL_BASE_URL", "http://127.0.0.1:1")  # invalid but dry-run
        from evaluation.agents.autogen_style import AutoGenStyleAgent
        from evaluation.agents.base import ModelClient
        from evaluation.experiments.protocol import ExperimentProtocol

        call_count = [0]

        def factory():
            call_count[0] += 1
            return AutoGenStyleAgent(
                client=ModelClient(dry_run=True), dry_run=True, max_turns=1
            )

        proto = ExperimentProtocol(
            agent_factory=factory,
            fault_name="none",
            output_dir=str(tmp_path),
            dry_run=True,
        )
        from evaluation.benchmarks.humanevalplus import _bundled_tasks
        task  = _bundled_tasks()[0]
        recs  = proto.run_task(task, seed=0)
        # factory called at least once (baseline)
        assert call_count[0] >= 1
        assert len(recs) == 1  # fault_name=none: only baseline

    def test_protocol_raises_without_agent_or_factory(self):
        from evaluation.experiments.protocol import ExperimentProtocol
        with pytest.raises(ValueError, match="agent.*agent_factory"):
            ExperimentProtocol(fault_name="none")

    def test_pair_id_shared_with_factory(self, monkeypatch, tmp_path):
        """Baseline and fault records share pair_id even when factory is used."""
        from evaluation.agents.autogen_style import AutoGenStyleAgent
        from evaluation.agents.base import ModelClient
        from evaluation.experiments.protocol import ExperimentProtocol
        from evaluation.benchmarks.humanevalplus import _bundled_tasks

        monkeypatch.setenv("CJ_EVAL_BASE_URL", "http://127.0.0.1:1")

        def factory():
            return AutoGenStyleAgent(
                client=ModelClient(dry_run=True), dry_run=True, max_turns=1
            )

        proto = ExperimentProtocol(
            agent_factory=factory,
            fault_name="llm_timeout",
            output_dir=str(tmp_path),
            dry_run=True,
        )
        task  = _bundled_tasks()[0]
        recs  = proto.run_task(task, seed=0)
        b_rec = [r for r in recs if r.phase == "baseline"][0]
        f_rec = [r for r in recs if r.phase == "fault"][0]
        assert b_rec.pair_id == f_rec.pair_id
        assert b_rec.pair_id != ""


# ---------------------------------------------------------------------------
# ── YAML config parsing ───────────────────────────────────────────────────
# ---------------------------------------------------------------------------

class TestYamlConfig:

    def _load(self, path: str) -> dict:
        import yaml
        with open(path) as f:
            return yaml.safe_load(f) or {}

    def test_real_smoke_yaml_parses(self):
        base = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        path = os.path.join(base, "evaluation", "configs", "real_smoke.yaml")
        cfg  = self._load(path)
        assert "model" in cfg
        assert "experiments" in cfg
        assert len(cfg["experiments"]) == 3

    def test_real_smoke_shared_model(self):
        base = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        path = os.path.join(base, "evaluation", "configs", "real_smoke.yaml")
        cfg  = self._load(path)
        model = cfg["model"]
        assert "name" in model or "model" in model
        # All experiments reference the same model block (not per-experiment)
        for exp in cfg["experiments"]:
            assert "model" not in exp, "Per-experiment model overrides are not supported"

    def test_real_smoke_transport_retries_zero(self):
        base = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        path = os.path.join(base, "evaluation", "configs", "real_smoke.yaml")
        cfg  = self._load(path)
        assert cfg["model"].get("transport_retries", 0) == 0

    def test_real_development_yaml_parses(self):
        base = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        path = os.path.join(base, "evaluation", "configs", "real_development.yaml")
        cfg  = self._load(path)
        assert "model" in cfg
        assert len(cfg["experiments"]) >= 10

    def test_env_example_no_secret(self):
        base = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        path = os.path.join(base, ".env.example")
        text = open(path).read()
        # The example file must have the key name but not a real value
        assert "CJ_EVAL_API_KEY" in text
        assert "CJ_EVAL_API_KEY=\n" in text or "CJ_EVAL_API_KEY=\r" in text or \
               text.strip().endswith("CJ_EVAL_API_KEY=")


# ---------------------------------------------------------------------------
# ── CLI / YAML / env precedence ───────────────────────────────────────────
# ---------------------------------------------------------------------------

class TestConfigPrecedence:

    def test_cli_flag_overrides_yaml(self, monkeypatch):
        """CLI model flag wins over YAML model section."""
        from evaluation.run import _apply_env_overrides, _apply_model_config
        import argparse
        args = argparse.Namespace(
            base_url=None, api_key=None, model="cli-model", dry_run=False
        )
        monkeypatch.delenv("CJ_EVAL_MODEL", raising=False)
        _apply_env_overrides(args)
        assert os.environ.get("CJ_EVAL_MODEL") == "cli-model"
        # YAML value must NOT override CLI value (setdefault semantics)
        _apply_model_config({"model": {"model": "yaml-model"}}, dry_run=False)
        assert os.environ.get("CJ_EVAL_MODEL") == "cli-model"

    def test_yaml_sets_when_env_absent(self, monkeypatch):
        from evaluation.run import _apply_model_config
        monkeypatch.delenv("CJ_EVAL_MODEL", raising=False)
        monkeypatch.delenv("CJ_EVAL_BASE_URL", raising=False)
        _apply_model_config({
            "model": {
                "model": "yaml-only-model",
                "base_url": "http://yaml-only.example.com",
            }
        }, dry_run=False)
        assert os.environ.get("CJ_EVAL_MODEL") == "yaml-only-model"

    def test_dry_run_skips_model_config(self, monkeypatch):
        from evaluation.run import _apply_model_config
        monkeypatch.delenv("CJ_EVAL_MODEL", raising=False)
        _apply_model_config({"model": {"model": "should-not-be-set"}}, dry_run=True)
        assert os.environ.get("CJ_EVAL_MODEL") is None


# ---------------------------------------------------------------------------
# ── Integration tests (real_eval) — require eval packages ─────────────────
# ---------------------------------------------------------------------------

@pytest.mark.real_eval
class TestAutoGenRealProxyRouting:
    """AutoGen → CJ proxy → FakeModelServer wiring.

    Instantiates the real autogen-agentchat package.  Requires:
        pip install -e ".[evaluation]"
    """

    def test_autogen_routes_through_fake_server(self, fake_server, monkeypatch, tmp_path):
        """AutoGen real adapter sends requests to FakeModelServer."""
        autogen_ac = pytest.importorskip("autogen_agentchat",
                                         reason="autogen-agentchat not installed")
        monkeypatch.setenv("CJ_EVAL_BASE_URL", fake_server.base_url)
        monkeypatch.setenv("CJ_EVAL_API_KEY",  "dummy")
        fake_server.reset_calls()

        from evaluation.model_config import ModelConfig
        from evaluation.agents.autogen_real import AutoGenRealAgent

        mc    = ModelConfig(name="fake", request_timeout_s=30.0)
        agent = AutoGenRealAgent(model_config=mc, max_turns=2)

        result = agent.run("Write a Python function that returns 42.", seed=0)

        assert isinstance(result.generated_code, str)
        assert isinstance(result.duration_s, float)
        assert len(fake_server.calls) >= 1, "No requests reached the fake server"

    def test_autogen_fresh_client_per_run(self, fake_server, monkeypatch, tmp_path):
        """Each agent.run() uses the current CJ_EVAL_BASE_URL, not construction-time URL."""
        pytest.importorskip("autogen_agentchat",
                            reason="autogen-agentchat not installed")
        from evaluation.agents.fake_model import FakeModelServer
        from evaluation.model_config import ModelConfig
        from evaluation.agents.autogen_real import AutoGenRealAgent

        with FakeModelServer() as server2:
            monkeypatch.setenv("CJ_EVAL_BASE_URL", fake_server.base_url)
            mc    = ModelConfig(name="fake", request_timeout_s=30.0)
            agent = AutoGenRealAgent(model_config=mc, max_turns=2)

            # First run — hits fake_server
            fake_server.reset_calls()
            agent.run("Write a function that returns 1.", seed=0)
            calls_1 = len(fake_server.calls)

            # Swap URL to server2 — fresh client must follow
            monkeypatch.setenv("CJ_EVAL_BASE_URL", server2.base_url)
            server2_calls_before = len(server2.calls)
            agent.run("Write a function that returns 2.", seed=0)
            server2_calls_after = len(server2.calls)

            assert calls_1 >= 1,  "First run sent no requests"
            assert server2_calls_after > server2_calls_before, (
                "Second run did not route to the updated server URL"
            )


@pytest.mark.real_eval
class TestLangGraphRealProxyRouting:
    """LangGraph → CJ proxy → FakeModelServer wiring."""

    def test_langgraph_routes_through_fake_server(self, fake_server, monkeypatch, tmp_path):
        pytest.importorskip("langgraph", reason="langgraph not installed")
        pytest.importorskip("langchain_openai", reason="langchain-openai not installed")
        monkeypatch.setenv("CJ_EVAL_BASE_URL", fake_server.base_url)
        monkeypatch.setenv("CJ_EVAL_API_KEY",  "dummy")
        fake_server.reset_calls()

        from evaluation.model_config import ModelConfig
        from evaluation.agents.langgraph_real import LangGraphRealAgent

        mc    = ModelConfig(name="fake", request_timeout_s=30.0)
        agent = LangGraphRealAgent(model_config=mc, max_turns=4)

        result = agent.run("Write a Python function that returns 42.", seed=0)

        assert isinstance(result.generated_code, str)
        assert len(fake_server.calls) >= 1, "No requests reached the fake server"

    def test_langgraph_fresh_client_per_run(self, fake_server, monkeypatch):
        pytest.importorskip("langgraph", reason="langgraph not installed")
        pytest.importorskip("langchain_openai", reason="langchain-openai not installed")
        from evaluation.agents.fake_model import FakeModelServer
        from evaluation.model_config import ModelConfig
        from evaluation.agents.langgraph_real import LangGraphRealAgent

        with FakeModelServer() as server2:
            monkeypatch.setenv("CJ_EVAL_BASE_URL", fake_server.base_url)
            mc    = ModelConfig(name="fake", request_timeout_s=30.0)
            agent = LangGraphRealAgent(model_config=mc, max_turns=2)

            fake_server.reset_calls()
            agent.run("Write a function.", seed=0)
            calls_1 = len(fake_server.calls)

            monkeypatch.setenv("CJ_EVAL_BASE_URL", server2.base_url)
            before = len(server2.calls)
            agent.run("Write another function.", seed=0)
            assert len(server2.calls) > before, "LangGraph did not pick up new URL"
            assert calls_1 >= 1


@pytest.mark.real_eval
class TestCrewAIRealProxyRouting:
    """CrewAI → CJ proxy → FakeModelServer wiring."""

    def test_crewai_routes_through_fake_server(self, fake_server, monkeypatch):
        pytest.importorskip("crewai", reason="crewai not installed")
        monkeypatch.setenv("CJ_EVAL_BASE_URL", fake_server.base_url)
        monkeypatch.setenv("CJ_EVAL_API_KEY",  "dummy")
        fake_server.reset_calls()

        from evaluation.model_config import ModelConfig
        from evaluation.agents.crewai_real import CrewAIRealAgent

        mc    = ModelConfig(name="gpt-4o-mini", request_timeout_s=30.0)
        agent = CrewAIRealAgent(model_config=mc, max_turns=3)

        result = agent.run("Write a Python function that returns 42.", seed=0)

        assert isinstance(result.generated_code, str)
        assert len(fake_server.calls) >= 1, "No requests reached the fake server"

    def test_crewai_role_tool_in_proxy(self, monkeypatch):
        """CrewAI sends role=tool message via LiteLLM when a tool is used."""
        pytest.importorskip("crewai", reason="crewai not installed")

        from evaluation.agents.fake_model import ToolAwareFakeServer
        from evaluation.model_config import ModelConfig
        from evaluation.agents.crewai_real import CrewAIRealAgent

        with ToolAwareFakeServer(tool_name="execute_python") as srv:
            monkeypatch.setenv("CJ_EVAL_BASE_URL", srv.base_url)
            monkeypatch.setenv("CJ_EVAL_API_KEY", "dummy")

            mc    = ModelConfig(name="gpt-4o-mini", request_timeout_s=30.0)
            agent = CrewAIRealAgent(model_config=mc, max_turns=4)

            agent.run("Write a Python function that returns 42.", seed=0)

            assert len(srv.calls) >= 2, (
                f"Expected >=2 requests (tool_calls + tool_result), got {len(srv.calls)}"
            )
            assert len(srv.tool_call_requests) >= 1, (
                "No role='tool' message reached the server from CrewAI"
            )


# ---------------------------------------------------------------------------
# ── Tool-call traversal integration tests (real_eval) ─────────────────────
# ---------------------------------------------------------------------------

@pytest.mark.real_eval
class TestNativeToolCallTraversal:
    """Verify role="tool" request crosses the proxy when the model requests a tool.

    Uses ToolAwareFakeServer which:
      - Returns tool_calls on first request
      - Returns text completion on role=tool request
    """

    def test_langgraph_role_tool_in_proxy(self, monkeypatch, tmp_path):
        """LangGraph sends role=tool message back to the model."""
        pytest.importorskip("langgraph", reason="langgraph not installed")
        pytest.importorskip("langchain_openai", reason="langchain-openai not installed")

        from evaluation.agents.fake_model import ToolAwareFakeServer
        from evaluation.model_config import ModelConfig
        from evaluation.agents.langgraph_real import LangGraphRealAgent

        with ToolAwareFakeServer(tool_name="execute_python_code") as srv:
            monkeypatch.setenv("CJ_EVAL_BASE_URL", srv.base_url)
            monkeypatch.setenv("CJ_EVAL_API_KEY", "dummy")

            mc    = ModelConfig(name="fake", request_timeout_s=30.0)
            agent = LangGraphRealAgent(model_config=mc, max_turns=4)

            result = agent.run("def add(a, b): ...", seed=0)

            assert len(srv.calls) >= 2, (
                "Expected at least 2 requests (tool_calls + tool_result), "
                f"got {len(srv.calls)}"
            )
            assert len(srv.tool_call_requests) >= 1, (
                "No request with role='tool' reached the server"
            )

    def test_autogen_role_tool_in_proxy(self, monkeypatch):
        """AutoGen sends role=tool message back when tool is registered."""
        pytest.importorskip("autogen_agentchat",
                            reason="autogen-agentchat not installed")
        from evaluation.agents.fake_model import ToolAwareFakeServer
        from evaluation.model_config import ModelConfig
        from evaluation.agents.autogen_real import AutoGenRealAgent

        with ToolAwareFakeServer(tool_name="execute_code") as srv:
            monkeypatch.setenv("CJ_EVAL_BASE_URL", srv.base_url)
            monkeypatch.setenv("CJ_EVAL_API_KEY", "dummy")

            mc    = ModelConfig(name="fake", request_timeout_s=30.0)
            agent = AutoGenRealAgent(model_config=mc, max_turns=4)

            agent.run("Write a solution.", seed=0)

            # At least 2 calls: the initial + at least one with tool result
            assert len(srv.calls) >= 2, (
                f"Expected >=2 requests, got {len(srv.calls)}"
            )


@pytest.mark.real_eval
class TestToolFaultActivation:
    """Prove ToolFault activates when role=tool request reaches the CJ proxy.

    Chain:
      real framework agent
        → request 1 (no tool messages)  → ToolAwareFakeServer returns tool_calls
        → framework executes tool
        → request 2 (role="tool")       → CJ ToolFault intercepts!
        → evidence: triggered=True, manifested=True
    """

    def test_langgraph_toolfault_activation(self, monkeypatch, tmp_path):
        pytest.importorskip("langgraph", reason="langgraph not installed")
        pytest.importorskip("langchain_openai", reason="langchain-openai not installed")

        from evaluation.agents.fake_model import ToolAwareFakeServer
        from evaluation.model_config import ModelConfig
        from evaluation.agents.langgraph_real import LangGraphRealAgent
        from evaluation.experiments.protocol import ExperimentProtocol
        from evaluation.benchmarks.humanevalplus import _bundled_tasks

        with ToolAwareFakeServer(tool_name="execute_python_code") as srv:
            monkeypatch.setenv("CJ_EVAL_BASE_URL", srv.base_url)
            monkeypatch.setenv("CJ_EVAL_API_KEY", "dummy")

            mc = ModelConfig(name="fake", request_timeout_s=30.0)

            def factory():
                return LangGraphRealAgent(model_config=mc, max_turns=4)

            proto = ExperimentProtocol(
                agent_factory=factory,
                fault_name="tool_failure",
                output_dir=str(tmp_path),
                dry_run=False,
            )

            task  = _bundled_tasks()[0]
            recs  = proto.run_task(task, seed=0)
            f_recs = [r for r in recs if r.phase == "fault"]
            assert len(f_recs) == 1
            lc = f_recs[0].lifecycle
            # ToolFault lifecycle: must have activated → triggered → manifested
            assert lc.activated is True, "Fault did not activate"
            # triggered: at least one role=tool request crossed the proxy
            assert lc.triggered is True, (
                f"Expected lifecycle.triggered=True (role=tool request hit proxy); "
                f"lifecycle={lc}"
            )
            # manifested: the fault actually modified or blocked the tool response
            assert lc.manifested is True, (
                f"Expected lifecycle.manifested=True; lifecycle={lc}"
            )
            # Validity must be 'valid' for this record to count in metrics
            assert f_recs[0].validity == "valid", (
                f"Expected validity='valid', got {f_recs[0].validity!r}"
            )
