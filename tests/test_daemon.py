"""Tests for the chaos-jungle daemon HTTP API.

Covers: auth, /exec disabled by default, scenario CRUD + run + stop,
emergency stop connecting to real runners.
"""
from __future__ import annotations
import threading
import time

import pytest
from fastapi.testclient import TestClient

import chaos_jungle.control.daemon as _daemon_mod
from chaos_jungle.control.daemon import app


# ── Fixture: reset daemon global state between tests ─────────────────────────

@pytest.fixture(autouse=True)
def reset_daemon(monkeypatch):
    """Restore all daemon globals after each test."""
    monkeypatch.setattr(_daemon_mod, "_TOKEN", "test-secret-token")
    monkeypatch.setattr(_daemon_mod, "_DEV_MODE", False)
    monkeypatch.setattr(_daemon_mod, "_EXEC_ENABLED", False)
    # Clear scenario store and runner registry
    _daemon_mod._SCENARIO_STORE.clear()
    _daemon_mod._RUNNER_REGISTRY.clear()
    _daemon_mod._RUNNER_STATUS.clear()
    _daemon_mod._emergency_stop.clear()
    yield
    # Cleanup any runners started during test
    with _daemon_mod._REGISTRY_LOCK:
        for runner in list(_daemon_mod._RUNNER_REGISTRY.values()):
            try:
                runner.stop()
            except Exception:
                pass
        _daemon_mod._RUNNER_REGISTRY.clear()
        _daemon_mod._RUNNER_STATUS.clear()
    _daemon_mod._SCENARIO_STORE.clear()
    _daemon_mod._emergency_stop.clear()


def _auth_header():
    return {"Authorization": "Bearer test-secret-token"}


def _client():
    return TestClient(app, raise_server_exceptions=False)


# ── Authentication tests ──────────────────────────────────────────────────────

class TestAuthentication:
    def test_no_token_configured_returns_503(self, monkeypatch):
        """Daemon started without a token should return 503 on protected routes."""
        monkeypatch.setattr(_daemon_mod, "_TOKEN", "")
        c = _client()
        resp = c.get("/scenarios")
        assert resp.status_code == 503
        assert "token" in resp.json()["detail"].lower()

    def test_wrong_token_returns_401(self):
        c = _client()
        resp = c.get("/scenarios", headers={"Authorization": "Bearer wrong-token"})
        assert resp.status_code == 401

    def test_no_authorization_header_returns_401(self):
        c = _client()
        resp = c.get("/scenarios")
        assert resp.status_code == 401

    def test_correct_token_returns_200(self):
        c = _client()
        resp = c.get("/scenarios", headers=_auth_header())
        assert resp.status_code == 200

    def test_health_requires_no_auth(self):
        c = _client()
        resp = c.get("/health")
        assert resp.status_code == 200

    def test_dev_mode_bypasses_auth(self, monkeypatch):
        monkeypatch.setattr(_daemon_mod, "_DEV_MODE", True)
        monkeypatch.setattr(_daemon_mod, "_TOKEN", "")
        c = _client()
        resp = c.get("/scenarios")
        assert resp.status_code == 200


# ── /exec security tests ──────────────────────────────────────────────────────

class TestExecEndpoint:
    def test_exec_disabled_by_default_returns_403(self):
        """/exec must return 403 when not in dev mode and not explicitly enabled."""
        c = _client()
        resp = c.post(
            "/exec",
            json={"cmd": "echo hello"},
            headers=_auth_header(),
        )
        assert resp.status_code == 403
        assert "disabled" in resp.json()["detail"].lower()

    def test_exec_available_in_dev_mode(self, monkeypatch):
        monkeypatch.setattr(_daemon_mod, "_DEV_MODE", True)
        monkeypatch.setattr(_daemon_mod, "_TOKEN", "")
        c = _client()
        resp = c.post("/exec", json={"cmd": "echo hello"})
        assert resp.status_code == 200
        assert resp.json()["exit_code"] == 0

    def test_exec_enabled_flag_allows_it(self, monkeypatch):
        monkeypatch.setattr(_daemon_mod, "_EXEC_ENABLED", True)
        c = _client()
        resp = c.post("/exec", json={"cmd": "echo hello"}, headers=_auth_header())
        assert resp.status_code == 200

    def test_exec_python_not_in_allowlist(self, monkeypatch):
        """python/python3 must not be in the non-dev allowlist."""
        monkeypatch.setattr(_daemon_mod, "_EXEC_ENABLED", True)
        c = _client()
        resp = c.post(
            "/exec",
            json={"cmd": "python3 -c 'print(1)'"},
            headers=_auth_header(),
        )
        assert resp.status_code == 403

    def test_exec_dd_not_in_allowlist(self, monkeypatch):
        monkeypatch.setattr(_daemon_mod, "_EXEC_ENABLED", True)
        c = _client()
        resp = c.post(
            "/exec",
            json={"cmd": "dd if=/dev/zero of=/tmp/evil bs=1M count=1"},
            headers=_auth_header(),
        )
        assert resp.status_code == 403

    def test_exec_emergency_stop_active_returns_503(self, monkeypatch):
        monkeypatch.setattr(_daemon_mod, "_EXEC_ENABLED", True)
        _daemon_mod._emergency_stop.set()
        c = _client()
        resp = c.post("/exec", json={"cmd": "echo hi"}, headers=_auth_header())
        assert resp.status_code == 503


# ── Scenario CRUD tests ───────────────────────────────────────────────────────

_SCENARIO_PAYLOAD = {
    "id": "test-uuid-1234",
    "name": "test-scenario",
    "faults": [
        {"kind": "NetworkDelay", "params": {"delay": "100ms", "jitter": "", "iface": ""}}
    ],
}


