from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from chaos_jungle.faults.base import VerificationResult
from chaos_jungle.faults.llm import LLMLatency
from chaos_jungle.scripts.llm_proxy import llm_proxy
from chaos_jungle.targets.docker import DockerContainerControllerTarget, DockerTarget
from evaluation.agents.base import ModelClient, extract_python_code
from evaluation.analysis.statistics import (
    holm_correction,
    mcnemar_exact,
    paired_permutation_test,
    task_clustered_bootstrap_ci,
)
from evaluation.container_entrypoint import main as container_main
from evaluation.costing import estimate_condition_count, estimate_cost_usd, pilot_power_recommendation
from evaluation.docker_runner import (
    DockerAgentRunResult,
    DockerAgentRunner,
    DockerExecutionConfig,
    PreparedContainer,
    RunContext,
    redact_secrets,
)
from evaluation.infrastructure_orchestrator import run_container_scoped_fault
from evaluation.benchmarks.base import BenchmarkTask
from evaluation.multi_agent import LINEAR, MultiAgentWorkflow, TraceEvent, validate_event_schema
from evaluation.output import generate_all_outputs
from evaluation.run import _publication_fault_names, build_parser, run_publication_study
from evaluation.schema import OUTPUT_SCHEMA_VERSION
from evaluation.study_protocol import (
    PublicationStudyOrchestrator,
    _classify_lifecycle,
    _merge_runner_lifecycle,
    _proxy_call_evidence,
    default_experiment_definition,
    make_pair_id,
)


def test_secret_redaction_nested_values():
    value = {
        "api_key": "sk-secret",
        "api_key_env": "OPENAI_API_KEY",
        "max_tokens": 2048,
        "input_price_per_1k_tokens": 0.01,
        "nested": {"token": "Bearer abc", "safe": "ok"},
        "list": [{"password": "pw"}],
    }
    assert redact_secrets(value) == {
        "api_key": "[REDACTED]",
        "api_key_env": "OPENAI_API_KEY",
        "max_tokens": 2048,
        "input_price_per_1k_tokens": 0.01,
        "nested": {"token": "[REDACTED]", "safe": "ok"},
        "list": [{"password": "[REDACTED]"}],
    }


def test_tool_fault_lifecycle_targets_only_tool_requests():
    cfg = {"fault": "tool_fault", "tool_name": ""}
    non_tool = {"model": "x", "messages": [{"role": "user", "content": "hello"}]}
    tool = {"model": "x", "messages": [{"role": "tool", "name": "python", "content": "42"}]}

    non_tool_records = llm_proxy._build_lifecycle_chain(
        [cfg],
        [],
        {},
        call_index=1,
        req_body=non_tool,
        request_meta={"run_id": "r1", "agent_role": "coder", "step": "1"},
    )
    tool_records = llm_proxy._build_lifecycle_chain(
        [cfg],
        ["tool_fault"],
        {"tool_fault": {"http_status": 400}},
        call_index=2,
        req_body=tool,
        request_meta={"run_id": "r1", "agent_role": "coder", "step": "2"},
    )

    assert non_tool_records[0]["target_matched"] is False
    assert tool_records[0]["target_matched"] is True
    assert tool_records[0]["target"]["agent_role"] == "coder"
    assert tool_records[0]["target"]["step"] == "2"


def test_publication_lifecycle_marks_intercepted_tool_fault_as_untriggered():
    class FakeDB:
        def get_session(self, session_id):
            return {"verdict": "valid"}

        def get_llm_calls(self, session_id, phase):
            assert phase == "fault"
            return [{
                "was_blocked": 0,
                "was_modified": 0,
                "fault_triggered": 0,
                "triggered_faults_json": "[]",
                "configured_faults_json": '["tool_fault"]',
                "fault_evidence_json": json.dumps([{
                    "fault_type": "tool_fault",
                    "target_matched": False,
                    "triggered": False,
                    "applied": False,
                    "manifested": False,
                    "evidence": {"observed": "not triggered"},
                }]),
            }]

        def export_session(self, session_id):
            return {"faults": [{
                "kind": "ToolFault",
                "status": "reverted",
                "verified_active": True,
                "verified_recovered": True,
            }]}

    class FakeRunner:
        _session_id = 123
        db = FakeDB()

    lifecycle = {
        "configured": True,
        "activated": None,
        "triggered": None,
        "manifested": None,
        "reverted": None,
        "recovered": None,
        "verdict": "inconclusive",
    }

    _merge_runner_lifecycle(lifecycle, FakeRunner())

    assert lifecycle["proxy_call_count"] == 1
    assert lifecycle["activated"] is True
    assert lifecycle["triggered"] is False
    assert lifecycle["manifested"] is False
    assert lifecycle["reverted"] is True
    assert lifecycle["recovered"] is True
    assert _classify_lifecycle(lifecycle) == "untriggered"


def test_proxy_call_evidence_lifts_trace_metadata_without_full_payloads():
    lifecycle = [{
        "target": {"run_id": "run-1", "agent_role": "reviewer", "step": "3"},
        "evidence": {"request_meta": {"run_id": "run-1", "agent_role": "reviewer", "step": "3"}},
    }]
    evidence = _proxy_call_evidence({
        "id": 9,
        "phase": "fault",
        "call_index": 2,
        "fault_evidence_json": json.dumps(lifecycle),
        "prompt_text": "secret prompt",
        "response_text": "secret response",
        "full_messages_json": "[large payload]",
    })

    assert evidence["run_id"] == "run-1"
    assert evidence["agent_role"] == "reviewer"
    assert evidence["step"] == "3"
    assert evidence["prompt_text"]["preview"] == "secret prompt"
    assert evidence["response_text"]["length"] == len("secret response")


