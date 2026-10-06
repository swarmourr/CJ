"""Paper-grade guardrails beyond the gateway smoke tests.

These tests intentionally avoid Docker and paid model calls.  They encode the
scientific interpretation rules that must hold before the paper can claim tool,
semantic, infrastructure, distributed, and provenance support.
"""

from __future__ import annotations

import csv
import copy
import inspect
import json

from chaos_jungle.scripts.llm_proxy import llm_proxy
from evaluation.docker_runner import DockerAgentRunner, DockerExecutionConfig
from evaluation.infrastructure_orchestrator import (
    _cpu_manifestation,
    _disk_manifestation,
    _memory_manifestation,
    _network_manifestation,
    run_container_scoped_fault,
)
from evaluation.multi_agent import CLOSED_LOOP, LINEAR, MAX_REVISIONS, MultiAgentWorkflow
from evaluation.output import (
    write_agent_resilience_summary_csv,
    write_condition_summary_csv,
    write_distributed_process_summary_csv,
    write_fault_fidelity_summary_csv,
    write_inferential_summary_csv,
    write_infrastructure_summary_csv,
    write_scientific_summary_csv,
    write_study_manifest,
    write_validity_summary_csv,
)
from evaluation.run import _publication_fault_names, build_parser
from evaluation.tests.test_output import _make_exact_triplet_records


class _FakeTarget:
    def __init__(self, *, run_responses=None, sudo_responses=None):
        self.run_responses = list(run_responses or [])
        self.sudo_responses = list(sudo_responses or [])
        self.run_commands: list[str] = []
        self.sudo_commands: list[str] = []

    def run(self, command: str):
        self.run_commands.append(command)
        if not self.run_responses:
            return 0, "", ""
        response = self.run_responses.pop(0)
        if callable(response):
            return response(command)
        return response

    def sudo(self, command: str):
        self.sudo_commands.append(command)
        if not self.sudo_responses:
            return 0, "", ""
        response = self.sudo_responses.pop(0)
        if callable(response):
            return response(command)
        return response


class _SequentialClient:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.messages: list[list[dict[str, str]]] = []

    def complete(self, messages, seed=0):  # noqa: ARG002
        self.messages.append(messages)
        if not self.responses:
            raise AssertionError("unexpected extra model call")
        return self.responses.pop(0)


def test_tool_fault_reports_precision_coverage_separately_from_affected_fraction(tmp_path):
    records = _make_exact_triplet_records()
    fault = records[2]
    fault["fault_type"] = "tool_failure"
    fault["fault_parameters"] = {"tool_name": "python"}
    fault["fault_target_role"] = "coder"
    fault["lifecycle"]["proxy_call_count"] = 2
    fault["lifecycle"]["proxy_calls_affected_count"] = 1
    fault["lifecycle"]["evidence"]["proxy_calls"] = [
        {
            "call_index": 0,
            "fault_name": "tool_fault",
            "fault_triggered": 0,
            "triggered_faults_json": "[]",
        },
        {
            "call_index": 1,
            "fault_name": "tool_fault",
            "fault_triggered": 1,
            "triggered_faults_json": json.dumps(["tool_fault"]),
        },
    ]
    fault["lifecycle"]["evidence"]["fault_evidence"] = [
        json.dumps([
            {
                "fault_type": "tool_fault",
                "target": {
                    "agent_role": "coder",
                    "operation": "chat.completions",
                    "call_index": 0,
                },
                "target_matched": False,
                "triggered": False,
                "applied": False,
                "manifested": False,
                "evidence": {"observed": "ordinary LLM call"},
            },
            {
                "fault_type": "tool_fault",
                "target": {
                    "agent_role": "coder",
                    "operation": "python",
                    "tool_name": "python",
                    "call_index": 1,
                },
                "target_matched": True,
                "triggered": True,
                "applied": True,
                "manifested": True,
                "evidence": {"http_status": 400, "tool_name": "python"},
            },
        ])
    ]

    path = tmp_path / "fault_fidelity_summary.csv"
    write_fault_fidelity_summary_csv(records, str(path))
    with path.open() as f:
        row = list(csv.DictReader(f))[0]

    assert row["fault"] == "tool_failure"
    assert float(row["affected_call_fraction"]) == 0.5
    assert float(row["selectivity_precision"]) == 1.0
    assert float(row["target_coverage"]) == 1.0
    assert row["tool_call_target"] == "python"