class TestScenarioCRUD:
    def test_create_scenario_stores_faults(self):
        """POST /scenarios must parse and persist the faults list."""
        c = _client()
        resp = c.post("/scenarios", json=_SCENARIO_PAYLOAD, headers=_auth_header())
        assert resp.status_code == 201
        data = resp.json()
        assert data["scenario_id"] == "test-uuid-1234"
        assert data["name"] == "test-scenario"

        # Verify stored in the in-memory store with faults
        stored = _daemon_mod._SCENARIO_STORE.get("test-uuid-1234")
        assert stored is not None
        assert len(stored["faults"]) == 1
        assert stored["faults"][0]["kind"] == "NetworkDelay"

    def test_list_scenarios_returns_stored(self):
        c = _client()
        c.post("/scenarios", json=_SCENARIO_PAYLOAD, headers=_auth_header())
        resp = c.get("/scenarios", headers=_auth_header())
        assert resp.status_code == 200
        ids = [s["id"] for s in resp.json()["scenarios"]]
        assert "test-uuid-1234" in ids

    def test_get_scenario_by_id(self):
        c = _client()
        c.post("/scenarios", json=_SCENARIO_PAYLOAD, headers=_auth_header())
        resp = c.get("/scenarios/test-uuid-1234", headers=_auth_header())
        assert resp.status_code == 200
        assert resp.json()["name"] == "test-scenario"

    def test_get_nonexistent_scenario_returns_404(self):
        c = _client()
        resp = c.get("/scenarios/does-not-exist", headers=_auth_header())
        assert resp.status_code == 404

    def test_scenario_status_endpoint(self):
        c = _client()
        c.post("/scenarios", json=_SCENARIO_PAYLOAD, headers=_auth_header())
        resp = c.get("/scenarios/test-uuid-1234/status", headers=_auth_header())
        assert resp.status_code == 200
        assert "status" in resp.json()


class TestScenarioRunStop:
    def test_run_nonexistent_scenario_returns_404(self):
        c = _client()
        resp = c.post(
            "/scenarios/does-not-exist/run",
            json={},
            headers=_auth_header(),
        )
        assert resp.status_code == 404

    def test_stop_scenario_not_running_returns_404(self):
        """Stopping a scenario that was never run must return 404, not 500."""
        c = _client()
        c.post("/scenarios", json=_SCENARIO_PAYLOAD, headers=_auth_header())
        resp = c.post(
            "/scenarios/test-uuid-1234/stop",
            headers=_auth_header(),
        )
        assert resp.status_code == 404

    def test_run_registers_runner(self, monkeypatch):
        """POST /scenarios/{id}/run must register a runner in the registry."""
        from tests.conftest import TrackingFault

        # Patch Scenario.from_dict to return a scenario with a TrackingFault
        from chaos_jungle.core.scenario import Scenario

        f = TrackingFault()

        def fake_from_dict(data):
            s = Scenario.__new__(Scenario)
            s.id = data["id"]
            s.name = data["name"]
            s.faults = [f]
            return s

        monkeypatch.setattr(Scenario, "from_dict", staticmethod(fake_from_dict))

        c = _client()
        c.post("/scenarios", json=_SCENARIO_PAYLOAD, headers=_auth_header())
        resp = c.post(
            "/scenarios/test-uuid-1234/run",
            json={},
            headers=_auth_header(),
        )
        assert resp.status_code == 202
        # Give the background thread a moment
        time.sleep(0.3)
        with _daemon_mod._REGISTRY_LOCK:
            assert "test-uuid-1234" in _daemon_mod._RUNNER_REGISTRY or \
                   _daemon_mod._RUNNER_STATUS.get("test-uuid-1234") == "active", \
                   "Runner should be registered or active"

    def test_run_when_emergency_stop_active_returns_503(self):
        _daemon_mod._emergency_stop.set()
        c = _client()
        c.post("/scenarios", json=_SCENARIO_PAYLOAD, headers=_auth_header())
        resp = c.post(
            "/scenarios/test-uuid-1234/run",
            json={},
            headers=_auth_header(),
        )
        assert resp.status_code == 503


# ── Emergency stop tests ──────────────────────────────────────────────────────

class TestEmergencyStop:
    def test_emergency_stop_sets_event(self):
        c = _client()
        resp = c.post(
            "/emergency-stop",
            json={"reason": "test emergency"},
            headers=_auth_header(),
        )
        assert resp.status_code == 200
        assert _daemon_mod._emergency_stop.is_set()

    def test_emergency_stop_stops_all_active_runners(self, monkeypatch):
        """POST /emergency-stop must call stop() on every registered runner."""
        stopped_runners = []

        class FakeRunner:
            def __init__(self):
                self._stopped = False

            def stop(self, _status_override=None):
                stopped_runners.append(self)
                self._stopped = True

        r1, r2 = FakeRunner(), FakeRunner()
        with _daemon_mod._REGISTRY_LOCK:
            _daemon_mod._RUNNER_REGISTRY["scenario-1"] = r1
            _daemon_mod._RUNNER_REGISTRY["scenario-2"] = r2

        c = _client()
        resp = c.post(
            "/emergency-stop",
            json={"reason": "test"},
            headers=_auth_header(),
        )
        assert resp.status_code == 200
        assert resp.json()["stopped"] == 2
        assert len(stopped_runners) == 2

        # Registry must be cleared
        with _daemon_mod._REGISTRY_LOCK:
            assert _daemon_mod._RUNNER_REGISTRY == {}

    def test_clear_emergency_stop(self):
        _daemon_mod._emergency_stop.set()
        c = _client()
        resp = c.delete("/emergency-stop", headers=_auth_header())
        assert resp.status_code == 200
        assert not _daemon_mod._emergency_stop.is_set()