def test_docker_target_executes_only_inside_validated_container(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "inspect":
            return subprocess.CompletedProcess(cmd, 0, "true\n", "")
        return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr("subprocess.run", fake_run)

    target = DockerTarget("abc123", timeout_s=3)
    code, out, err = target.run("echo hello")
    assert code == 0
    assert out == "ok\n"
    assert err == ""
    assert calls[-1][:4] == ["docker", "exec", "abc123", "/bin/sh"]


def test_docker_target_rejects_unsafe_host_control():
    target = DockerTarget("abc123")
    target._connected = True
    with pytest.raises(ValueError):
        target.run("docker ps")


def test_container_controller_allows_only_exact_experiment_container():
    target = DockerContainerControllerTarget("abc123")
    args, fallback = target._parse_allowed("docker inspect --format='{{.State.Status}}' abc123 2>/dev/null || echo missing")
    assert args[-1] == "abc123"
    assert fallback == "missing"
    with pytest.raises(ValueError):
        target._parse_allowed("docker kill other-container")


def test_docker_runner_create_command_rejects_secret_env(tmp_path, monkeypatch):
    runner = DockerAgentRunner(DockerExecutionConfig(image="cj:test"))
    with pytest.raises(ValueError):
        runner._create_command("name", tmp_path / "in", tmp_path / "out", {"OPENAI_API_KEY": "secret"})


def test_docker_runner_image_digest_uses_inspect_format_before_image(monkeypatch):
    runner = DockerAgentRunner(DockerExecutionConfig(image="cj:test"))
    calls = []

    def fake_run(cmd, timeout):
        calls.append(cmd)
        if "{{json .RepoDigests}}" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "[]\n", "")
        return subprocess.CompletedProcess(cmd, 0, "sha256:test\n", "")

    monkeypatch.setattr(runner, "_run", fake_run)

    assert runner._image_digest("cj:test") == "sha256:test"
    assert calls == [
        ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", "cj:test"],
        ["docker", "image", "inspect", "--format", "{{.Id}}", "cj:test"],
    ]