def test_static_false_response_is_deterministic_protocol_valid_and_not_generated():
    body = json.dumps({
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "real answer"},
            "finish_reason": "stop",
        }],
    }).encode()
    cfg = {
        "fault": "corrupt",
        "mode": "false_response",
        "false_text": "false but schema-valid answer",
    }

    triggered_a: list[str] = []
    triggered_b: list[str] = []
    evidence_a: dict = {}
    evidence_b: dict = {}
    mutated_a = llm_proxy._mutate_response(cfg, body, None, triggered_a, evidence_a)
    mutated_b = llm_proxy._mutate_response(cfg, body, None, triggered_b, evidence_b)

    assert mutated_a == mutated_b
    parsed = json.loads(mutated_a)
    assert parsed["choices"][0]["message"]["content"] == "false but schema-valid answer"
    assert triggered_a == ["corrupt"]
    assert evidence_a["corrupt"]["mode"] == "false_response"
    assert evidence_a["corrupt"]["false_text_hash"] == evidence_b["corrupt"]["false_text_hash"]
    assert "generation_source" not in evidence_a["corrupt"]


def test_generated_false_response_records_generator_metadata(monkeypatch):
    def fake_generate(req_body, generator_url, model, *, temperature, seed):
        assert generator_url == "https://generator.example/v1"
        assert model == "fault-generator"
        assert temperature == 0.2
        assert seed == 42
        assert req_body["messages"][0]["content"] == "solve this"
        return "generated false answer"

    monkeypatch.setattr(llm_proxy, "_generate_hallucination", fake_generate)
    body = json.dumps({
        "choices": [{
            "message": {"role": "assistant", "content": "real answer"},
            "finish_reason": "stop",
        }],
    }).encode()
    triggered: list[str] = []
    evidence: dict = {}

    mutated = llm_proxy._mutate_response(
        {
            "fault": "hallucinate",
            "generator_url": "https://generator.example/v1",
            "generator_model": "fault-generator",
            "generator_temperature": 0.2,
            "generator_seed": 42,
        },
        body,
        {"messages": [{"role": "user", "content": "solve this"}]},
        triggered,
        evidence,
    )

    parsed = json.loads(mutated)
    assert parsed["choices"][0]["message"]["content"] == "generated false answer"
    assert triggered == ["hallucinate"]
    assert evidence["hallucinate"]["generation_source"] == "llm_generator"
    assert evidence["hallucinate"]["generator_model"] == "fault-generator"
    assert evidence["hallucinate"]["generator_seed"] == 42
    assert evidence["hallucinate"]["injected_text_hash"]


def test_distributed_summary_reports_containment_when_fault_stays_in_target_role(tmp_path):
    record = _make_exact_triplet_records()[2]
    record.update({
        "deployment_mode": "distributed",
        "agent_level": "multi_agent",
        "fault_target_role": "reviewer",
        "distributed_trace": [
            {
                "event_type": "fault_observed",
                "agent_role": "reviewer",
                "timestamp": 10.0,
                "container_id": "reviewer-c",
            },
            {
                "event_type": "attempted_recovery",
                "agent_role": "reviewer",
                "timestamp": 11.0,
                "container_id": "reviewer-c",
            },
            {
                "event_type": "handoff",
                "agent_role": "coder",
                "timestamp": 12.0,
                "container_id": "coder-c",
            },
        ],
    })

    path = tmp_path / "distributed_process_summary.csv"
    write_distributed_process_summary_csv([record], str(path))
    with path.open() as f:
        row = list(csv.DictReader(f))[0]

    assert row["role_container_count"] == "2"
    assert row["affected_roles"] == '["reviewer"]'
    assert row["fault_propagation_probability"] == "0.0"
    assert row["propagation_depth"] == "0"
    assert row["containment_rate"] == "1.0"
    assert row["repairing_role"] == "reviewer"


