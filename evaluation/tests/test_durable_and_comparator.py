"""Unit checks for durable-state and comparator evaluation gates."""

from __future__ import annotations

from evaluation.comparator_campaign import _toxiproxy_status
from evaluation.durable_state_campaign import EXPECTED_CONTENT, _resume_validation


def test_durable_resume_validation_detects_corrupted_checkpoint():
    ok_state = {"messages": [{"role": "planner", "content": EXPECTED_CONTENT}]}
    bad_state = {"messages": [{"role": "planner", "content": "wrong"}]}

    assert _resume_validation(ok_state) == (True, "checkpoint content valid")
    valid, reason = _resume_validation(bad_state)
    assert valid is False
    assert "mismatch" in reason


def test_toxiproxy_gate_reports_missing_binaries_without_success(monkeypatch):
    monkeypatch.setattr("evaluation.comparator_campaign.shutil.which", lambda _cmd: None)

    status = _toxiproxy_status()

    assert status["comparator"] == "toxiproxy"
    assert status["ready"] is False
    assert "install toxiproxy-server" in status["required_next_steps"]