def test_docker_runner_prepare_writes_redacted_request(tmp_path, monkeypatch):
    runner = DockerAgentRunner(DockerExecutionConfig(image="cj:test"))
    monkeypatch.setattr(runner, "_require_docker", lambda: None)
    monkeypatch.setattr(runner, "_image_digest", lambda image: "sha256:test")

    def fake_run(cmd, timeout):
        if cmd[1] == "create":
            return subprocess.CompletedProcess(cmd, 0, "container123\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(runner, "_run", fake_run)
    ctx = RunContext(
        study_id="study",
        campaign_id="campaign",
        pair_id="pair",
        run_id="run",
        condition="direct_baseline",
        output_root=str(tmp_path),
    )
    container = runner.prepare(
        agent_system="autogen",
        topology="single",
        task={"prompt": "solve"},
        seed=1,
        model_config={"api_key": "sk-secret", "name": "model"},
        execution_config={"dry_run": True},
        run_context=ctx,
    )
    req = json.loads((tmp_path / "input" / "request.json").read_text())
    assert req["model_config"]["api_key"] == "[REDACTED]"
    assert container.image_digest == "sha256:test"


def test_docker_runner_prepare_resolves_relative_output_root(tmp_path, monkeypatch):
    runner = DockerAgentRunner(DockerExecutionConfig(image="cj:test"))
    monkeypatch.setattr(runner, "_require_docker", lambda: None)
    monkeypatch.setattr(runner, "_image_digest", lambda image: "sha256:test")
    monkeypatch.chdir(tmp_path)

    def fake_run(cmd, timeout):
        if cmd[1] == "create":
            mount_args = [cmd[i + 1] for i, item in enumerate(cmd) if item == "--mount"]
            assert any("source=" + str(tmp_path / "relative-out" / "input") in arg for arg in mount_args)
            assert any("source=" + str(tmp_path / "relative-out" / "output") in arg for arg in mount_args)
            return subprocess.CompletedProcess(cmd, 0, "container123\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(runner, "_run", fake_run)
    ctx = RunContext(
        study_id="study",
        campaign_id="campaign",
        pair_id="pair",
        run_id="run",
        condition="direct_baseline",
        output_root="relative-out",
    )
    container = runner.prepare(
        agent_system="autogen",
        topology="single",
        task={"prompt": "solve"},
        seed=1,
        model_config={"name": "model"},
        execution_config={"dry_run": True},
        run_context=ctx,
    )
    assert container.input_dir == str(tmp_path / "relative-out" / "input")
    assert container.output_dir == str(tmp_path / "relative-out" / "output")


def test_docker_runner_prepare_retries_missing_bind_source(tmp_path, monkeypatch):
    runner = DockerAgentRunner(DockerExecutionConfig(image="cj:test"))
    monkeypatch.setattr(runner, "_require_docker", lambda: None)
    monkeypatch.setattr(runner, "_image_digest", lambda image: "sha256:test")
    calls = {"create": 0}

    def fake_run(cmd, timeout):
        if cmd[1] == "create":
            calls["create"] += 1
            if calls["create"] == 1:
                return subprocess.CompletedProcess(
                    cmd,
                    1,
                    "",
                    'Error response from daemon: invalid mount config for type "bind": '
                    f"bind source path does not exist: {tmp_path / 'run' / 'input'}",
                )
            return subprocess.CompletedProcess(cmd, 0, "container123\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(runner, "_run", fake_run)
    ctx = RunContext(
        study_id="study",
        campaign_id="campaign",
        pair_id="pair",
        run_id="run",
        condition="direct_baseline",
        output_root=str(tmp_path / "run"),
    )
    container = runner.prepare(
        agent_system="autogen",
        topology="single",
        task={"prompt": "solve"},
        seed=1,
        model_config={"name": "model"},
        execution_config={"dry_run": True},
        run_context=ctx,
    )
    assert calls["create"] == 2
    assert container.container_id == "container123"
    assert (tmp_path / "run" / "input" / ".cj_mount_probe").exists()


def test_docker_runner_prepare_falls_back_to_tmp_staging_for_unmountable_results(
    tmp_path,
    monkeypatch,
):
    runner = DockerAgentRunner(DockerExecutionConfig(image="cj:test"))
    monkeypatch.setattr(runner, "_require_docker", lambda: None)
    monkeypatch.setattr(runner, "_image_digest", lambda image: "sha256:test")
    calls = {"create": 0}
    requested = tmp_path / "run"

    def fake_run(cmd, timeout):
        if cmd[1] == "create":
            calls["create"] += 1
            mount_args = [cmd[i + 1] for i, item in enumerate(cmd) if item == "--mount"]
            if any(str(requested / "input") in arg for arg in mount_args):
                return subprocess.CompletedProcess(
                    cmd,
                    1,
                    "",
                    'Error response from daemon: invalid mount config for type "bind": '
                    f"bind source path does not exist: {requested / 'input'}",
                )
            return subprocess.CompletedProcess(cmd, 0, "container123\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(runner, "_run", fake_run)
    ctx = RunContext(
        study_id="study",
        campaign_id="campaign",
        pair_id="pair",
        run_id="run",
        condition="direct_baseline",
        output_root=str(requested),
    )
    container = runner.prepare(
        agent_system="autogen",
        topology="single",
        task={"prompt": "solve"},
        seed=1,
        model_config={"name": "model"},
        execution_config={"dry_run": True},
        run_context=ctx,
    )
    assert calls["create"] == 6
    assert container.container_id == "container123"
    assert container.staging_root
    assert container.input_dir != str(requested / "input")
    assert container.artifact_input_dir == str(requested / "input")
    assert (requested / "input" / "request.json").exists()


def test_proxy_selector_matching_and_header_stripping():
    class Headers(dict):
        def items(self):
            return super().items()

    headers = Headers({
        "Content-Type": "application/json",
        "X-CJ-Run-ID": "run1",
        "X-CJ-Agent-Role": "planner",
        "X-CJ-Step": "2",
    })
    meta = llm_proxy._request_meta(headers)
    assert meta == {"run_id": "run1", "agent_role": "planner", "step": "2"}
    assert llm_proxy._selector_matches({"selector": {"agent_role": "planner"}}, meta)
    assert not llm_proxy._selector_matches({"selector": {"agent_role": "coder"}}, meta)
    assert not llm_proxy._selector_matches({"selector": {"unknown": "x"}}, meta)
    fwd = llm_proxy._build_fwd_headers(headers, b"{}")
    assert "X-CJ-Agent-Role" not in fwd
    assert "X-CJ-Run-ID" not in fwd
    assert fwd["Content-Length"] == "2"


def test_study_pair_id_is_stable_and_specific():
    a = make_pair_id(
        study_id="s", framework="autogen", topology="linear", task_id="t", seed=1,
        fault_name="llm_latency", fault_parameters={"delay_s": 0.5}, repetition=0,
    )
    b = make_pair_id(
        study_id="s", framework="autogen", topology="linear", task_id="t", seed=1,
        fault_name="llm_latency", fault_parameters={"delay_s": 0.5}, repetition=0,
    )
    c = make_pair_id(
        study_id="s", framework="langgraph", topology="linear", task_id="t", seed=1,
        fault_name="llm_latency", fault_parameters={"delay_s": 0.5}, repetition=0,
    )
    d = make_pair_id(
        study_id="s", framework="autogen", topology="linear", task_id="t", seed=1,
        fault_name="llm_unavailable", fault_parameters={}, repetition=0,
    )
    e = make_pair_id(
        study_id="s", framework="autogen", topology="linear", task_id="t", seed=1,
        fault_name="llm_latency", fault_parameters={"delay_s": 0.5}, repetition=1,
    )
    assert a == b
    assert a != c
    assert a != d
    assert a != e


def test_costing_helpers_are_explicit():
    executions = estimate_condition_count(
        tasks_per_benchmark=2,
        repetitions=1,
        frameworks=3,
        individual_faults=2,
        multi_agent_faults=1,
        multi_agent_topologies=2,
    )
    assert executions == 144
    assert estimate_cost_usd(
        executions=10,
        avg_prompt_tokens=1000,
        avg_completion_tokens=500,
        llm_calls_per_execution=2,
        input_price_per_1k=0.001,
        output_price_per_1k=0.002,
    ) == pytest.approx(0.04)
    rec = pilot_power_recommendation(
        pilot_task_count=5,
        observed_sd=0.2,
        minimum_detectable_effect=0.1,
    )
    assert rec["recommended_tasks"] >= 5


def test_publication_fault_all_expands_to_connected_catalog():
    args = build_parser().parse_args([
        "--publication-study",
        "--docker-image", "cj:test",
        "--system", "autogen-real",
        "--benchmark", "humanevalplus",
        "--fault", "all",
    ])
    assert _publication_fault_names(args) == [
        "llm_timeout",
        "llm_rate_limit",
        "llm_unavailable",
        "response_truncation",
        "malformed_response",
        "false_response",
        "tool_failure",
        "token_starvation",
        "llm_latency",
    ]


def test_publication_fault_suites_and_comma_lists_are_validated():
    suite_args = build_parser().parse_args([
        "--publication-study",
        "--docker-image", "cj:test",
        "--system", "autogen-real",
        "--benchmark", "humanevalplus",
        "--fault-suite", "llm_api",
    ])
    assert _publication_fault_names(suite_args) == [
        "llm_latency",
        "llm_timeout",
        "llm_rate_limit",
        "llm_unavailable",
    ]

    response_args = build_parser().parse_args([
        "--publication-study",
        "--docker-image", "cj:test",
        "--system", "autogen-real",
        "--benchmark", "humanevalplus",
        "--fault-suite", "response",
    ])
    assert _publication_fault_names(response_args) == [
        "response_truncation",
        "malformed_response",
        "false_response",
        "token_starvation",
    ]

    semantic_args = build_parser().parse_args([
        "--publication-study",
        "--docker-image", "cj:test",
        "--system", "autogen-real",
        "--benchmark", "humanevalplus",
        "--fault-suite", "semantic",
    ])
    assert _publication_fault_names(semantic_args) == [
        "false_response",
        "generated_false_response",
    ]

    semantic_static_args = build_parser().parse_args([
        "--publication-study",
        "--docker-image", "cj:test",
        "--system", "autogen-real",
        "--benchmark", "humanevalplus",
        "--fault-suite", "semantic_static",
    ])
    assert _publication_fault_names(semantic_static_args) == ["false_response"]

    semantic_generated_args = build_parser().parse_args([
        "--publication-study",
        "--docker-image", "cj:test",
        "--system", "autogen-real",
        "--benchmark", "humanevalplus",
        "--fault-suite", "semantic_generated",
    ])
    assert _publication_fault_names(semantic_generated_args) == ["generated_false_response"]

    ma_args = build_parser().parse_args([
        "--publication-study",
        "--docker-image", "cj:test",
        "--system", "autogen-real",
        "--benchmark", "humanevalplus",
        "--fault-suite", "multi_agent",
    ])
    assert _publication_fault_names(ma_args) == [
        "planner_llm_unavailable",
        "reviewer_response_corrupt",
        "coder_tool_fault",
    ]

    ma_semantic_args = build_parser().parse_args([
        "--publication-study",
        "--docker-image", "cj:test",
        "--system", "autogen-real",
        "--benchmark", "humanevalplus",
        "--fault-suite", "multi_agent_semantic",
    ])
    assert _publication_fault_names(ma_semantic_args) == [
        "reviewer_false_response",
        "reviewer_generated_false_response",
    ]

    list_args = build_parser().parse_args([
        "--publication-study",
        "--docker-image", "cj:test",
        "--system", "autogen-real",
        "--benchmark", "humanevalplus",
        "--fault", "llm_latency,llm_unavailable",
    ])
    assert _publication_fault_names(list_args) == ["llm_latency", "llm_unavailable"]


def test_generated_false_response_defaults_to_current_model_endpoint(monkeypatch):
    from evaluation.experiments.fault_campaign import build_cj_fault

    monkeypatch.setenv("CJ_EVAL_BASE_URL", "https://ellm.example/v1")
    monkeypatch.setenv("CJ_EVAL_MODEL", "minimax-m2")

    fault = build_cj_fault("generated_false_response")

    assert fault.generator_url == "https://ellm.example"
    assert fault.generator_model == "minimax-m2"
    assert fault._parameters()["generator_temperature"] == 0.7


def test_publication_fault_resolution_rejects_ambiguous_or_empty_selection():
    both_args = build_parser().parse_args([
        "--publication-study",
        "--docker-image", "cj:test",
        "--system", "autogen-real",
        "--benchmark", "humanevalplus",
        "--fault", "llm_latency",
        "--fault-suite", "smoke",
    ])
    with pytest.raises(SystemExit):
        _publication_fault_names(both_args)

    none_args = build_parser().parse_args([
        "--publication-study",
        "--docker-image", "cj:test",
        "--system", "autogen-real",
        "--benchmark", "humanevalplus",
        "--fault", "none",
    ])
    with pytest.raises(SystemExit):
        _publication_fault_names(none_args)


def test_publication_study_runs_selected_fault_suite_once_per_fault(tmp_path, monkeypatch):
    task = BenchmarkTask(
        task_id="toy/0",
        benchmark="toy",
        prompt="Write solution.",
        entry_point="solution",
        test_code="assert solution() is None\n",
        metadata={"source": "bundled"},
    )
    calls = []
    output_calls = []

    class FakeOrchestrator:
        def __init__(self, docker_runner, *, study_id=None, results_dir="results", proxy_port=18000):
            self.study_id = study_id or "study-suite"
            self.results_dir = results_dir
            self.proxy_port = proxy_port

        def run_pair(self, **kwargs):
            calls.append((kwargs["fault_name"], kwargs["fault"], kwargs["task"].task_id))
            return []

    monkeypatch.setattr("evaluation.run._load_task_subset", lambda *args, **kwargs: [task])
    monkeypatch.setattr("evaluation.docker_runner.DockerAgentRunner", lambda cfg: object())
    monkeypatch.setattr("evaluation.study_protocol.PublicationStudyOrchestrator", FakeOrchestrator)
    monkeypatch.setattr("evaluation.experiments.fault_campaign.build_cj_fault", lambda name: f"fault:{name}")
    monkeypatch.setattr(
        "evaluation.output.generate_all_outputs",
        lambda results_dir, study_id=None: output_calls.append((results_dir, study_id)),
    )

    args = build_parser().parse_args([
        "--publication-study",
        "--docker-image", "cj:test",
        "--system", "autogen-real",
        "--benchmark", "humanevalplus",
        "--fault", "llm_latency,llm_unavailable",
        "--results-dir", str(tmp_path),
    ])
    run_publication_study(args)

    assert calls == [
        ("llm_latency", "fault:llm_latency", "toy/0"),
        ("llm_unavailable", "fault:llm_unavailable", "toy/0"),
    ]
    assert output_calls == [(str(tmp_path), "study-suite")]


def test_multi_agent_event_schema():
    event = TraceEvent(
        study_id="s",
        run_id="r",
        pair_id="p",
        agent_role="planner",
        step_index=0,
        timestamp=1.0,
        trace_id="trace",
        event_type="agent_activation",
    ).to_dict()
    validate_event_schema(event)
    event.pop("trace_id")
    with pytest.raises(ValueError):
        validate_event_schema(event)


def test_extract_python_code_handles_leading_and_malformed_fences():
    assert extract_python_code("  ```python\n\ndef solution():\n    return 1\n```") == (
        "def solution():\n    return 1"
    )
    assert extract_python_code("```pythondef solution():\n    return 2\n```") == (
        "def solution():\n    return 2"
    )
    assert extract_python_code("Here is the answer:\ndef solution():\n    return 3") == (
        "def solution():\n    return 3"
    )


def test_model_client_forwards_cj_trace_headers(monkeypatch):
    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "ok"}}],
                "usage": {},
            }).encode()

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setenv("CJ_EVAL_BASE_URL", "http://proxy.example/v1")
    monkeypatch.setenv("CJ_EVAL_API_KEY", "dummy")
    monkeypatch.setenv("CJ_EVAL_MODEL", "fake-model")
    monkeypatch.setenv("CJ_RUN_ID", "run-123")
    monkeypatch.setenv("CJ_AGENT_ROLE", "planner")
    monkeypatch.setenv("CJ_STEP", "7")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    response = ModelClient().chat([{"role": "user", "content": "hello"}])

    assert response["choices"][0]["message"]["content"] == "ok"
    assert seen["url"] == "http://proxy.example/v1/chat/completions"
    assert seen["headers"]["x-cj-run-id"] == "run-123"
    assert seen["headers"]["x-cj-agent-role"] == "planner"
    assert seen["headers"]["x-cj-step"] == "7"


