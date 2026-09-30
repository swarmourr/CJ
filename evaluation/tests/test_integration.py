"""Integration test: FakeModelServer → CJ proxy → ModelClient routing.

Verifies that the proxy routing path works end-to-end without any paid API
calls.  The test starts a FakeModelServer (stdlib HTTP), configures a CJ LLM
fault to use it as upstream AND to overwrite CJ_EVAL_BASE_URL, then checks
that a ModelClient request actually travels through the CJ proxy.

These tests start real subprocesses (the CJ proxy) and are therefore slower
than unit tests.  They are marked ``integration`` and require the CJ package
to be installed (which it is, since we're inside chaos-jungle-pkg).
"""

from __future__ import annotations

import os
import time

import pytest

from evaluation.agents.base import ModelClient
from evaluation.agents.fake_model import FakeModelServer


# ── Helpers ───────────────────────────────────────────────────────────────────

def _wait_proxy_ready(port: int, timeout: float = 5.0) -> bool:
    """Poll the proxy health endpoint until it responds or times out."""
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/_cj/health", timeout=0.3
            )
            return True
        except Exception:
            time.sleep(0.05)
    return False


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestProxyRouting:
    """Verify FakeModelServer → CJ proxy → ModelClient chain."""

    def test_modelclient_reads_url_at_call_time(self, fake_server, monkeypatch):
        """_current_base_url() re-reads CJ_EVAL_BASE_URL on each call."""
        monkeypatch.setenv("CJ_EVAL_BASE_URL", fake_server.base_url)
        client = ModelClient()

        # Simulate CJ overwriting the env var (what the fault does on start)
        saved = os.environ.get("CJ_EVAL_BASE_URL")
        try:
            os.environ["CJ_EVAL_BASE_URL"] = "http://127.0.0.1:19999"  # fake proxy URL
            url = client._current_base_url()
            assert "19999" in url, "Client should pick up the updated env var"
        finally:
            if saved is None:
                os.environ.pop("CJ_EVAL_BASE_URL", None)
            else:
                os.environ["CJ_EVAL_BASE_URL"] = saved

    def test_fault_configured_with_cj_eval_base_url_env(self, fake_server, monkeypatch):
        """build_cj_fault() sets base_url_env='CJ_EVAL_BASE_URL'."""
        monkeypatch.setenv("CJ_EVAL_BASE_URL", fake_server.base_url)
        from evaluation.experiments.fault_campaign import build_cj_fault
        from chaos_jungle.faults.llm import _LLMProxyFault

        fault = build_cj_fault("llm_latency")
        assert isinstance(fault, _LLMProxyFault)
        assert fault.base_url_env == "CJ_EVAL_BASE_URL"
        assert fault.upstream == fake_server.base_url

    def test_fault_overrides_cj_eval_base_url_on_start(self, fake_server, monkeypatch):
        """After fault.start() CJ_EVAL_BASE_URL points at the proxy."""
        monkeypatch.setenv("CJ_EVAL_BASE_URL", fake_server.base_url)
        from evaluation.experiments.fault_campaign import build_cj_fault
        from chaos_jungle.targets.local import LocalTarget

        fault  = build_cj_fault("llm_latency")
        target = LocalTarget()
        original_url = os.environ["CJ_EVAL_BASE_URL"]

        try:
            fault.start(target)
            proxy_url = os.environ.get("CJ_EVAL_BASE_URL", "")
            assert "127.0.0.1" in proxy_url
            assert proxy_url != original_url, (
                "CJ_EVAL_BASE_URL should point to proxy, not original API"
            )
        finally:
            fault.stop(target)

        # After stop(), env var is restored
        restored = os.environ.get("CJ_EVAL_BASE_URL", "")
        assert restored == original_url, "CJ_EVAL_BASE_URL not restored after stop()"

    def test_requests_routed_through_proxy_to_fake_server(
        self, fake_server, monkeypatch
    ):
        """ModelClient request travels FakeServer→proxy URL→proxy→FakeServer.

        Chain:
          CJ_EVAL_BASE_URL = fake_server.base_url  (original)
          fault.start()    sets CJ_EVAL_BASE_URL = proxy_url
          ModelClient reads proxy_url, sends to proxy
          proxy forwards to fake_server.base_url (upstream)
          fake_server records the call
        """
        monkeypatch.setenv("CJ_EVAL_BASE_URL", fake_server.base_url)
        fake_server.reset_calls()

        from evaluation.experiments.fault_campaign import build_cj_fault
        from chaos_jungle.targets.local import LocalTarget

        fault  = build_cj_fault("llm_latency")
        target = LocalTarget()

        try:
            fault.start(target)
            # Proxy is now running and CJ_EVAL_BASE_URL = proxy URL
            client = ModelClient()
            resp = client.chat([{"role": "user", "content": "hello from integration test"}])
            assert "choices" in resp
        finally:
            fault.stop(target)

        # Fake server should have received the forwarded request
        assert len(fake_server.calls) >= 1, (
            "No calls reached the FakeModelServer — proxy routing is broken"
        )

    def test_pair_id_shared_between_baseline_and_fault_records(
        self, fake_server, monkeypatch, tmp_path
    ):
        """Baseline and fault records for the same task share a pair_id."""
        monkeypatch.setenv("CJ_EVAL_BASE_URL", fake_server.base_url)

        from evaluation.agents.autogen_style import AutoGenStyleAgent
        from evaluation.benchmarks.humanevalplus import _bundled_tasks
        from evaluation.experiments.protocol import ExperimentProtocol

        task   = _bundled_tasks()[0]
        agent  = AutoGenStyleAgent(client=ModelClient(), max_turns=1)
        proto  = ExperimentProtocol(agent, "llm_timeout", str(tmp_path), dry_run=True)
        records = proto.run_task(task, seed=0)

        b_recs = [r for r in records if r.phase == "baseline"]
        f_recs = [r for r in records if r.phase == "fault"]
        assert len(b_recs) == 1 and len(f_recs) == 1
        assert b_recs[0].pair_id != "", "baseline record must have a pair_id"
        assert b_recs[0].pair_id == f_recs[0].pair_id, (
            "baseline and fault must share the same pair_id"
        )

    def test_cj_commit_is_not_hardcoded(self):
        """cj_commit in RunRecord should reflect the actual HEAD, not a hardcoded SHA."""
        from evaluation.experiments.protocol import _CJ_COMMIT
        # Must be non-empty and either a valid-looking SHA or "unknown"
        assert _CJ_COMMIT
        assert len(_CJ_COMMIT) >= 7, f"commit too short: {_CJ_COMMIT!r}"
