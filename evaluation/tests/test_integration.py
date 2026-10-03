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

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _proxy_config(port: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/_cj/config", timeout=0.5) as resp:
        return json.loads(resp.read())


def _wait_proxy_config(
    port: int,
    expected_fault: str,
    *,
    session_id: int | None = None,
    phase: str | None = None,
    timeout: float = 5.0,
) -> dict:
    deadline = time.time() + timeout
    last: object = None
    while time.time() < deadline:
        try:
            cfg = _proxy_config(port)
            active = (
                [str(item.get("fault", "")) for item in cfg.get("fault_chain", [])]
                if cfg.get("fault_chain")
                else [str(cfg.get("fault", ""))]
            )
            session_matches = session_id is None or str(cfg.get("session_id", "")) == str(session_id)
            phase_matches = phase is None or str(cfg.get("phase", "")) == phase
            if expected_fault in active and session_matches and phase_matches:
                return cfg
            last = cfg
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.05)
    raise AssertionError(
        f"proxy on port {port} did not report fault={expected_fault!r}; last={last!r}"
    )


def _wait_proxy_down(port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/_cj/health", timeout=0.2)
        except Exception:
            return
        time.sleep(0.05)
    raise AssertionError(f"proxy on port {port} is still responding")


def _stop_proxy_process(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _post_chat(base_url: str, messages: list[dict]) -> tuple[int, bytes]:
    body = json.dumps({
        "model": "fake",
        "messages": messages,
        "temperature": 0,
        "max_tokens": 64,
    }).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer dummy"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


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

    def test_role_scoped_unavailable_affects_only_selected_role(
        self, fake_server, monkeypatch
    ):
        """A selector-scoped LLM fault blocks only the matching agent role."""
        monkeypatch.setenv("CJ_EVAL_BASE_URL", fake_server.base_url)
        fake_server.reset_calls()

        from chaos_jungle.faults.llm import LLMUnavailable
        from chaos_jungle.targets.local import LocalTarget

        port = _free_port()
        fault = LLMUnavailable(
            port=port,
            upstream=fake_server.base_url.removesuffix("/v1"),
            base_url_env="CJ_EVAL_BASE_URL",
            selector={"agent_role": "planner"},
        )
        target = LocalTarget()
        try:
            fault.start(target)
            client = ModelClient()

            monkeypatch.setenv("CJ_RUN_ID", "role-scope-test")
            monkeypatch.setenv("CJ_STEP", "1")
            monkeypatch.setenv("CJ_AGENT_ROLE", "planner")
            with pytest.raises(RuntimeError) as excinfo:
                client.chat([{"role": "user", "content": "planner should be blocked"}])
            assert "503" in str(excinfo.value)

            monkeypatch.setenv("CJ_AGENT_ROLE", "coder")
            resp = client.chat([{"role": "user", "content": "coder should pass"}])
            assert "choices" in resp
        finally:
            fault.stop(target)

        assert len(fake_server.calls) == 1

    def test_same_port_passthrough_then_fault_modes_restart_cleanly(
        self, fake_server, monkeypatch, tmp_path
    ):
        """Pass-through must fully yield a port before the fault proxy activates.

        This reproduces the publication-run failure mode where the readiness
        check saw a stale pass-through proxy on the desired fault port and
        verification later reported ``passthrough`` instead of the requested
        fault.  Every stage uses the same port, sends a real request, records
        proxy DB evidence, and verifies clean shutdown before the next mode.
        """
        from chaos_jungle import ChaosRunner, Scenario
        from chaos_jungle.db import SessionDB
        from chaos_jungle.faults.llm import (
            LLMResponseCorrupt,
            LLMUnavailable,
            ToolFault,
            _proxy_script_path,
        )
        from chaos_jungle.targets.local import LocalTarget

        port = _free_port()
        db = SessionDB(str(tmp_path / "cj_proxy_sessions.sqlite3"))
        monkeypatch.setenv("CJ_EVAL_BASE_URL", fake_server.base_url)

        session_id = db.open_session(
            name="same-port-passthrough",
            target_type="http",
            target_addr=f"127.0.0.1:{port}",
        )
        passthrough = subprocess.Popen(
            [
                sys.executable,
                _proxy_script_path(),
                "--port",
                str(port),
                "--upstream",
                fake_server.base_url,
                "--fault",
                "passthrough",
                "--db-path",
                str(db.path),
                "--session-id",
                str(session_id),
                "--phase",
                "control",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            cfg = _wait_proxy_config(
                port,
                "passthrough",
                session_id=session_id,
                phase="control",
            )
            assert cfg["fault"] == "passthrough"
            status, _body = _post_chat(
                f"http://127.0.0.1:{port}/v1",
                [{"role": "user", "content": "control request"}],
            )
            assert status == 200
        finally:
            _stop_proxy_process(passthrough)
            _wait_proxy_down(port)

        control_calls = db.get_llm_calls(session_id, "control")
        assert len(control_calls) == 1
        assert control_calls[0]["fault_name"] == "passthrough"
        assert control_calls[0]["was_blocked"] == 0
        assert control_calls[0]["was_modified"] == 0

        def run_fault_mode(fault, expected_fault: str, messages: list[dict], expected_status: int) -> list[dict]:
            runner = ChaosRunner(
                Scenario(f"same-port-{expected_fault}", [fault]),
                LocalTarget(),
                db=db,
                auto_preflight=False,
            )
            try:
                runner.start()
                cfg = _wait_proxy_config(
                    port,
                    expected_fault,
                    session_id=runner._session_id,
                    phase="fault",
                )
                assert expected_fault in (
                    [str(item.get("fault", "")) for item in cfg.get("fault_chain", [])]
                    if cfg.get("fault_chain")
                    else [str(cfg.get("fault", ""))]
                )
                status, _body = _post_chat(f"http://127.0.0.1:{port}/v1", messages)
                assert status == expected_status
            finally:
                runner.stop()
                _wait_proxy_down(port)
            calls = db.get_llm_calls(runner._session_id, "fault")
            assert calls, f"{expected_fault} did not record any proxy evidence"
            return calls

        unavailable_calls = run_fault_mode(
            LLMUnavailable(
                port=port,
                upstream=fake_server.base_url,
                base_url_env="CJ_EVAL_BASE_URL",
            ),
            "unavailable",
            [{"role": "user", "content": "should be unavailable"}],
            503,
        )
        assert unavailable_calls[0]["fault_name"] == "unavailable"
        assert unavailable_calls[0]["was_blocked"] == 1

        corrupt_calls = run_fault_mode(
            LLMResponseCorrupt(
                mode="invalid_json",
                port=port,
                upstream=fake_server.base_url,
                base_url_env="CJ_EVAL_BASE_URL",
            ),
            "corrupt",
            [{"role": "user", "content": "should be corrupted"}],
            200,
        )
        assert corrupt_calls[0]["fault_name"] == "corrupt"
        assert corrupt_calls[0]["was_modified"] == 1

        tool_calls = run_fault_mode(
            ToolFault(
                port=port,
                upstream=fake_server.base_url,
                base_url_env="CJ_EVAL_BASE_URL",
            ),
            "tool_fault",
            [
                {"role": "user", "content": "run the tool"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "execute_python_code", "arguments": "{}"},
                    }],
                },
                {
                    "role": "tool",
                    "name": "execute_python_code",
                    "tool_call_id": "call_1",
                    "content": "{}",
                },
            ],
            400,
        )
        assert tool_calls[0]["fault_name"] == "tool_fault"
        assert tool_calls[0]["was_blocked"] == 1

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


# ── Evidence classification integration tests ─────────────────────────────────

class TestFaultEvidenceClassification:
    """Non-dry-run ExperimentProtocol tests that assert triggered/manifested/validity.

    These tests start a real CJ proxy subprocess and route through a
    FakeModelServer so no paid API is required.  They verify that the
    evidence fields populated from the CJ DB are correct for latency and
    blocking fault types.
    """

    def _run_fault(self, fault_name, fake_server, monkeypatch, tmp_path, delay_override=None):
        """Helper: run ExperimentProtocol for one task, return the fault RunRecord."""
        import evaluation.experiments.fault_campaign as fc
        monkeypatch.setenv("CJ_EVAL_BASE_URL", fake_server.base_url)
        fake_server.reset_calls()

        if delay_override is not None:
            # Patch in-place for speed; monkeypatch restores after the test.
            monkeypatch.setitem(
                fc._CATALOG_BY_NAME[fault_name]["parameters"], "delay_s", delay_override
            )

        from evaluation.agents.autogen_style import AutoGenStyleAgent
        from evaluation.benchmarks.humanevalplus import _bundled_tasks
        from evaluation.experiments.protocol import ExperimentProtocol

        task   = _bundled_tasks()[0]
        client = ModelClient()
        agent  = AutoGenStyleAgent(client=client, max_turns=1)
        proto  = ExperimentProtocol(agent, fault_name, str(tmp_path), dry_run=False)
        records = proto.run_task(task, seed=0)
        fault_recs = [r for r in records if r.phase == "fault"]
        assert len(fault_recs) == 1, "Expected exactly one fault record"
        return fault_recs[0]

    def test_llm_latency_triggered_and_manifested(
        self, fake_server, monkeypatch, tmp_path
    ):
        """LLMLatency: request reaches proxy, latency applied → triggered + manifested.

        Latency faults are neither blocked nor content-modified; manifestation
        must be detected from triggered_faults_json, not was_blocked/was_modified.
        """
        rec = self._run_fault(
            "llm_latency", fake_server, monkeypatch, tmp_path, delay_override=0.3
        )
        lc = rec.lifecycle
        assert lc.triggered  is True,  f"LLMLatency: triggered should be True, got {lc.triggered}"
        assert lc.manifested is True,  f"LLMLatency: manifested should be True, got {lc.manifested}"
        assert rec.validity  == "valid", f"LLMLatency: validity should be 'valid', got {rec.validity!r}"
        assert lc.recovered  is True,  f"LLMLatency: recovered should be True, got {lc.recovered}"

    def test_llm_unavailable_triggered_and_manifested(
        self, fake_server, monkeypatch, tmp_path
    ):
        """LLMUnavailable: proxy blocks every request (HTTP 503) → triggered + manifested.

        Blocking faults are detected via was_blocked; this test confirms the
        full evidence chain for a fault that never reaches the upstream.
        """
        rec = self._run_fault("llm_unavailable", fake_server, monkeypatch, tmp_path)
        lc = rec.lifecycle
        assert lc.triggered  is True,  f"LLMUnavailable: triggered={lc.triggered}"
        assert lc.manifested is True,  f"LLMUnavailable: manifested={lc.manifested}"
        assert rec.validity  == "valid", f"LLMUnavailable: validity={rec.validity!r}"
        assert lc.recovered  is True,  f"LLMUnavailable: recovered={lc.recovered}"