def test_multi_agent_workflow_sets_role_metadata_and_extracts_code(monkeypatch):
    monkeypatch.delenv("CJ_RUN_ID", raising=False)
    monkeypatch.delenv("CJ_AGENT_ROLE", raising=False)
    monkeypatch.delenv("CJ_STEP", raising=False)

    class CapturingClient:
        def __init__(self):
            self.calls = []

        def complete(self, messages, seed=None):
            self.calls.append({
                "run_id": os.environ.get("CJ_RUN_ID"),
                "role": os.environ.get("CJ_AGENT_ROLE"),
                "step": os.environ.get("CJ_STEP"),
                "seed": seed,
            })
            if len(self.calls) == 1:
                return "Plan the solution."
            if len(self.calls) == 2:
                return "```pythondef solution():\n    return None\n```"
            return "ACCEPT"

    client = CapturingClient()
    workflow = MultiAgentWorkflow(
        client,
        topology=LINEAR,
        study_id="study-1",
        pair_id="pair-1",
        run_id="run-1",
    )
    result = workflow.run("Write solution.", seed=13)

    assert result.generated_code == "def solution():\n    return None"
    assert [call["role"] for call in client.calls] == ["planner", "coder", "reviewer"]
    assert [call["step"] for call in client.calls] == ["0", "1", "2"]
    assert all(call["run_id"] == "run-1" for call in client.calls)
    assert all(call["seed"] == 13 for call in client.calls)
    assert os.environ.get("CJ_AGENT_ROLE") is None