def test_distributed_summary_does_not_treat_shared_container_as_container_per_role(tmp_path):
    record = _make_exact_triplet_records()[2]
    record.update({
        "deployment_mode": "distributed",
        "agent_level": "multi_agent",
        "fault_target_role": "planner",
        "distributed_trace": [
            {"event_type": "fault_observed", "agent_role": "planner", "container_id": "shared-c"},
            {"event_type": "handoff", "agent_role": "coder", "container_id": "shared-c"},
            {"event_type": "handoff", "agent_role": "reviewer", "container_id": "shared-c"},
        ],
    })

    path = tmp_path / "distributed_process_summary.csv"
    write_distributed_process_summary_csv([record], str(path))
    with path.open() as f:
        row = list(csv.DictReader(f))[0]

    assert row["role_count"] == "3"
    assert row["role_container_count"] == "1"


def test_publication_docker_command_does_not_mount_docker_socket(tmp_path):
    runner = DockerAgentRunner(DockerExecutionConfig(image="cj:test"))
    cmd = runner._create_command("run-name", tmp_path / "input", tmp_path / "output", {})
    joined = " ".join(cmd)

    assert "/var/run/docker.sock" not in joined
    assert "docker.sock" not in joined


def test_dockerignore_excludes_env_and_result_archives():
    dockerignore = open(".dockerignore", encoding="utf-8").read().splitlines()

    assert ".env" in dockerignore
    assert "results/" in dockerignore
    assert "*.zip" in dockerignore


def test_study_manifest_records_digest_and_never_secret_values(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwxyz"
    records = _make_exact_triplet_records()
    for record in records:
        record["docker_image"] = "cj-eval-agent:test"
        record["docker_image_digest"] = "sha256:testdigest"
        record["container_resource_limits"] = {"cpus": 1, "memory": "2g"}
        record["framework_versions"] = {"python": "3.12"}
        record["fault_parameters"] = {"delay_s": 3.0}
        record["model"] = "minimax-m2"
    records[0]["api_key"] = secret

    path = tmp_path / "study_manifest.json"
    write_study_manifest(records, str(path), "study")
    manifest_text = path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)

    assert manifest["docker_image_digests"] == ["sha256:testdigest"]
    assert manifest["models"] == ["minimax-m2"]
    assert secret not in manifest_text


def _read_rows(path):
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _single_row(path):
    rows = _read_rows(path)
    assert len(rows) == 1
    return rows[0]


# ── Infrastructure fault guardrails ──────────────────────────────────────────


def test_infra_fault_orchestrator_uses_dockertarget_not_localtarget():
    source = inspect.getsource(run_container_scoped_fault)

    assert "DockerTarget" in source
    assert "LocalTarget" not in source


def test_cpu_pressure_manifestation_and_recovery_require_observed_stress_process():
    class Fault:
        _pid_file = "/tmp/stress.pid"

    missing = _cpu_manifestation(_FakeTarget(run_responses=[(0, "\n", "")]), Fault())
    observed = _cpu_manifestation(_FakeTarget(run_responses=[(0, "87.5\n", "")]), Fault())

    assert missing["manifested"] is False
    assert observed["manifested"] is True
    assert observed["observed"]["cpu_percent"] == 87.5


def test_memory_pressure_manifestation_and_recovery_require_observed_allocation():
    missing = _memory_manifestation(_FakeTarget(run_responses=[(0, "\n", "")]))
    observed = _memory_manifestation(_FakeTarget(run_responses=[(0, "67108864\n", "")]))

    assert missing["manifested"] is False
    assert observed["manifested"] is True
    assert observed["observed"]["peak_memory_bytes"] == 67108864


