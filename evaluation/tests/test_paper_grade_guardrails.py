"""Paper-grade guardrails beyond the gateway smoke tests.

These tests intentionally avoid Docker and paid model calls.  They encode the
scientific interpretation rules that must hold before the paper can claim tool,
semantic, infrastructure, distributed, and provenance support.
"""

from __future__ import annotations

import csv
import json

from chaos_jungle.scripts.llm_proxy import llm_proxy
from evaluation.docker_runner import DockerAgentRunner, DockerExecutionConfig
from evaluation.output import (
    write_distributed_process_summary_csv,
    write_fault_fidelity_summary_csv,
    write_study_manifest,
)
from evaluation.tests.test_output import _make_exact_triplet_records


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