def test_paired_statistics_helpers():
    baseline = [
        {"pair_id": "p1", "task_id": "t1", "success": 1.0, "duration_s": 1.0},
        {"pair_id": "p2", "task_id": "t2", "success": 0.0, "duration_s": 2.0},
        {"pair_id": "p3", "task_id": "t2", "success": 1.0, "duration_s": 3.0},
    ]
    fault = [
        {"pair_id": "p1", "task_id": "t1", "success": 0.0, "duration_s": 2.0},
        {"pair_id": "p2", "task_id": "t2", "success": 1.0, "duration_s": 1.0},
    ]
    mcn = mcnemar_exact(baseline, fault)
    assert mcn["n_pairs"] == 2
    assert mcn["missing_pairs"] == 1
    perm = paired_permutation_test(baseline, fault, metric="duration_s")
    assert perm["n_pairs"] == 2
    boot = task_clustered_bootstrap_ci(baseline, fault, metric="duration_s", iterations=20)
    assert boot["n_tasks"] == 2
    assert holm_correction([0.01, 0.04, None])[:2] == [0.02, 0.04]


def test_generate_outputs_filters_exact_study_id(tmp_path, monkeypatch):
    records = [
        {
            "study_id": "selected",
            "run_id": "r1",
            "campaign_id": "c1",
            "pair_id": "p1",
            "condition": "direct_baseline",
            "agent_system": "a",
            "benchmark": "b",
            "task_id": "t",
            "phase": "baseline",
            "fault_type": "none",
            "success": 1.0,
            "duration_s": 1.0,
        },
        {
            "study_id": "other",
            "run_id": "r2",
            "campaign_id": "c2",
            "pair_id": "p2",
            "condition": "direct_baseline",
            "agent_system": "a",
            "benchmark": "b",
            "task_id": "old",
            "phase": "baseline",
            "fault_type": "none",
            "success": 0.0,
            "duration_s": 2.0,
        },
        {"run_id": "legacy", "campaign_id": "", "task_id": "legacy"},
    ]
    with (tmp_path / "runs.jsonl").open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")

    monkeypatch.setattr("evaluation.analysis.plots.plot_degradation", lambda *a, **k: None)
    monkeypatch.setattr("evaluation.analysis.plots.plot_validity_summary", lambda *a, **k: None)
    generate_all_outputs(str(tmp_path), study_id="selected")
    task_csv = (tmp_path / "task_results.csv").read_text()
    assert "selected" in task_csv
    assert "other" not in task_csv
    assert "legacy" not in task_csv
    assert (tmp_path / "inferential_summary.csv").exists()
    manifest = json.loads((tmp_path / "study_manifest.json").read_text())
    assert manifest["record_count"] == 1
    assert manifest["output_schema_version"] == OUTPUT_SCHEMA_VERSION
    assert manifest["output_schema_status"] == "frozen"