def test_network_latency_and_packet_loss_manifestation_require_container_tc_netem():
    no_netem = _network_manifestation(
        _FakeTarget(
            sudo_responses=[(0, "qdisc noqueue 0: dev eth0 root refcnt 2\n", "")],
            run_responses=[(0, "172.17.0.1\n", ""), (0, "0% packet loss\n", "")],
        ),
        "NetworkDelay",
    )
    with_netem = _network_manifestation(
        _FakeTarget(
            sudo_responses=[(0, "qdisc netem 8001: dev eth0 root delay 100ms loss 5%\n", "")],
            run_responses=[
                (0, "172.17.0.1\n", ""),
                (0, "5 packets transmitted, 5 received, 0% packet loss\nrtt min/avg/max/mdev = 100.0/101.5/103.0/1.0 ms\n", ""),
            ],
        ),
        "NetworkDelay",
    )

    assert no_netem["manifested"] is False
    assert with_netem["manifested"] is True
    assert with_netem["observed"]["latency_ms"] == 101.5
    assert with_netem["observed"]["packet_loss_rate"] == 0.0


def test_disk_full_enospc_manifestation_requires_failed_write_probe():
    class Fault:
        path = "/tmp"

    target = _FakeTarget(
        run_responses=[
            (0, "1048576 0 100%\n", ""),
            (1, "dd: error writing '/tmp/.cj_enospc_probe': No space left on device\n", ""),
        ]
    )

    evidence = _disk_manifestation(target, Fault())

    assert evidence["manifested"] is True
    assert evidence["observed"]["disk_enospc_verified"] is True
    assert evidence["observed"]["disk_write_probe_exit_code"] == 1


def test_infra_fault_record_invalid_without_manifestation(tmp_path):
    record = _make_exact_triplet_records()[2]
    record.update({
        "fault_layer": "infrastructure",
        "fault_type": "cpu_pressure",
        "validity": "invalid",
        "activation_evidence": {"verified": True, "observed": {}},
        "recovery_evidence": {"verified": True, "observed": {}},
        "lifecycle": {
            "configured": True,
            "activated": True,
            "triggered": True,
            "manifested": False,
            "reverted": True,
            "recovered": True,
            "verdict": "invalid",
            "evidence_source": "docker_target",
        },
    })

    path = tmp_path / "infrastructure_summary.csv"
    write_infrastructure_summary_csv([record], str(path))
    row = _single_row(path)

    assert row["validity"] == "invalid"
    assert row["manifested"] == "False"
    assert row["evidence_source"] == "docker_target"
    assert row["observed_cpu"] == ""


# ── Tool-fault and recovery guardrails ───────────────────────────────────────


def test_tool_fault_untriggered_when_workload_never_reaches_tool_call(tmp_path):
    records = _make_exact_triplet_records()
    fault = records[2]
    fault.update({
        "fault_type": "tool_failure",
        "validity": "untriggered",
        "lifecycle": {
            "configured": True,
            "activated": True,
            "triggered": False,
            "manifested": False,
            "reverted": True,
            "recovered": True,
            "verdict": "untriggered",
            "proxy_call_count": 4,
            "proxy_calls_affected_count": 0,
        },
    })

    path = tmp_path / "validity_summary.csv"
    write_validity_summary_csv(records, str(path))
    rows = _read_rows(path)
    row = next(r for r in rows if r["fault_type"] == "tool_failure")

    assert row["n_untriggered"] == "1"
    assert row["n_valid"] == "0"
    assert row["trigger_rate"] == "0.0"


