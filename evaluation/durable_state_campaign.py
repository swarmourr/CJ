"""Docker-scoped durable-state corruption validation.

This campaign is intentionally separate from stateless agent runs.  It validates
the CJ ``JsonStateCorrupt`` lifecycle against a real checkpoint file inside a
disposable Docker container, then verifies detection and recovery.  It does not
claim durable-agent resilience for frameworks that are not actually using
persistent checkpoints.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from chaos_jungle.faults.state import JsonStateCorrupt
from chaos_jungle.targets import DockerTarget
from evaluation.docker_runner import DockerAgentRunner, DockerExecutionConfig, RunContext
from evaluation.experiments.protocol import _CJ_COMMIT, _CJ_SOURCE_PROVENANCE
from evaluation.schema import OUTPUT_SCHEMA_VERSION
from evaluation.study_protocol import default_experiment_definition, make_pair_id


STATE_PATH = "/tmp/cj_durable_state/checkpoint.json"
EXPECTED_CONTENT = "planner approved implementation plan"
CORRUPTED_CONTENT = "chaos-jungle:corrupted-checkpoint"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-image", required=True)
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--study-id")
    parser.add_argument("--cpus", type=float, default=1.0)
    parser.add_argument("--memory", default="1g")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--env-file", default=os.environ.get("CJ_EVAL_ENV_FILE"))
    return parser


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _target_json(target: DockerTarget) -> dict[str, Any]:
    code, stdout, stderr = target.run(f"cat '{STATE_PATH}'")
    if code != 0:
        raise RuntimeError(f"could not read checkpoint: {stderr or stdout}")
    return json.loads(stdout)


def _write_checkpoint(target: DockerTarget) -> str:
    checkpoint = {
        "checkpoint_id": "ckpt-1",
        "cursor": 1,
        "messages": [
            {
                "role": "planner",
                "content": EXPECTED_CONTENT,
            }
        ],
        "status": "ready_to_resume",
    }
    payload = json.dumps(checkpoint, indent=2, sort_keys=True)
    code, stdout, stderr = target.run(
        "mkdir -p /tmp/cj_durable_state && "
        f"python3 -c \"import sys; open('{STATE_PATH}', 'w').write(sys.stdin.read())\" "
        f"<< 'CJ_STATE_EOF'\n{payload}\nCJ_STATE_EOF"
    )
    if code != 0:
        raise RuntimeError(f"could not write checkpoint: {stderr or stdout}")
    return payload


def _resume_validation(state: dict[str, Any]) -> tuple[bool, str]:
    try:
        content = state["messages"][0]["content"]
    except Exception as exc:  # noqa: BLE001
        return False, f"checkpoint schema invalid: {exc!r}"
    if content != EXPECTED_CONTENT:
        return False, "checkpoint content mismatch detected"
    return True, "checkpoint content valid"


def _record(
    *,
    study_id: str,
    campaign_id: str,
    pair_id: str,
    run_id: str,
    condition: str,
    fault_type: str,
    validity: str,
    lifecycle: dict[str, Any],
    evidence: dict[str, Any],
    docker_image: str,
    docker_image_digest: str,
    resource_limits: dict[str, Any],
    artifact_paths: dict[str, Any],
    success: float,
    duration_s: float,
) -> dict[str, Any]:
    return {
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "study_id": study_id,
        "campaign_id": campaign_id,
        "pair_id": pair_id,
        "run_id": run_id,
        "condition": condition,
        "phase": "fault" if condition == "cj_fault" else "baseline",
        "agent_level": "durable_state",
        "agent_system": "durable-reference-checkpoint",
        "framework": "durable-reference-checkpoint",
        "framework_native": False,
        "multi_agent_impl": "",
        "deployment_mode": "single_container_checkpoint",
        "topology": "checkpoint_resume",
        "benchmark": "durable_state",
        "task_id": "checkpoint/message-content",
        "seed": 0,
        "model": "none",
        "endpoint_type": "no_model",
        "fault_type": fault_type,
        "fault_layer": "durable_state",
        "fault_parameters": {
            "file_path": STATE_PATH,
            "field_path": "messages[0].content",
            "mutation": "inject",
            "inject_value_hash": _sha256(CORRUPTED_CONTENT),
        },
        "fault_target_role": "",
        "target": "docker",
        "validity": validity,
        "lifecycle": lifecycle,
        "success": success,
        "task_success_verdict": "success" if success == 1.0 else "failure",
        "operational_success": True,
        "continued_operation": True,
        "agent_detected_fault": bool(evidence.get("corruption_detected")),
        "runner_observed_failure": False,
        "failure_detection_source": "checkpoint_validator" if evidence.get("corruption_detected") else "",
        "silent_failure": False,
        "duration_s": duration_s,
        "reported_error": 0.0,
        "termination_reason": "completed",
        "llm_calls": 0,
        "agent_reported_llm_calls": 0,
        "tool_calls": 0,
        "turns": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "tests_passed": 1 if success == 1.0 else 0,
        "tests_total": 1,
        "scorer_status": "ok",
        "scoring_evidence": evidence,
        "activation_evidence": evidence.get("activation_evidence", {}),
        "recovery_evidence": evidence.get("recovery_evidence", {}),
        "experiment_definition": default_experiment_definition("json_state_corrupt").to_dict(),
        "cj_commit": _CJ_COMMIT,
        **_CJ_SOURCE_PROVENANCE,
        "docker_image": docker_image,
        "docker_image_digest": docker_image_digest,
        "container_resource_limits": resource_limits,
        "artifact_paths": artifact_paths,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    study_id = args.study_id or f"durable-study-{uuid.uuid4().hex[:12]}"
    campaign_id = f"durable-json-state-{uuid.uuid4().hex[:8]}"
    pair_id = make_pair_id(
        study_id=study_id,
        framework="durable-reference-checkpoint",
        topology="checkpoint_resume",
        task_id="checkpoint/message-content",
        seed=0,
        fault_name="json_state_corrupt",
        fault_parameters={"field_path": "messages[0].content", "mutation": "inject"},
        repetition=0,
    )

    docker_cfg = DockerExecutionConfig(
        image=args.docker_image,
        cpus=args.cpus,
        memory=args.memory,
        timeout_s=args.timeout,
        env_file=args.env_file,
        preserve_io=True,
    )
    runner = DockerAgentRunner(docker_cfg)
    context = RunContext(
        study_id=study_id,
        campaign_id=campaign_id,
        pair_id=pair_id,
        run_id=f"durable-{uuid.uuid4().hex[:16]}",
        condition="cj_fault",
        output_root=str(results_dir / "durable-json-state"),
    )
    container = runner.prepare(
        agent_system="durable-reference-checkpoint",
        topology="checkpoint_resume",
        task={"task_id": "checkpoint/message-content", "benchmark": "durable_state"},
        seed=0,
        model_config={"name": "none"},
        execution_config={"durable_state": True},
        run_context=context,
    )
    t0 = time.time()
    lifecycle = {
        "configured": True,
        "activated": False,
        "triggered": False,
        "manifested": False,
        "reverted": False,
        "recovered": False,
        "verdict": "inconclusive",
        "evidence_source": "json_state_corrupt_docker_target",
        "timestamps": {"configured": time.time()},
    }
    evidence: dict[str, Any] = {}
    try:
        start_proc = runner.start_container(container)
        if start_proc.returncode != 0:
            raise RuntimeError(start_proc.stderr or start_proc.stdout)
        target = DockerTarget(container.container_id, docker_bin=docker_cfg.docker_bin)
        target.connect()
        try:
            before_payload = _write_checkpoint(target)
            before_hash = _sha256(_json(_target_json(target)))
            valid_before, before_reason = _resume_validation(_target_json(target))
            fault = JsonStateCorrupt(
                STATE_PATH,
                "messages[0].content",
                mutation="inject",
                inject_value=CORRUPTED_CONTENT,
            )
            fault.start(target)
            lifecycle["activated"] = True
            lifecycle["triggered"] = True
            lifecycle["timestamps"]["activated"] = time.time()
            corrupted = _target_json(target)
            after_hash = _sha256(_json(corrupted))
            valid_after, after_reason = _resume_validation(corrupted)
            lifecycle["manifested"] = before_hash != after_hash and not valid_after
            evidence["activation_evidence"] = {
                "before_hash": before_hash,
                "after_hash": after_hash,
                "field_before_hash": _sha256(EXPECTED_CONTENT),
                "field_after_hash": _sha256(str(corrupted["messages"][0]["content"])),
                "valid_before": valid_before,
                "before_reason": before_reason,
                "valid_after": valid_after,
                "after_reason": after_reason,
            }
            evidence["corruption_detected"] = not valid_after
            fault.revert(target)
            lifecycle["reverted"] = True
            lifecycle["timestamps"]["reverted"] = time.time()
            restored = _target_json(target)
            restored_hash = _sha256(_json(restored))
            valid_restored, restored_reason = _resume_validation(restored)
            lifecycle["recovered"] = restored_hash == before_hash and valid_restored
            lifecycle["timestamps"]["recovered"] = time.time()
            lifecycle["verdict"] = (
                "valid"
                if lifecycle["activated"] and lifecycle["triggered"] and lifecycle["manifested"]
                else "invalid"
            )
            evidence["recovery_evidence"] = {
                "restored_hash": restored_hash,
                "valid_restored": valid_restored,
                "restored_reason": restored_reason,
                "matches_before": restored_hash == before_hash,
            }
            evidence["checkpoint_preview"] = {
                "before_bytes": len(before_payload.encode("utf-8")),
                "after_status": corrupted.get("status"),
                "restored_status": restored.get("status"),
            }
        finally:
            target.disconnect()
    finally:
        try:
            runner._sync_staged_io(container)  # noqa: SLF001
        finally:
            runner.cleanup(container)

    duration_s = time.time() - t0
    record = _record(
        study_id=study_id,
        campaign_id=campaign_id,
        pair_id=pair_id,
        run_id=context.run_id,
        condition="cj_fault",
        fault_type="json_state_corrupt",
        validity=str(lifecycle["verdict"]),
        lifecycle=lifecycle,
        evidence=evidence,
        docker_image=container.image,
        docker_image_digest=container.image_digest,
        resource_limits=container.resource_limits,
        artifact_paths={
            "input_dir": container.artifact_input_dir or container.input_dir,
            "output_dir": container.artifact_output_dir or container.output_dir,
        },
        success=1.0 if lifecycle["recovered"] else 0.0,
        duration_s=duration_s,
    )
    with (results_dir / "runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    with (results_dir / "durable_state_summary.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "study_id",
                "validity",
                "activated",
                "triggered",
                "manifested",
                "reverted",
                "recovered",
                "corruption_detected",
                "before_hash",
                "after_hash",
                "restored_hash",
            ],
        )
        writer.writeheader()
        writer.writerow({
            "study_id": study_id,
            "validity": lifecycle["verdict"],
            "activated": lifecycle["activated"],
            "triggered": lifecycle["triggered"],
            "manifested": lifecycle["manifested"],
            "reverted": lifecycle["reverted"],
            "recovered": lifecycle["recovered"],
            "corruption_detected": evidence.get("corruption_detected"),
            "before_hash": evidence.get("activation_evidence", {}).get("before_hash", ""),
            "after_hash": evidence.get("activation_evidence", {}).get("after_hash", ""),
            "restored_hash": evidence.get("recovery_evidence", {}).get("restored_hash", ""),
        })
    print(f"[cj-eval] durable-state smoke complete: {results_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