def test_container_entrypoint_dry_run_individual(tmp_path, monkeypatch):
    monkeypatch.delenv("CJ_EVAL_BASE_URL", raising=False)
    req = {
        "agent_system": "autogen",
        "topology": "single",
        "task": {
            "task_id": "toy/0",
            "benchmark": "toy",
            "prompt": "Write a function named solution returning None.",
            "entry_point": "solution",
            "test_code": "assert solution() is None\n",
            "metadata": {"source": "bundled"},
        },
        "seed": 0,
        "model_config": {"name": "fake", "base_url": "http://example.invalid/v1"},
        "execution_config": {"dry_run": True, "agent_level": "individual", "score_timeout_s": 2},
        "run_context": {"study_id": "s", "pair_id": "p", "run_id": "r"},
    }
    request_path = tmp_path / "request.json"
    output_path = tmp_path / "result.json"
    request_path.write_text(json.dumps(req), encoding="utf-8")
    code = container_main(["--request", str(request_path), "--output", str(output_path)])
    assert code == 0
    result = json.loads(output_path.read_text())
    assert result["executor_status"] == "ok"
    assert result["agent_level"] == "individual"
    assert result["scorer_status"] == "ok"
    assert result["success"] == 1.0
    assert result["tests_total"] == 1
    assert os.environ["CJ_EVAL_BASE_URL"] == "http://example.invalid/v1"
    assert (tmp_path / "execution_trace.json").exists()


def test_container_entrypoint_keeps_condition_specific_base_url(tmp_path, monkeypatch):
    monkeypatch.setenv("CJ_EVAL_BASE_URL", "http://condition-proxy.invalid/v1")
    req = {
        "agent_system": "autogen",
        "topology": "single",
        "task": {
            "task_id": "toy/0",
            "benchmark": "toy",
            "prompt": "Write a function named solution returning None.",
            "entry_point": "solution",
            "test_code": "assert solution() is None\n",
            "metadata": {"source": "bundled"},
        },
        "seed": 0,
        "model_config": {"name": "fake", "base_url": "http://direct-upstream.invalid/v1"},
        "execution_config": {"dry_run": True, "agent_level": "individual", "score_timeout_s": 2},
        "run_context": {"study_id": "s", "pair_id": "p", "run_id": "r"},
    }
    request_path = tmp_path / "request.json"
    output_path = tmp_path / "result.json"
    request_path.write_text(json.dumps(req), encoding="utf-8")
    code = container_main(["--request", str(request_path), "--output", str(output_path)])
    assert code == 0
    assert os.environ["CJ_EVAL_BASE_URL"] == "http://condition-proxy.invalid/v1"


def test_publication_condition_flattens_scores_and_persists(tmp_path):
    class FakeRunner:
        config = DockerExecutionConfig(image="cj:test")

        def run(self, **kwargs):
            return DockerAgentRunResult(
                container_id="abc123",
                exit_code=0,
                timed_out=False,
                duration_s=0.5,
                stdout="",
                stderr="",
                result={
                    "generated_code": "def solution():\n    return None\n",
                    "duration_s": 0.4,
                    "llm_calls": 1,
                    "success": 1.0,
                    "tests_passed": 1,
                    "tests_total": 1,
                    "scorer_status": "ok",
                    "scorer_error": "",
                    "scoring_evidence": {"strict_evalplus": False},
                    "termination_reason": "completed",
                },
                image="cj:test",
                image_digest="sha256:test",
                resource_limits={"cpus": 1.0, "memory": "2g"},
                artifact_paths={"result_json": "/tmp/result.json"},
            )

    task = BenchmarkTask(
        task_id="toy/0",
        benchmark="toy",
        prompt="Write solution.",
        entry_point="solution",
        test_code="assert solution() is None\n",
        metadata={"source": "bundled"},
    )
    orch = PublicationStudyOrchestrator(FakeRunner(), study_id="study-x", results_dir=str(tmp_path))
    record = orch._run_condition(
        condition="direct_baseline",
        agent_system="autogen",
        topology="single",
        task={
            "task_id": task.task_id,
            "benchmark": task.benchmark,
            "prompt": task.prompt,
            "entry_point": task.entry_point,
            "test_code": task.test_code,
            "metadata": task.metadata,
        },
        seed=0,
        model_config={"name": "fake", "base_url": "http://fake/v1"},
        execution_config={"agent_level": "individual", "score_timeout_s": 2},
        campaign_id="campaign-x",
        pair_id="pair-x",
        extra_env={"CJ_EVAL_BASE_URL": "http://fake/v1"},
        definition=default_experiment_definition("llm_latency"),
        benchmark_task=task,
        fault_name="none",
        fault_parameters={},
        lifecycle={"verdict": "valid"},
        validity="valid",
    )
    assert record["success"] == 1.0
    assert record["tests_total"] == 1
    assert record["llm_calls"] == 1
    assert record["executor_success_verdict"] == "success"
    assert record["scorer_success_verdict"] == "success"
    assert record["task_success_verdict"] == "success"
    assert record["operational_success"] is True
    assert record["continued_operation"] is True
    assert record["agent_detected_fault"] is False
    assert record["runner_observed_failure"] is False
    assert record["failure_detection_source"] == "none"
    assert record["silent_failure"] is False
    assert record["cj_source_state"] in {"clean", "dirty", "unknown"}
    assert record["output_schema_version"] == OUTPUT_SCHEMA_VERSION
    rows = (tmp_path / "runs.jsonl").read_text().strip().splitlines()
    assert len(rows) == 1
    persisted = json.loads(rows[0])
    assert persisted["study_id"] == "study-x"
    assert persisted["success"] == 1.0
    assert persisted["task_success_verdict"] == "success"
    assert persisted["operational_success"] is True
    assert persisted["output_schema_version"] == OUTPUT_SCHEMA_VERSION