def test_tool_fault_agent_recovery_metrics_are_separate_from_cj_cleanup(tmp_path):
    records = _make_exact_triplet_records()
    fault = records[2]
    fault.update({
        "fault_type": "tool_failure",
        "fault_parameters": {"tool_name": "python"},
        "success": 1.0,
        "reported_error": 0.0,
        "termination_reason": "completed",
        "retries": 1,
        "agent_detected_fault": True,
        "continued_operation": True,
        "silent_failure": False,
        "execution_trace": [
            {"event_type": "error_detection", "agent_role": "coder"},
            {"event_type": "attempted_recovery", "agent_role": "coder"},
        ],
    })

    path = tmp_path / "agent_resilience_summary.csv"
    write_agent_resilience_summary_csv(records, str(path))
    row = _single_row(path)

    assert row["fault"] == "tool_failure"
    assert row["fault_detection_rate"] == "1.0"
    assert row["continued_operation_rate"] == "1.0"
    assert row["retry_repair_rate"] == "1.0"
    assert row["successful_repair_rate"] == "1.0"
    assert row["silent_failure_rate"] == ""


def test_silent_failure_counts_only_task_failure_without_reported_or_detected_error(tmp_path):
    silent_records = _make_exact_triplet_records()
    silent_records[2].update({
        "fault_type": "token_starvation",
        "success": 0.0,
        "reported_error": 0.0,
        "termination_reason": "completed",
        "agent_detected_fault": False,
        "continued_operation": True,
        "silent_failure": True,
    })
    explicit_records = copy.deepcopy(silent_records)
    explicit_records[2]["pair_id"] = "pair-2"
    explicit_records[2]["run_id"] = "f2"
    explicit_records[2]["reported_error"] = 1.0
    explicit_records[2]["termination_reason"] = "agent_exception"
    explicit_records[2]["agent_detected_fault"] = False
    explicit_records[2]["continued_operation"] = False
    explicit_records[2]["silent_failure"] = False
    explicit_records[0]["pair_id"] = "pair-2"
    explicit_records[0]["run_id"] = "b2"
    explicit_records[1]["pair_id"] = "pair-2"
    explicit_records[1]["run_id"] = "c2"

    path = tmp_path / "agent_resilience_summary.csv"
    write_agent_resilience_summary_csv(silent_records + explicit_records, str(path))
    row = _single_row(path)

    assert row["baseline_eligible_pairs"] == "2"
    assert row["silent_failure_rate"] == "0.5"
    assert row["continued_operation_rate"] == "0.5"


# ── Semantic response-fault guardrails ───────────────────────────────────────


def test_false_response_preserves_openai_chat_schema():
    body = json.dumps({
        "id": "chatcmpl-schema",
        "object": "chat.completion",
        "created": 1,
        "model": "model",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "real"},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }).encode()
    triggered: list[str] = []
    evidence: dict = {}

    mutated = llm_proxy._mutate_response(
        {"fault": "corrupt", "mode": "false_response", "false_text": "incorrect answer"},
        body,
        {"messages": []},
        triggered,
        evidence,
    )
    parsed = json.loads(mutated)

    assert parsed["id"] == "chatcmpl-schema"
    assert parsed["object"] == "chat.completion"
    assert parsed["choices"][0]["message"]["role"] == "assistant"
    assert parsed["choices"][0]["message"]["content"] == "incorrect answer"
    assert parsed["usage"]["total_tokens"] == 4


def test_static_vs_generated_false_response_matrix_is_explicit():
    static_args = build_parser().parse_args([
        "--publication-study",
        "--docker-image", "cj:test",
        "--system", "autogen-real",
        "--benchmark", "humanevalplus",
        "--fault-suite", "semantic_static",
    ])
    generated_args = build_parser().parse_args([
        "--publication-study",
        "--docker-image", "cj:test",
        "--system", "autogen-real",
        "--benchmark", "humanevalplus",
        "--fault-suite", "semantic_generated",
    ])
    semantic_args = build_parser().parse_args([
        "--publication-study",
        "--docker-image", "cj:test",
        "--system", "autogen-real",
        "--benchmark", "humanevalplus",
        "--fault-suite", "semantic",
    ])

    assert _publication_fault_names(static_args) == ["false_response"]
    assert _publication_fault_names(generated_args) == ["generated_false_response"]
    assert _publication_fault_names(semantic_args) == [
        "false_response",
        "generated_false_response",
    ]


