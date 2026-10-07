"""Tests for the distributed reference multi-agent architecture."""

from __future__ import annotations

import json
from pathlib import Path

from chaos_jungle.faults.base import VerificationResult
from evaluation.distributed_faults import DistributedFaultPlan, apply_communication_fault
from evaluation.distributed_entrypoint import run_role_request, run_score_request
from evaluation.distributed_runner import DistributedNativeRunner, DistributedReferenceRunner
from evaluation.docker_runner import DockerExecution, DockerExecutionConfig, PreparedContainer, RunContext


def _simple_task() -> dict:
    return {
        "task_id": "toy/add",
        "benchmark": "toy",
        "prompt": "Write add(a, b).",
        "entry_point": "add",
        "test_code": "def check(candidate):\n    assert candidate(2, 3) == 5\ncheck(add)\n",
        "canonical_solution": "def add(a, b):\n    return a + b\n",
        "metadata": {"source": "bundled"},
    }


def test_distributed_role_entrypoint_emits_role_identity_in_dry_run(monkeypatch):
    monkeypatch.setenv("CJ_EVAL_BASE_URL", "http://example.test/v1")
    req = {
        "mode": "role",
        "role": "planner",
        "step_index": 0,
        "trace_id": "trace",
        "messages": [{"role": "user", "content": "Solve it."}],
        "seed": 1,
        "model_config": {"name": "fake", "base_url": "http://example.test/v1"},
        "execution_config": {"dry_run": True},
        "run_context": {"study_id": "study", "pair_id": "pair", "run_id": "run"},
    }

    result = run_role_request(req)

    assert result["executor_status"] == "ok"
    assert result["role"] == "planner"
    assert result["llm_calls"] == 1
    assert result["execution_trace"][0]["study_id"] == "study"
    assert result["execution_trace"][0]["agent_role"] == "planner"
    assert result["execution_trace"][0]["step_index"] == 0


def test_distributed_native_role_entrypoint_uses_native_labels_in_dry_run(monkeypatch):
    monkeypatch.setenv("CJ_EVAL_BASE_URL", "http://example.test/v1")
    req = {
        "mode": "role",
        "agent_system": "autogen-real",
        "role": "coder",
        "step_index": 1,
        "trace_id": "trace",
        "messages": [{"role": "user", "content": "Write add(a, b)."}],
        "seed": 1,
        "model_config": {"name": "fake", "base_url": "http://example.test/v1"},
        "execution_config": {"dry_run": True, "distributed_agent_system": "autogen-real"},
        "run_context": {"study_id": "study", "pair_id": "pair", "run_id": "run"},
    }

    result = run_role_request(req)

    assert result["executor_status"] == "ok"
    assert result["framework"] == "autogen-real"
    assert result["framework_native"] is True
    assert result["deployment_mode"] == "distributed"
    assert result["multi_agent_impl"] == "distributed-native-autogen-real-multi-agent"
    assert result["role"] == "coder"
    assert result["generated_code"]


def test_distributed_score_entrypoint_scores_inside_container_path():
    result = run_score_request({
        "mode": "score",
        "task": _simple_task(),
        "generated_code": "```python\ndef add(a, b):\n    return a + b\n```",
        "execution_config": {"score_timeout_s": 5},
    })

    assert result["executor_status"] == "ok"
    assert result["scorer_status"] == "ok"
    assert result["success"] == 1.0
    assert result["tests_total"] > 0