def test_publication_condition_fails_closed_without_container_scoring(tmp_path):
    class FakeRunner:
        config = DockerExecutionConfig(image="cj:test")

        def run(self, **kwargs):
            return DockerAgentRunResult(
                container_id="abc123",
                exit_code=0,
                timed_out=False,
                duration_s=0.5,
                stdout="",
                stderr="",
                result={
                    "generated_code": "def solution():\n    return None\n",
                    "duration_s": 0.4,
                    "llm_calls": 1,
                    "termination_reason": "completed",
                },
                image="cj:test",
                image_digest="sha256:test",
                resource_limits={"cpus": 1.0, "memory": "2g"},
                artifact_paths={"result_json": "/tmp/result.json"},
            )

    task = BenchmarkTask(
        task_id="toy/0",
        benchmark="toy",
        prompt="Write solution.",
        entry_point="solution",
        test_code="assert solution() is None\n",
        metadata={"source": "bundled"},
    )
    orch = PublicationStudyOrchestrator(FakeRunner(), study_id="study-x", results_dir=str(tmp_path))
    record = orch._run_condition(
        condition="direct_baseline",
        agent_system="autogen",
        topology="single",
        task={
            "task_id": task.task_id,
            "benchmark": task.benchmark,
            "prompt": task.prompt,
            "entry_point": task.entry_point,
            "test_code": task.test_code,
            "metadata": task.metadata,
        },
        seed=0,
        model_config={"name": "fake", "base_url": "http://fake/v1"},
        execution_config={"agent_level": "individual", "score_timeout_s": 2},
        campaign_id="campaign-x",
        pair_id="pair-x",
        extra_env={"CJ_EVAL_BASE_URL": "http://fake/v1"},
        definition=default_experiment_definition("llm_latency"),
        benchmark_task=task,
        fault_name="none",
        fault_parameters={},
        lifecycle={"verdict": "valid"},
        validity="valid",
    )
    assert record["success"] == 0.0
    assert record["tests_total"] == 0
    assert record["scorer_status"] == "error"
    assert "host fallback scoring is disabled" in record["scorer_error"]
    assert record["scoring_evidence"]["score_location"] == "container"
    assert record["scoring_evidence"]["valid"] is False
    assert record["executor_success_verdict"] == "success"
    assert record["scorer_success_verdict"] == "failure"
    assert record["task_success_verdict"] == "failure"
    assert record["operational_success"] is True
    assert record["continued_operation"] is True
    assert record["runner_observed_failure"] is False
    assert record["failure_detection_source"] == "none"
    assert record["silent_failure"] is True


def test_failed_multi_agent_fault_record_uses_reference_labels(tmp_path):
    class FakeRunner:
        config = DockerExecutionConfig(image="cj:test")

    task = BenchmarkTask(
        task_id="toy/0",
        benchmark="toy",
        prompt="Write solution.",
        entry_point="solution",
        test_code="assert solution() is None\n",
        metadata={"source": "bundled"},
    )
    orch = PublicationStudyOrchestrator(FakeRunner(), study_id="study-x", results_dir=str(tmp_path))
    record = orch._failed_fault_record(
        condition="cj_fault",
        agent_system="autogen-real",
        topology="linear",
        task={
            "task_id": task.task_id,
            "benchmark": task.benchmark,
            "prompt": task.prompt,
            "entry_point": task.entry_point,
            "test_code": task.test_code,
            "metadata": task.metadata,
        },
        seed=0,
        agent_level="multi_agent",
        model_config={"name": "fake", "base_url": "http://fake/v1"},
        campaign_id="campaign-x",
        pair_id="pair-x",
        fault_name="planner_llm_unavailable",
        fault_parameters={"selector": {"agent_role": "planner"}},
        lifecycle={"verdict": "invalid"},
        definition=default_experiment_definition("planner_llm_unavailable"),
        error="activation failed",
    )

    assert record["agent_system"] == "reference-multi-agent"
    assert record["framework"] == "reference-multi-agent"
    assert record["requested_framework"] == "autogen-real"
    assert record["framework_native"] is False
    assert record["multi_agent_impl"] == "reference-multi-agent"
    assert record["validity"] == "invalid"


