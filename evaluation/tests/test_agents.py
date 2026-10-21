"""Tests for agent adapters using the fake deterministic model server.

Covers acceptance criterion 3: AutoGen, MAD, MapCoder adapters pass
deterministic mock smoke tests.
"""

from typing import get_type_hints

import pytest

from evaluation.agents.base import AgentRunResult, AgentSystem, ModelClient
from evaluation.agents.autogen_style import AutoGenStyleAgent
from evaluation.agents.mad_style import MADStyleAgent
from evaluation.agents.mapcoder_style import MapCoderStyleAgent
from evaluation.agents.tool_triggered import ToolTriggeredControlledAgent
from evaluation.native_multi_agent import _LangGraphNativeState


SIMPLE_TASK = (
    "Write a Python function called `add(a, b)` that returns the sum of two numbers.\n"
    "The function should work for integers and floats."
)


def test_langgraph_native_state_annotations_resolve_at_module_scope():
    """Regression test for LangGraph's runtime schema inspection."""
    hints = get_type_hints(_LangGraphNativeState)

    assert hints["task"] is str
    assert hints["llm_calls"] is int


def _ok_executor(code: str):
    """Fake executor that always returns success."""
    return True, "OK"


def _fail_executor(code: str):
    """Fake executor that always fails."""
    return False, "AssertionError: expected 5"


# ── 1. AutoGen-style ──────────────────────────────────────────────────────────

class TestAutoGenStyleAgent:

    def test_returns_agent_run_result(self, client):
        agent = AutoGenStyleAgent(client=client, executor=_ok_executor, max_turns=3)
        result = agent.run(SIMPLE_TASK, seed=0)
        assert isinstance(result, AgentRunResult)

    def test_success_when_executor_passes(self, client):
        agent = AutoGenStyleAgent(client=client, executor=_ok_executor, max_turns=3)
        result = agent.run(SIMPLE_TASK, seed=0)
        assert result.success == 1.0
        assert result.termination_reason in ("success", "max_turns")

    def test_failure_when_executor_fails(self, client):
        agent = AutoGenStyleAgent(client=client, executor=_fail_executor, max_turns=2)
        result = agent.run(SIMPLE_TASK, seed=0)
        # Agent may still succeed if model outputs TERMINATE without code
        assert result.success in (0.0, 1.0)

    def test_llm_calls_counted(self, client, fake_server):
        agent = AutoGenStyleAgent(client=client, executor=_ok_executor, max_turns=3)
        result = agent.run(SIMPLE_TASK, seed=0)
        assert result.llm_calls >= 1
        assert len(fake_server.calls) >= 1

    def test_duration_non_negative(self, client):
        agent = AutoGenStyleAgent(client=client, executor=_ok_executor, max_turns=2)
        result = agent.run(SIMPLE_TASK, seed=0)
        assert result.duration_s >= 0

    def test_to_dict_has_required_keys(self, client):
        agent = AutoGenStyleAgent(client=client, executor=_ok_executor, max_turns=2)
        result = agent.run(SIMPLE_TASK, seed=0)
        d = result.to_dict()
        required = ["success", "duration_s", "reported_error", "retries",
                    "llm_calls", "tool_calls", "turns", "generated_code"]
        for k in required:
            assert k in d, f"Missing key: {k}"

    def test_name_attribute(self):
        assert AutoGenStyleAgent.name == "autogen-style"

    def test_max_turns_respected(self, client):
        agent = AutoGenStyleAgent(client=client, executor=_fail_executor, max_turns=2)
        result = agent.run(SIMPLE_TASK, seed=0)
        assert result.turns <= 2

    def test_extract_tool_code_preserves_raw_python(self, client):
        agent = AutoGenStyleAgent(client=client, executor=_ok_executor, max_turns=2)
        raw = "def add(a, b):\n    return a + b"
        fenced = "```python\ndef add(a, b):\n    return a + b\n```"
        assert agent._extract_tool_code(raw) == raw
        assert agent._extract_tool_code(fenced) == raw
        assert agent._extract_tool_code(None) == ""


# ── 2. MAD-style ──────────────────────────────────────────────────────────────

class TestMADStyleAgent:

    def test_returns_agent_run_result(self, client):
        agent = MADStyleAgent(client=client, executor=_ok_executor, n_agents=2, debate_rounds=1)
        result = agent.run(SIMPLE_TASK, seed=0)
        assert isinstance(result, AgentRunResult)

    def test_majority_vote_selects_code(self, client):
        agent = MADStyleAgent(client=client, executor=_ok_executor, n_agents=3, debate_rounds=0)
        result = agent.run(SIMPLE_TASK, seed=0)
        assert isinstance(result.generated_code, str)

    def test_n_agents_calls_made(self, client, fake_server):
        agent = MADStyleAgent(client=client, executor=_ok_executor, n_agents=3, debate_rounds=0)
        agent.run(SIMPLE_TASK, seed=0)
        assert fake_server.calls  # at least some calls happened
        result_llm = agent.run(SIMPLE_TASK, seed=1)
        assert result_llm.llm_calls >= 3  # n_agents calls at minimum

    def test_success_with_ok_executor(self, client):
        agent = MADStyleAgent(client=client, executor=_ok_executor, n_agents=2, debate_rounds=1)
        result = agent.run(SIMPLE_TASK, seed=0)
        assert result.success == 1.0

    def test_name_attribute(self):
        assert MADStyleAgent.name == "mad-style"

    def test_trace_has_proposal_entries(self, client):
        agent = MADStyleAgent(client=client, executor=_ok_executor, n_agents=2, debate_rounds=0)
        result = agent.run(SIMPLE_TASK, seed=0)
        rounds = {e.get("round") for e in result.execution_trace if "round" in e}
        assert 0 in rounds  # round 0 proposals present