class _FakeDockerRunner:
    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.prepared: list[PreparedContainer] = []
        self.cleaned: list[str] = []
        self.running: set[str] = set()
        self.requests: list[dict] = []

    def prepare(self, *, run_context: RunContext, **_kwargs):
        role = Path(run_context.output_root).name
        input_dir = Path(run_context.output_root) / "input"
        output_dir = Path(run_context.output_root) / "output"
        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        container = PreparedContainer(
            container_id=f"{role}-container",
            image="cj:test",
            image_digest="sha256:test",
            input_dir=str(input_dir),
            output_dir=str(output_dir),
            request_path="/cj/input/request.json",
            resource_limits={"cpus": 1.0, "memory": "2g"},
            created_at=0.0,
            artifact_input_dir=str(input_dir),
            artifact_output_dir=str(output_dir),
        )
        self.prepared.append(container)
        return container

    def is_running(self, container):
        return container.container_id in self.running

    def start_container(self, container):
        self.running.add(container.container_id)
        return type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def exec_agent(self, container, *, request_path=None, output_path="/cj/output/result.json"):
        request_file = Path(container.input_dir) / Path(request_path or "request.json").name
        output_file = Path(container.output_dir) / Path(output_path).name
        req = json.loads(request_file.read_text(encoding="utf-8"))
        self.requests.append(req)
        mode = req.get("mode")
        if mode == "score":
            payload = {
                "executor_status": "ok",
                "mode": "score",
                "scorer_status": "ok",
                "success": 1.0,
                "tests_passed": 1,
                "tests_total": 1,
                "python_version": "3.12",
                "framework_versions": {"python": "3.12"},
            }
        else:
            role = req["role"]
            if role == "planner":
                content = "Plan: implement add."
            elif role == "coder":
                content = "```python\ndef add(a, b):\n    return a + b\n```"
            else:
                content = "ACCEPT"
            payload = {
                "executor_status": "ok",
                "mode": "role",
                "role": role,
                "step_index": req["step_index"],
                "content": content,
                "generated_code": "def add(a, b):\n    return a + b\n" if role == "coder" else "",
                "llm_calls": 1,
                "turns": 1,
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
                "python_version": "3.12",
                "framework_versions": {"python": "3.12"},
                "execution_trace": [{
                    "study_id": req["run_context"]["study_id"],
                    "run_id": req["run_context"]["run_id"],
                    "pair_id": req["run_context"]["pair_id"],
                    "agent_role": role,
                    "step_index": req["step_index"],
                    "timestamp": 1.0,
                    "trace_id": req["trace_id"],
                    "span_id": f"{role}-span",
                    "event_type": "sent_message",
                    "payload": {},
                }],
            }
        output_file.write_text(json.dumps(payload), encoding="utf-8")
        return DockerExecution(
            container=container,
            exec_command=["docker", "exec"],
            exit_code=0,
            stdout="",
            stderr="",
            timed_out=False,
            duration_s=0.1,
        )

    def _sync_staged_io(self, _container):
        return None

    def cleanup(self, container):
        self.cleaned.append(container.container_id)


def test_distributed_runner_uses_one_container_per_role(tmp_path):
    runner = DistributedReferenceRunner(DockerExecutionConfig(image="cj:test"))
    fake = _FakeDockerRunner(tmp_path)
    runner.docker_runner = fake
    context = RunContext(
        study_id="study",
        campaign_id="campaign",
        pair_id="pair",
        run_id="run",
        condition="direct_baseline",
        output_root=str(tmp_path / "distributed"),
    )

    result = runner.run(
        topology="linear",
        task=_simple_task(),
        seed=1,
        model_config={"name": "fake", "base_url": "http://example.test/v1"},
        execution_config={"dry_run": True, "score_timeout_s": 5},
        run_context=context,
    )

    assert result["deployment_mode"] == "distributed"
    assert result["agent_system"] == "distributed-reference-multi-agent"
    assert result["framework_native"] is False
    assert result["role_container_count"] == 3
    assert set(result["role_container_ids"]) == {"planner", "coder", "reviewer"}
    assert {event["container_id"] for event in result["distributed_trace"] if event.get("container_id")} >= {
        "planner-container",
        "coder-container",
        "reviewer-container",
    }
    assert result["scorer_status"] == "ok"
    assert result["success"] == 1.0
    assert sorted(fake.cleaned) == ["coder-container", "planner-container", "reviewer-container"]


def test_distributed_native_runner_uses_one_container_per_native_role(tmp_path):
    runner = DistributedNativeRunner(DockerExecutionConfig(image="cj:test"), native_system="autogen-real")
    fake = _FakeDockerRunner(tmp_path)
    runner.docker_runner = fake
    context = RunContext(
        study_id="study",
        campaign_id="campaign",
        pair_id="pair",
        run_id="run",
        condition="direct_baseline",
        output_root=str(tmp_path / "distributed-native"),
    )

    result = runner.run(
        topology="linear",
        task=_simple_task(),
        seed=1,
        model_config={"name": "fake", "base_url": "http://example.test/v1"},
        execution_config={"dry_run": True, "score_timeout_s": 5},
        run_context=context,
    )

    assert result["deployment_mode"] == "distributed"
    assert result["agent_system"] == "distributed-native-autogen-real-multi-agent"
    assert result["framework"] == "autogen-real"
    assert result["requested_framework"] == "autogen-real"
    assert result["framework_native"] is True
    assert result["multi_agent_impl"] == "distributed-native-autogen-real-multi-agent"
    assert result["role_container_count"] == 3
    assert set(result["role_container_ids"]) == {"planner", "coder", "reviewer"}
    role_requests = [req for req in fake.requests if req.get("mode") == "role"]
    assert role_requests
    assert {req["agent_system"] for req in role_requests} == {"autogen-real"}
    assert {req["execution_config"]["distributed_agent_system"] for req in role_requests} == {"autogen-real"}


