from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from chaos_jungle.faults.base import VerificationResult
from chaos_jungle.scripts.llm_proxy import llm_proxy
from chaos_jungle.targets.docker import DockerContainerControllerTarget, DockerTarget
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
from evaluation.multi_agent import TraceEvent, validate_event_schema
from evaluation.output import generate_all_outputs
from evaluation.study_protocol import (
    PublicationStudyOrchestrator,
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
    rows = (tmp_path / "runs.jsonl").read_text().strip().splitlines()
    assert len(rows) == 1
    persisted = json.loads(rows[0])
    assert persisted["study_id"] == "study-x"
    assert persisted["success"] == 1.0


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