def test_publication_pair_splits_host_and_container_routing(tmp_path, monkeypatch):
    class FakeRunner:
        config = DockerExecutionConfig(image="cj:test")

        def __init__(self):
            self.envs = []

        def run(self, **kwargs):
            self.envs.append(dict(kwargs["run_context"].environment))
            return DockerAgentRunResult(
                container_id="abc123",
                exit_code=0,
                timed_out=False,
                duration_s=0.5,
                stdout="",
                stderr="",
                result={
                    "generated_code": "def solution():\n    return None\n",
                    "duration_s": 0.4,
                    "llm_calls": 1,
                    "success": 1.0,
                    "tests_passed": 1,
                    "tests_total": 1,
                    "scorer_status": "ok",
                    "scorer_error": "",
                    "scoring_evidence": {"strict_evalplus": False},
                },
                image="cj:test",
                image_digest="sha256:test",
                resource_limits={"cpus": 1.0, "memory": "2g"},
                artifact_paths={"result_json": "/tmp/result.json"},
            )

    class FakeDB:
        def get_session(self, session_id):
            return {"verdict": "valid"}

        def get_llm_calls(self, session_id, phase):
            return [{
                "was_blocked": 0,
                "was_modified": 1,
                "triggered_faults_json": '["llm_latency"]',
                "configured_faults_json": '["llm_latency"]',
                "fault_evidence_json": '{"delay_s": 0.1}',
            }]

        def export_session(self, session_id):
            return {
                "faults": [{
                    "kind": "llm_latency",
                    "status": "reverted",
                    "verified_active": True,
                    "verified_recovered": True,
                    "verification_note": "ok",
                }]
            }

    class FakeChaosRunner:
        def __init__(self, *args, **kwargs):
            self._session_id = "session-1"
            self.db = FakeDB()

        def start(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr("evaluation.study_protocol.ChaosRunner", FakeChaosRunner)

    task = BenchmarkTask(
        task_id="toy/0",
        benchmark="toy",
        prompt="Write solution.",
        entry_point="solution",
        test_code="assert solution() is None\n",
        metadata={"source": "bundled"},
    )
    fake_runner = FakeRunner()
    orch = PublicationStudyOrchestrator(
        fake_runner,
        study_id="study-x",
        results_dir=str(tmp_path),
        proxy_port=18099,
    )
    monkeypatch.setattr(orch, "_start_passthrough_proxy", lambda model_config, **kwargs: object())
    monkeypatch.setattr(orch, "_stop_process", lambda proc: None)
    monkeypatch.setattr(orch, "_wait_proxy_down", lambda *args, **kwargs: None)
    fault = LLMLatency(
        delay_s=0.1,
        upstream="http://wrong-upstream.invalid",
        base_url_env="OPENAI_BASE_URL",
    )
    records = orch.run_pair(
        agent_system="autogen",
        agent_level="individual",
        topology="single",
        task=task,
        seed=0,
        model_config={"name": "fake", "base_url": "http://127.0.0.1:9999/v1"},
        execution_config={"score_timeout_s": 2},
        fault_name="llm_latency",
        fault=fault,
        container_direct_base_url="http://host.docker.internal:9999/v1",
    )
    assert fake_runner.envs[0]["CJ_EVAL_BASE_URL"] == "http://host.docker.internal:9999/v1"
    assert fake_runner.envs[1]["CJ_EVAL_BASE_URL"] == "http://host.docker.internal:18099/v1"
    assert fake_runner.envs[2]["CJ_EVAL_BASE_URL"] == "http://host.docker.internal:18100/v1"
    assert fault.port == 18100
    assert fault.upstream == "http://127.0.0.1:9999"
    assert fault.base_url_env == "CJ_EVAL_BASE_URL"
    assert records[0]["routing"]["host_upstream_base_url"] == "http://127.0.0.1:9999/v1"
    assert records[0]["routing"]["container_direct_base_url"] == "http://host.docker.internal:9999/v1"
    assert records[0]["routing"]["container_control_proxy_base_url"] == "http://host.docker.internal:18099/v1"
    assert records[0]["routing"]["container_fault_proxy_base_url"] == "http://host.docker.internal:18100/v1"
    assert records[2]["fault_parameters"]["port"] == 18100
    assert records[2]["fault_parameters"]["upstream"] == "http://127.0.0.1:9999"
    assert records[2]["validity"] == "valid"


def test_container_scoped_fault_orchestration_sequence(monkeypatch):
    events = []

    class FakeFault:
        def start(self, target):
            events.append(("start", target.container_id))

        def stop(self, target):
            events.append(("stop", target.container_id))

        def verify_active(self, target):
            events.append(("verify_active", target.container_id))
            return VerificationResult(verified=True, reason="ok", observed={"state": "ok"})

        def verify_recovered(self, target):
            events.append(("verify_recovered", target.container_id))
            return VerificationResult(verified=True, reason="ok", observed={"state": "ok"})

    class FakeRunner:
        def prepare(self, **kwargs):
            events.append(("prepare", kwargs["run_context"].run_id))
            return PreparedContainer(
                container_id="abc123",
                image="image",
                image_digest="sha256:test",
                input_dir="/tmp/in",
                output_dir="/tmp/out",
                request_path="/cj/input/request.json",
                resource_limits={},
                created_at=0.0,
            )

        def start_container(self, container):
            events.append(("start_container", container.container_id))
            return subprocess.CompletedProcess(["docker", "start", container.container_id], 0, "", "")

        def exec_agent(self, container):
            events.append(("exec", container.container_id))
            return object()

        def collect(self, execution):
            events.append(("collect", "done"))
            return DockerAgentRunResult(
                container_id="abc123",
                exit_code=0,
                timed_out=False,
                duration_s=1.0,
                stdout="",
                stderr="",
                result={"success": 1.0},
                image="image",
                image_digest="sha256:test",
                resource_limits={},
                artifact_paths={},
            )

        def cleanup(self, container):
            events.append(("cleanup", container.container_id))

    monkeypatch.setattr("evaluation.infrastructure_orchestrator.DockerTarget.connect", lambda self: setattr(self, "_connected", True))
    monkeypatch.setattr("evaluation.infrastructure_orchestrator.DockerTarget.disconnect", lambda self: setattr(self, "_connected", False))
    result = run_container_scoped_fault(
        docker_runner=FakeRunner(),
        fault=FakeFault(),
        agent_system="autogen",
        topology="single",
        task={"prompt": "x"},
        seed=0,
        model_config={},
        execution_config={},
        run_context=RunContext(
            study_id="s",
            campaign_id="c",
            pair_id="p",
            run_id="r",
            condition="cj_fault",
        ),
    )
    assert result.lifecycle["verdict"] == "valid"
    assert events == [
        ("prepare", "r"),
        ("start_container", "abc123"),
        ("start", "abc123"),
        ("verify_active", "abc123"),
        ("exec", "abc123"),
        ("collect", "done"),
        ("stop", "abc123"),
        ("verify_recovered", "abc123"),
        ("cleanup", "abc123"),
    ]