def test_communication_fault_drop_records_missing_handoff():
    plan = DistributedFaultPlan.from_config({
        "fault_type": "message_drop",
        "from_role": "planner",
        "to_role": "coder",
        "severity": "medium",
    })

    deliveries, events = apply_communication_fault(
        plan=plan,
        content="plan",
        from_role="planner",
        to_role="coder",
    )

    assert deliveries == []
    assert plan.applied is True
    assert events[0]["event_type"] == "message_loss"
    assert events[0]["payload"]["from_role"] == "planner"


def test_communication_fault_corruption_mutates_only_matching_handoff():
    plan = DistributedFaultPlan.from_config({
        "fault_type": "message_corruption",
        "from_role": "coder",
        "to_role": "reviewer",
        "replacement": "bad code",
    })

    first, first_events = apply_communication_fault(
        plan=plan,
        content="plan",
        from_role="planner",
        to_role="coder",
    )
    second, second_events = apply_communication_fault(
        plan=plan,
        content="good code",
        from_role="coder",
        to_role="reviewer",
    )

    assert first == [("coder", "plan")]
    assert first_events == []
    assert second == [("reviewer", "bad code")]
    assert second_events[0]["event_type"] == "message_corruption"
    assert second_events[0]["payload"]["original_length"] == len("good code")


def test_distributed_runner_records_communication_fault_lifecycle(tmp_path):
    runner = DistributedReferenceRunner(DockerExecutionConfig(image="cj:test"))
    runner.docker_runner = _FakeDockerRunner(tmp_path)
    context = RunContext(
        study_id="study",
        campaign_id="campaign",
        pair_id="pair",
        run_id="run",
        condition="cj_fault",
        output_root=str(tmp_path / "distributed"),
    )

    result = runner.run(
        topology="linear",
        task=_simple_task(),
        seed=1,
        model_config={"name": "fake", "base_url": "http://example.test/v1"},
        execution_config={
            "dry_run": True,
            "score_timeout_s": 5,
            "distributed_fault": {
                "fault_type": "message_drop",
                "from_role": "planner",
                "to_role": "coder",
            },
        },
        run_context=context,
    )

    lifecycle = result["distributed_fault_lifecycle"]
    assert result["executor_status"] == "error"
    assert lifecycle["activated"] is True
    assert lifecycle["triggered"] is True
    assert lifecycle["manifested"] is True
    assert lifecycle["verdict"] == "valid"
    assert any(e["event_type"] == "message_loss" for e in result["distributed_trace"])
    assert any(e["event_type"] == "handoff_failed" for e in result["distributed_trace"])


def test_distributed_runner_records_role_container_fault(monkeypatch, tmp_path):
    class FakeControllerTarget:
        def __init__(self, container_id, **_kwargs):
            self.container_id = container_id
            self.connected = False

        def connect(self):
            self.connected = True

        def disconnect(self):
            self.connected = False

        def run(self, _cmd):
            return 0, "ok", ""

    class FakeContainerKill:
        def __init__(self, container_id, action="kill"):
            self.container_id = container_id
            self.action = action

        def start(self, _target):
            return None

        def stop(self, _target):
            return None

        def verify_active(self, _target):
            return VerificationResult(verified=True, reason="container exited", observed={"status": "exited"})

        def verify_recovered(self, _target):
            return VerificationResult(verified=True, reason="container running", observed={"status": "running"})

    monkeypatch.setattr("evaluation.distributed_runner.DockerContainerControllerTarget", FakeControllerTarget)
    monkeypatch.setattr("evaluation.distributed_runner.ContainerKill", FakeContainerKill)

    runner = DistributedReferenceRunner(DockerExecutionConfig(image="cj:test"))
    runner.docker_runner = _FakeDockerRunner(tmp_path)
    context = RunContext(
        study_id="study",
        campaign_id="campaign",
        pair_id="pair",
        run_id="run",
        condition="cj_fault",
        output_root=str(tmp_path / "distributed"),
    )

    result = runner.run(
        topology="linear",
        task=_simple_task(),
        seed=1,
        model_config={"name": "fake", "base_url": "http://example.test/v1"},
        execution_config={
            "dry_run": True,
            "score_timeout_s": 5,
            "distributed_fault": {
                "fault_type": "role_container_kill",
                "target_role": "coder",
            },
        },
        run_context=context,
    )

    lifecycle = result["distributed_fault_lifecycle"]
    assert result["executor_status"] == "error"
    assert lifecycle["activated"] is True
    assert lifecycle["triggered"] is True
    assert lifecycle["manifested"] is True
    assert lifecycle["reverted"] is True
    assert lifecycle["recovered"] is True
    assert lifecycle["verdict"] == "valid"
    assert any(e["event_type"] == "fault_observed" and e["agent_role"] == "coder" for e in result["distributed_trace"])