# ── Multi-agent topology and distributed-process guardrails ─────────────────


def test_linear_topology_has_no_revision_even_when_reviewer_requests_revision():
    client = _SequentialClient([
        "Plan: implement directly.",
        "```python\ndef solution():\n    return None\n```",
        "REVISION_REQUEST change the implementation",
    ])
    workflow = MultiAgentWorkflow(
        client=client,
        topology=LINEAR,
        study_id="study",
        pair_id="pair",
        run_id="run",
    )

    result = workflow.run("Write solution.", seed=1)
    events = result.execution_trace

    assert result.llm_calls == 3
    assert "def solution" in result.generated_code
    assert not any(e["event_type"] == "revision_request" for e in events)
    assert not client.responses


def test_closed_loop_revision_limit_enforced_at_two_rounds():
    client = _SequentialClient([
        "Plan: implement directly.",
        "```python\ndef solution():\n    return 0\n```",
        "REVISION_REQUEST first fix",
        "```python\ndef solution():\n    return 1\n```",
        "REVISION_REQUEST second fix",
        "```python\ndef solution():\n    return None\n```",
        "REVISION_REQUEST third fix should not trigger another coder call",
    ])
    workflow = MultiAgentWorkflow(
        client=client,
        topology=CLOSED_LOOP,
        study_id="study",
        pair_id="pair",
        run_id="run",
    )

    result = workflow.run("Write solution.", seed=1)
    events = result.execution_trace
    revision_events = [e for e in events if e["event_type"] == "revision_request"]
    recovery_events = [e for e in events if e["event_type"] == "attempted_recovery"]

    assert len(revision_events) == MAX_REVISIONS
    assert len(recovery_events) == MAX_REVISIONS
    assert result.llm_calls == 7
    assert result.generated_code.strip().endswith("return None")
    assert not client.responses


def test_multi_agent_trace_events_have_role_step_run_and_pair_identity():
    client = _SequentialClient([
        "Plan.",
        "```python\ndef solution():\n    return None\n```",
        "ACCEPT",
    ])
    workflow = MultiAgentWorkflow(
        client=client,
        topology=LINEAR,
        study_id="study-id",
        pair_id="pair-id",
        run_id="run-id",
    )

    result = workflow.run("Write solution.", seed=3)

    for event in result.execution_trace:
        assert event["study_id"] == "study-id"
        assert event["pair_id"] == "pair-id"
        assert event["run_id"] == "run-id"
        assert event["agent_role"] in {"planner", "coder", "reviewer", "coordinator"}
        assert isinstance(event["step_index"], int)
        assert event["timestamp"]
        assert event["trace_id"]
        assert event["span_id"]


def test_message_bus_records_handoffs_and_message_fault_counts(tmp_path):
    record = _make_exact_triplet_records()[2]
    record.update({
        "deployment_mode": "distributed",
        "agent_level": "multi_agent",
        "fault_target_role": "planner",
        "distributed_trace": [
            {"event_type": "fault_observed", "agent_role": "planner", "timestamp": 1.0, "container_id": "planner-c"},
            {"event_type": "handoff", "agent_role": "planner", "timestamp": 2.0, "container_id": "planner-c"},
            {"event_type": "handoff_failed", "agent_role": "coder", "timestamp": 3.0, "container_id": "coder-c"},
            {"event_type": "message_loss", "agent_role": "coder", "timestamp": 4.0, "container_id": "coder-c"},
            {"event_type": "message_corruption", "agent_role": "reviewer", "timestamp": 5.0, "container_id": "reviewer-c"},
            {"event_type": "message_duplication", "agent_role": "reviewer", "timestamp": 6.0, "container_id": "reviewer-c"},
            {"event_type": "error_detection", "agent_role": "coder", "timestamp": 7.0, "container_id": "coder-c"},
        ],
    })

    path = tmp_path / "distributed_process_summary.csv"
    write_distributed_process_summary_csv([record], str(path))
    row = _single_row(path)

    assert row["successful_handoff_count"] == "1"
    assert row["failed_handoff_count"] == "1"
    assert row["message_loss_count"] == "1"
    assert row["message_corruption_count"] == "1"
    assert row["message_duplication_count"] == "1"
    assert row["fault_propagation_probability"] == "1.0"
    assert row["propagation_depth"] == "1"
    assert row["containment_rate"] == "0.0"
    assert row["detection_role"] == "coder"