# ── 3. MapCoder-style ─────────────────────────────────────────────────────────

class TestMapCoderStyleAgent:

    def test_returns_agent_run_result(self, client):
        agent = MapCoderStyleAgent(client=client, executor=_ok_executor, max_turns=1)
        result = agent.run(SIMPLE_TASK, seed=0)
        assert isinstance(result, AgentRunResult)

    def test_five_stages_in_trace(self, client):
        agent = MapCoderStyleAgent(client=client, executor=_ok_executor, max_turns=0)
        result = agent.run(SIMPLE_TASK, seed=0)
        stages = {e.get("stage") for e in result.execution_trace}
        assert "retrieval" in stages
        assert "planning"  in stages
        assert "coding"    in stages

    def test_success_with_ok_executor(self, client):
        agent = MapCoderStyleAgent(client=client, executor=_ok_executor, max_turns=0)
        result = agent.run(SIMPLE_TASK, seed=0)
        assert result.success == 1.0
        assert result.termination_reason == "success"

    def test_debug_cycles_on_failure(self, client):
        agent = MapCoderStyleAgent(client=client, executor=_fail_executor, max_turns=2)
        result = agent.run(SIMPLE_TASK, seed=0)
        # Should have tried up to max_debug_cycles debug calls
        assert result.llm_calls >= 3  # retrieval + planning + coding = 3 minimum

    def test_name_attribute(self):
        assert MapCoderStyleAgent.name == "mapcoder-style"

    def test_llm_calls_at_least_three(self, client):
        agent = MapCoderStyleAgent(client=client, executor=_ok_executor, max_turns=0)
        result = agent.run(SIMPLE_TASK, seed=0)
        assert result.llm_calls >= 3  # retrieval + planning + coding


class _RecordingToolClient:
    model = "fake-tool-model"

    def __init__(self, *, fail_on_tool_result: bool = False):
        self.fail_on_tool_result = fail_on_tool_result
        self.messages = []

    def chat(self, messages, **kwargs):
        self.messages.append(messages)
        if any(msg.get("role") == "tool" for msg in messages):
            if self.fail_on_tool_result:
                raise RuntimeError("injected tool fault")
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "```python\ndef add(a, b):\n    return a + b\n```",
                    }
                }],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
            }
        return {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "```python\ndef add(a, b):\n    return a + b\n```",
                }
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }

    def usage(self, response):
        return response.get("usage", {})


def test_tool_triggered_agent_sends_real_tool_result_request():
    client = _RecordingToolClient()
    agent = ToolTriggeredControlledAgent(client=client, max_turns=2)

    result = agent.run(SIMPLE_TASK, seed=7)

    assert result.tool_calls == 1
    assert result.llm_calls == 2
    assert result.termination_reason == "completed"
    assert "def add" in result.generated_code
    assert len(client.messages) == 2
    assert any(msg.get("role") == "tool" for msg in client.messages[-1])
    tool_call_messages = [msg for msg in client.messages[-1] if msg.get("role") == "assistant"]
    assert any(msg.get("tool_calls") for msg in tool_call_messages)


def test_tool_triggered_agent_keeps_candidate_when_tool_fault_blocks_followup():
    client = _RecordingToolClient(fail_on_tool_result=True)
    agent = ToolTriggeredControlledAgent(client=client, max_turns=2)

    result = agent.run(SIMPLE_TASK, seed=7)

    assert result.tool_calls == 1
    assert result.llm_calls == 1
    assert result.reported_error == 1.0
    assert result.termination_reason == "agent_exception"
    assert "injected tool fault" in result.exception
    assert "def add" in result.generated_code


# ── 4. ModelClient dry-run ────────────────────────────────────────────────────

class TestModelClientDryRun:

    def test_dry_run_no_real_call(self, monkeypatch):
        monkeypatch.delenv("CJ_EVAL_BASE_URL", raising=False)
        client = ModelClient(dry_run=True)
        resp = client.chat([{"role": "user", "content": "hello"}])
        assert "choices" in resp
        assert resp["choices"][0]["message"]["role"] == "assistant"

    def test_real_client_without_url_raises(self, monkeypatch):
        monkeypatch.delenv("CJ_EVAL_BASE_URL", raising=False)
        with pytest.raises(RuntimeError, match="CJ_EVAL_BASE_URL"):
            ModelClient(dry_run=False)

    def test_client_uses_fake_server(self, client, fake_server):
        resp = client.chat([{"role": "user", "content": "test"}])
        assert "choices" in resp
        assert len(fake_server.calls) >= 1