# ── Statistical and output guardrails ────────────────────────────────────────


def test_unscored_runs_do_not_enter_pass_at_1_or_inference(tmp_path):
    records = _make_exact_triplet_records()
    for record in records:
        record["scorer_status"] = "error"
        record["tests_total"] = 0
        record["success"] = 0.0

    scientific = tmp_path / "scientific_summary.csv"
    inferential = tmp_path / "inferential_summary.csv"
    condition = tmp_path / "condition_summary.csv"
    write_scientific_summary_csv(records, str(scientific))
    write_inferential_summary_csv(records, str(inferential))
    write_condition_summary_csv(records, str(condition))

    sci = _single_row(scientific)
    inf = next(
        r for r in _read_rows(inferential)
        if r["comparison_type"] == "control_vs_fault"
    )
    fault_condition = next(r for r in _read_rows(condition) if r["fault_type"] == "llm_latency")

    assert sci["scored_triplets"] == "0"
    assert sci["inference_status"] == "no_eligible_scored_pairs"
    assert sci["baseline_pass_at_1"] == ""
    assert inf["test"] == "not_run"
    assert inf["inference_status"] == "no_eligible_scored_pairs"
    assert fault_condition["scored_triplets"] == "0"
    assert fault_condition["pass_at_1_direct_baseline"] == ""


def test_invalid_faults_excluded_from_effect_estimates_but_reported(tmp_path):
    records = _make_exact_triplet_records()
    records[2].update({
        "validity": "invalid",
        "lifecycle": {
            "configured": True,
            "activated": False,
            "triggered": False,
            "manifested": False,
            "reverted": False,
            "recovered": False,
            "verdict": "invalid",
        },
    })

    path = tmp_path / "scientific_summary.csv"
    write_scientific_summary_csv(records, str(path))
    row = _single_row(path)

    assert row["valid_pairs"] == "0"
    assert row["excluded_invalid_count"] == "1"
    assert row["fault_specific_degradation"] == ""
    assert row["fault_pass_at_1"] == ""


def test_control_minus_fault_is_primary_effect_not_direct_minus_fault(tmp_path):
    records = _make_exact_triplet_records()
    records[0]["success"] = 0.0
    records[1]["success"] = 1.0
    records[2]["success"] = 0.0

    path = tmp_path / "condition_summary.csv"
    write_condition_summary_csv(records, str(path))
    row = next(r for r in _read_rows(path) if r["fault_type"] == "llm_latency")

    assert row["total_degradation_direct_minus_fault"] == "0.0"
    assert row["fault_specific_degradation_control_minus_fault"] == "1.0"
    assert row["degradation"] == "1.0"


def test_baseline_and_control_eligible_counts_are_reported_separately(tmp_path):
    records = _make_exact_triplet_records()
    records[0]["success"] = 1.0
    records[1]["success"] = 0.0
    records[2]["success"] = 0.0

    path = tmp_path / "scientific_summary.csv"
    write_scientific_summary_csv(records, str(path))
    row = _single_row(path)

    assert row["baseline_eligible_pairs"] == "1"
    assert row["control_eligible_pairs"] == "0"
    assert row["conditional_robustness"] == "0.0"
    assert row["fault_specific_degradation"] == ""


def test_no_inference_with_low_unique_task_count(tmp_path):
    records = _make_exact_triplet_records()

    path = tmp_path / "scientific_summary.csv"
    write_scientific_summary_csv(records, str(path))
    row = _single_row(path)

    assert row["unique_tasks"] == "1"
    assert row["inference_status"] == "insufficient_unique_tasks"
    assert row["fault_degradation_ci_low"] == ""


def test_holm_correction_per_research_family_is_not_claimed_for_unscored_pairs(tmp_path):
    records = _make_exact_triplet_records()
    for record in records:
        record["scorer_status"] = "error"
        record["tests_total"] = 0

    path = tmp_path / "inferential_summary.csv"
    write_inferential_summary_csv(records, str(path))
    row = next(
        r for r in _read_rows(path)
        if r["comparison_type"] == "control_vs_fault"
    )

    assert row["test"] == "not_run"
    assert row["p_value"] == ""
    assert row.get("p_holm", "") == ""


# ── Provenance and safety guardrails ─────────────────────────────────────────


def test_publication_docker_command_uses_read_only_root_and_tmpfs(tmp_path):
    runner = DockerAgentRunner(DockerExecutionConfig(image="cj:test"))
    cmd = runner._create_command("run-name", tmp_path / "input", tmp_path / "output", {})

    assert "--read-only" in cmd
    assert "--tmpfs" in cmd
    assert any("/tmp" in value for value in cmd)


def test_publication_docker_command_drops_capabilities_by_default(tmp_path):
    runner = DockerAgentRunner(DockerExecutionConfig(image="cj:test"))
    cmd = runner._create_command("run-name", tmp_path / "input", tmp_path / "output", {})

    assert "--cap-drop" in cmd
    assert "ALL" in cmd
    assert "--privileged" not in cmd


def test_infrastructure_network_faults_require_explicit_minimal_capabilities(tmp_path):
    runner = DockerAgentRunner(
        DockerExecutionConfig(
            image="cj:test",
            capabilities=["NET_ADMIN", "NET_RAW"],
        )
    )
    cmd = runner._create_command("run-name", tmp_path / "input", tmp_path / "output", {})

    cap_values = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--cap-add"]
    assert cap_values == ["NET_ADMIN", "NET_RAW"]
    assert "--privileged" not in cmd


def test_result_folders_are_isolated_by_condition_run_id(tmp_path):
    from evaluation.infrastructure_campaign import _context

    ctx_a = _context(tmp_path, "study", "campaign", "pair", "direct_baseline")
    ctx_b = _context(tmp_path, "study", "campaign", "pair", "cj_fault")

    assert ctx_a.output_root != ctx_b.output_root
    assert str(tmp_path) in ctx_a.output_root
    assert "direct_baseline-" in ctx_a.output_root
    assert "cj_fault-" in ctx_b.output_root


def test_one_command_paper_runner_includes_image_debug_and_all_scenarios():
    script = open("evaluation/scripts/run_paper_grade_tests.sh", encoding="utf-8").read()

    assert "--debug-image" in script
    assert "--paper-all" in script
    assert "debug_docker_image.sh" in script
    assert "run_all_current_scenarios.sh" in script


def test_docker_image_debug_script_runs_without_credentials_or_docker_socket_mount():
    script = open("evaluation/scripts/debug_docker_image.sh", encoding="utf-8").read()

    assert "CJ_EVAL_API_KEY" not in script
    assert "LLM_API_KEY" not in script
    assert "/var/run/docker.sock" in script
    assert "-v /var/run/docker.sock" not in script
    assert "--network none" in script
    assert "--read-only" in script
    assert "--cap-drop ALL" in script
    assert "python -m evaluation.container_entrypoint" in script


def test_common_image_requirement_does_not_autobuild_on_docker_daemon_error():
    script = open("evaluation/scripts/common.sh", encoding="utf-8").read()

    assert "Docker image inspection failed before image existence could be verified" in script
    assert "permission denied|cannot connect|docker daemon" in script
    assert "Auto-building missing evaluation image" in script
