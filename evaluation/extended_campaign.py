"""Additive paper studies E7 (distributed), E8 (cross-layer), and E9 (performance).

These studies write to their own result roots and never read, mutate, or append
to the 21-study canonical data set.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from chaos_jungle.distributed import Injection
from chaos_jungle.db.session_db import SessionDB
from chaos_jungle.faults.llm import LLMLatency
from chaos_jungle.faults.resources import CPUStress
from chaos_jungle.inject.group import InjectionGroup, InjectionGroupRunner
from chaos_jungle.targets import DockerTarget, LocalTarget
from evaluation.docker_runner import redact_secrets
from evaluation.experiments.protocol import _CJ_COMMIT, _CJ_SOURCE_PROVENANCE
from evaluation.extended_studies import (
    LoadSample,
    RestorationCycle,
    compound_interaction_effect,
    percentile,
    summarize_load,
    summarize_restoration,
)
from evaluation.schema import OUTPUT_SCHEMA_VERSION


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redact_secrets(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(redact_secrets(payload), sort_keys=True) + "\n")


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or f"command failed: {cmd[0]}")
    return proc


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _docker_image_digest(docker_bin: str, image: str) -> str:
    return _run([
        docker_bin,
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        image,
    ]).stdout.strip()


def _create_idle_container(args: argparse.Namespace, role: str) -> str:
    name = f"cj-e{args.study}-{role}-{uuid.uuid4().hex[:8]}"
    cmd = [
        args.docker_bin, "create", "--name", name,
        "--cpus", str(args.cpus), "--memory", args.memory,
        "--read-only", "--tmpfs", "/tmp:rw,nosuid,size=512m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
    ]
    if getattr(args, "env_file", None):
        cmd += ["--env-file", os.path.abspath(args.env_file)]
    cmd += ["--entrypoint", "/bin/sh", args.docker_image, "-c", "sleep 3600"]
    container_id = _run(cmd).stdout.strip()
    if not container_id:
        raise RuntimeError("docker create returned an empty container identifier")
    _run([args.docker_bin, "start", container_id])
    return container_id


def _cleanup_container(docker_bin: str, container_id: str) -> None:
    _run([docker_bin, "rm", "-f", container_id], check=False)


def _group_evidence(
    runner: InjectionGroupRunner,
    start_error: str = "",
    target_metadata: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    try:
        evidence = runner.stop()
        payload = evidence.to_dict()
        for member in payload.get("members", []):
            metadata = (target_metadata or {}).get(member["injection_id"], {})
            member.pop("host", None)
            member.update(metadata)
        payload["group_valid"] = evidence.group_valid
        payload["partial_activation"] = any(m.active_at for m in evidence.members) and not all(
            m.active_at for m in evidence.members
        )
        payload["rollback_success"] = all(m.reverted_at is not None for m in evidence.members)
        payload["verified_recovery"] = (
            payload["rollback_success"] and evidence.verdict != "recovery_invalid"
        )
        payload["start_error"] = start_error
        return payload
    except Exception as exc:  # noqa: BLE001
        return {
            "verdict": "recovery_invalid",
            "group_valid": False,
            "partial_activation": False,
            "rollback_success": False,
            "verified_recovery": False,
            "start_error": start_error,
            "stop_error": repr(exc),
        }


def run_e7(args: argparse.Namespace) -> int:
    results = Path(args.results_dir)
    results.mkdir(parents=True, exist_ok=True)
    roles = [part.strip() for part in args.roles.split(",") if part.strip()]
    if not roles or len(roles) != len(set(roles)):
        raise SystemExit("--roles must contain unique comma-separated role names")
    study_id = f"e7-{uuid.uuid4().hex[:12]}"
    image_digest = _docker_image_digest(args.docker_bin, args.docker_image)
    started_at = _now()
    manifest = {
        "study": "E7-distributed-coordination",
        "study_id": study_id,
        "started_at": started_at,
        "schema": OUTPUT_SCHEMA_VERSION,
        "docker_image": args.docker_image,
        "docker_image_digest": image_digest,
        "cj_commit": _CJ_COMMIT,
        **_CJ_SOURCE_PROVENANCE,
        "roles": roles,
        "cycles": args.cycles,
        "maximum_skew_ms": args.maximum_skew_ms,
        "resource_limits": {"cpus": args.cpus, "memory": args.memory},
        "blast_radius": "named disposable role containers only",
        "canonical_studies_modified": False,
    }
    _write_json(results / "study_manifest.json", manifest)
    records: list[dict[str, Any]] = []

    for cycle in range(args.cycles):
        containers: dict[str, str] = {}
        targets: dict[str, DockerTarget] = {}
        try:
            for role in roles:
                containers[role] = _create_idle_container(args, role)
                targets[role] = DockerTarget(containers[role], timeout_s=30.0)
            injections = [
                Injection(
                    id=f"{role}-cpu",
                    target=targets[role],
                    fault=CPUStress(cores=1, duration_s=args.stress_duration),
                )
                for role in roles
            ]
            target_metadata = {
                f"{role}-cpu": {
                    "target_type": "docker",
                    "target_role": role,
                    "target_id": containers[role],
                }
                for role in roles
            }
            group = InjectionGroup(
                name=f"E7-cycle-{cycle}",
                injections=injections,
                synchronization="scheduled",
                atomic=True,
                maximum_skew_ms=args.maximum_skew_ms,
                start_after=args.start_after,
                on_activation_failure="rollback_all",
                on_skew_violation="rollback_all",
                safety_maximum_duration=args.safety_duration,
            )
            runner = InjectionGroupRunner(group)
            started = time.time()
            start_error = ""
            workload: dict[str, Any] = {}
            try:
                runner.start()
                for role, target in targets.items():
                    code, stdout, stderr = target.run(
                        "python -c 'print(sum(i*i for i in range(250000)))'"
                    )
                    workload[role] = {
                        "exit_code": code,
                        "stdout_preview": stdout.strip()[:120],
                        "stderr_preview": stderr.strip()[:120],
                    }
            except Exception as exc:  # noqa: BLE001
                start_error = repr(exc)
            evidence = _group_evidence(runner, start_error, target_metadata)
            record = {
                "study": "E7-distributed-coordination",
                "study_id": study_id,
                "run_id": f"e7-run-{uuid.uuid4().hex[:12]}",
                "timestamp": _now(),
                "cycle": cycle,
                "condition": f"coordinated_{len(roles)}_target",
                "role_container_ids": containers,
                "target_count": len(roles),
                "docker_image": args.docker_image,
                "docker_image_digest": image_digest,
                "cj_commit": _CJ_COMMIT,
                **_CJ_SOURCE_PROVENANCE,
                "workload": workload,
                "duration_s": time.time() - started,
                **evidence,
            }
            _append_jsonl(results / "runs.jsonl", record)
            records.append(record)
        finally:
            for target in targets.values():
                try:
                    target.disconnect()
                except Exception:
                    pass
            for container_id in containers.values():
                _cleanup_container(args.docker_bin, container_id)
    skews = [float(record["activation_skew_ms"]) for record in records if record.get("activation_skew_ms") is not None]
    summary = {
        "study": "E7-distributed-coordination",
        "study_id": study_id,
        "cycles": len(records),
        "target_count": len(roles),
        "group_valid_rate": sum(bool(record.get("group_valid")) for record in records) / len(records),
        "partial_activation_rate": sum(bool(record.get("partial_activation")) for record in records) / len(records),
        "rollback_success_rate": sum(bool(record.get("rollback_success")) for record in records) / len(records),
        "verified_recovery_rate": sum(bool(record.get("verified_recovery")) for record in records) / len(records),
        "activation_skew_p50_ms": percentile(skews, 0.50),
        "activation_skew_p95_ms": percentile(skews, 0.95),
        "maximum_observed_skew_ms": max(skews) if skews else None,
        "maximum_allowed_skew_ms": args.maximum_skew_ms,
    }
    _write_json(results / "distributed_coordination_summary.json", summary)
    manifest["finished_at"] = _now()
    _write_json(results / "study_manifest.json", manifest)
    print(f"[cj-eval] E7 complete: {results}")
    return 0


def _container_llm_probe(target: DockerTarget, url: str, model: str) -> dict[str, Any]:
    script = """import json
import os
import sys
import urllib.error
import urllib.request

config = json.load(open('/tmp/cj_e8_request.json', encoding='utf-8'))
body = json.dumps({
    'model': config['model'],
    'messages': [{'role': 'user', 'content': 'Reply with OK'}],
    'max_tokens': 8,
}).encode()
request = urllib.request.Request(
    config['url'],
    data=body,
    headers={
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + os.environ.get('LLM_API_KEY', 'dummy'),
    },
)
try:
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read()
        print(json.dumps({'status': response.status, 'response_bytes': len(payload)}))
except urllib.error.HTTPError as exc:
    print(json.dumps({'status': exc.code, 'error': type(exc).__name__}))
    sys.exit(1)
except Exception as exc:
    print(json.dumps({'status': None, 'error': type(exc).__name__}))
    sys.exit(1)
"""
    encoded_script = base64.b64encode(script.encode()).decode("ascii")
    encoded_request = base64.b64encode(
        json.dumps({"url": url, "model": model}).encode()
    ).decode("ascii")
    setup_code, _, setup_error = target.run(
        f"printf '%s' '{encoded_script}' | base64 -d > /tmp/cj_e8_probe.py && "
        f"printf '%s' '{encoded_request}' | base64 -d > /tmp/cj_e8_request.json"
    )
    if setup_code != 0:
        raise RuntimeError(f"failed to stage E8 probe: {setup_error}")
    started = time.monotonic()
    code, stdout, stderr = target.run("python /tmp/cj_e8_probe.py")
    target.run("rm -f /tmp/cj_e8_probe.py /tmp/cj_e8_request.json")
    response: dict[str, Any] = {}
    try:
        response = json.loads(stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        pass
    return {
        "exit_code": code,
        "duration_s": time.monotonic() - started,
        "http_status": response.get("status"),
        "response_bytes": response.get("response_bytes"),
        "error_type": response.get("error"),
        "stdout_preview": stdout.strip()[:300],
        "stderr_preview": stderr.strip()[:300],
    }


def _proxy_evidence(calls: list[dict[str, Any]]) -> dict[str, Any]:
    configured_delay_s = 0.0
    observed_delay_s = 0.0
    affected_calls = 0
    status_counts: dict[str, int] = {}
    previews: list[dict[str, Any]] = []
    for call in calls:
        status = str(call.get("http_status", ""))
        status_counts[status] = status_counts.get(status, 0) + 1
        triggered = bool(call.get("fault_triggered"))
        affected_calls += int(triggered)
        try:
            evidence = json.loads(call.get("fault_evidence_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            evidence = {}
        latency_entries: list[dict[str, Any]] = []
        if isinstance(evidence, dict):
            legacy = evidence.get("latency", {})
            if isinstance(legacy, dict):
                latency_entries.append(legacy)
        elif isinstance(evidence, list):
            for entry in evidence:
                if not isinstance(entry, dict) or entry.get("fault_type") != "latency":
                    continue
                details = entry.get("evidence", {})
                if isinstance(details, dict):
                    latency_entries.append(details)
        for latency in latency_entries:
            configured_delay_s += float(latency.get("configured_delay_s") or 0.0)
            observed_delay_s += float(latency.get("observed_injected_delay_s") or 0.0)
        previews.append({
            "call_index": call.get("call_index"),
            "timestamp": call.get("timestamp"),
            "http_status": call.get("http_status"),
            "fault_triggered": triggered,
            "configured_faults_json": call.get("configured_faults_json", "[]"),
            "triggered_faults_json": call.get("triggered_faults_json", "[]"),
            "fault_evidence_json": call.get("fault_evidence_json", "{}"),
        })
    return {
        "intercepted_calls": len(calls),
        "affected_calls": affected_calls,
        "http_status_counts": status_counts,
        "configured_delay_s": configured_delay_s,
        "observed_injected_delay_s": observed_delay_s,
        "latency_absolute_error_s": (
            abs(observed_delay_s - configured_delay_s) if configured_delay_s else None
        ),
        "call_previews": previews[:5],
        "call_preview_truncated": len(previews) > 5,
    }


def _cross_layer_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_cycle: dict[int, dict[str, dict[str, Any]]] = {}
    for record in records:
        by_cycle.setdefault(int(record["cycle"]), {})[str(record["condition"])] = record
    required = {"direct", "llm_only", "infrastructure_only", "compound"}
    complete = [conditions for conditions in by_cycle.values() if required <= conditions.keys()]
    eligible = [
        conditions
        for conditions in complete
        if all(
            conditions[condition].get("group_valid") is True
            for condition in ("llm_only", "infrastructure_only", "compound")
        )
    ]

    def _mean(condition: str, key: str) -> float | None:
        values = [float(group[condition][key]) for group in eligible]
        return statistics.mean(values) if values else None

    success = {
        condition: _mean(condition, "task_success")
        for condition in sorted(required)
    }
    duration = {
        condition: statistics.mean(
            float(group[condition]["workload"]["duration_s"]) for group in eligible
        ) if eligible else None
        for condition in sorted(required)
    }
    success_interactions = [
        compound_interaction_effect(
            control=float(group["direct"]["task_success"]),
            fault_a=float(group["llm_only"]["task_success"]),
            fault_b=float(group["infrastructure_only"]["task_success"]),
            compound=float(group["compound"]["task_success"]),
        )
        for group in eligible
    ]
    duration_interactions = [
        float(group["compound"]["workload"]["duration_s"])
        - float(group["llm_only"]["workload"]["duration_s"])
        - float(group["infrastructure_only"]["workload"]["duration_s"])
        + float(group["direct"]["workload"]["duration_s"])
        for group in eligible
    ]
    return {
        "metric_scope": "OpenAI-compatible operational probe; not EvalPlus correctness",
        "requested_cycles": len(by_cycle),
        "complete_factorial_cycles": len(complete),
        "valid_factorial_cycles": len(eligible),
        "excluded_invalid_factorial_cycles": len(complete) - len(eligible),
        "missing_factorial_cycles": len(by_cycle) - len(complete),
        "success_rate_by_condition": success,
        "mean_probe_duration_s_by_condition": duration,
        "task_success_degradation_vs_direct": {
            condition: (success["direct"] - value)
            if success["direct"] is not None and value is not None else None
            for condition, value in success.items()
        },
        "compound_success_interaction_mean": (
            statistics.mean(success_interactions) if success_interactions else None
        ),
        "compound_duration_interaction_s_mean": (
            statistics.mean(duration_interactions) if duration_interactions else None
        ),
        "valid_compound_cycles": sum(
            bool(group["compound"].get("group_valid"))
            and bool(group["compound"].get("verified_recovery"))
            for group in eligible
        ),
        "interpretation": (
            "Positive success interaction means compound degradation exceeds the sum "
            "of isolated degradations; positive duration interaction means super-additive delay."
        ),
    }


def run_e8(args: argparse.Namespace) -> int:
    results = Path(args.results_dir)
    results.mkdir(parents=True, exist_ok=True)
    upstream = args.upstream.rstrip("/")
    if upstream.endswith("/v1"):
        upstream = upstream[:-3]
    study_id = f"e8-{uuid.uuid4().hex[:12]}"
    image_digest = _docker_image_digest(args.docker_bin, args.docker_image)
    manifest = {
        "study": "E8-cross-layer-chaos",
        "study_id": study_id,
        "started_at": _now(),
        "schema": OUTPUT_SCHEMA_VERSION,
        "docker_image": args.docker_image,
        "docker_image_digest": image_digest,
        "cj_commit": _CJ_COMMIT,
        **_CJ_SOURCE_PROVENANCE,
        "model": args.model,
        "layers": ["llm_proxy", "container_resource"],
        "conditions": ["direct", "llm_only", "infrastructure_only", "compound"],
        "canonical_studies_modified": False,
        "blast_radius": "one disposable container plus one host proxy process",
    }
    _write_json(results / "study_manifest.json", manifest)
    db = SessionDB(path=str(results / "cj_evidence.sqlite"))
    records: list[dict[str, Any]] = []

    for cycle in range(args.cycles):
        factorial_id = f"{study_id}-cycle-{cycle}"
        for condition in manifest["conditions"]:
            container_id = ""
            target: DockerTarget | None = None
            runner: InjectionGroupRunner | None = None
            proxy_session_id: int | None = None
            started = time.time()
            start_error = ""
            try:
                container_id = _create_idle_container(args, "agent")
                target = DockerTarget(container_id, timeout_s=90.0)
                injections = []
                if condition in {"llm_only", "compound"}:
                    proxy_target = LocalTarget()
                    proxy_session_id = db.open_session(
                        f"E8-{condition}-{cycle}",
                        target_type="local-proxy",
                        target_addr=f"127.0.0.1:{args.proxy_port}",
                    )
                    proxy_target._db = db
                    proxy_target._session_id = proxy_session_id
                    injections.append(Injection(
                        id="llm-latency",
                        target=proxy_target,
                        fault=LLMLatency(
                            delay_s=args.delay_s,
                            port=args.proxy_port,
                            upstream=upstream,
                            base_url_env="CJ_EVAL_BASE_URL",
                        ),
                    ))
                if condition in {"infrastructure_only", "compound"}:
                    injections.append(Injection(
                        id="container-cpu",
                        target=target,
                        fault=CPUStress(cores=1, duration_s=args.stress_duration),
                    ))
                evidence: dict[str, Any] = {
                    "verdict": "not_applicable",
                    "group_valid": None,
                    "rollback_success": None,
                    "verified_recovery": None,
                    "members": [],
                }
                if injections:
                    target_metadata = {
                        "llm-latency": {
                            "target_type": "host_proxy_process",
                            "target_role": "model_gateway",
                            "target_id": f"127.0.0.1:{args.proxy_port}",
                        },
                        "container-cpu": {
                            "target_type": "docker",
                            "target_role": "agent",
                            "target_id": container_id,
                        },
                    }
                    runner = InjectionGroupRunner(InjectionGroup(
                        name=f"E8-{condition}-{cycle}",
                        injections=injections,
                        synchronization="scheduled" if len(injections) > 1 else "barrier",
                        atomic=True,
                        maximum_skew_ms=args.maximum_skew_ms,
                        start_after=args.start_after if len(injections) > 1 else 0.0,
                        on_activation_failure="rollback_all",
                        on_skew_violation="rollback_all",
                        safety_maximum_duration=args.safety_duration,
                    ))
                    try:
                        runner.start()
                    except Exception as exc:  # noqa: BLE001
                        start_error = repr(exc)
                url = (
                    f"http://host.docker.internal:{args.proxy_port}/v1/chat/completions"
                    if condition in {"llm_only", "compound"}
                    else args.container_upstream.rstrip("/") + "/chat/completions"
                )
                workload = _container_llm_probe(target, url, args.model)
                if runner:
                    evidence = _group_evidence(runner, start_error, target_metadata)
                proxy_evidence = (
                    _proxy_evidence(db.get_llm_calls(proxy_session_id))
                    if proxy_session_id is not None else {
                        "intercepted_calls": 0,
                        "affected_calls": 0,
                    }
                )
                operational_success = workload["exit_code"] == 0
                record = {
                    "study": "E8-cross-layer-chaos",
                    "study_id": study_id,
                    "factorial_id": factorial_id,
                    "run_id": f"e8-run-{uuid.uuid4().hex[:12]}",
                    "timestamp": _now(),
                    "cycle": cycle,
                    "condition": condition,
                    "container_id": container_id,
                    "docker_image": args.docker_image,
                    "docker_image_digest": image_digest,
                    "cj_commit": _CJ_COMMIT,
                    **_CJ_SOURCE_PROVENANCE,
                    "workload": workload,
                    "operational_success": operational_success,
                    "task_success": operational_success,
                    "task_metric": "openai_compatible_probe_success",
                    "proxy_evidence": proxy_evidence,
                    "duration_s": time.time() - started,
                    **evidence,
                }
                _append_jsonl(results / "runs.jsonl", record)
                records.append(record)
            finally:
                if runner and runner.evidence and runner.evidence.verdict == "pending":
                    _group_evidence(runner, start_error)
                if target:
                    try:
                        target.disconnect()
                    except Exception:
                        pass
                if container_id:
                    _cleanup_container(args.docker_bin, container_id)
                if proxy_session_id is not None:
                    db.close_session(proxy_session_id)
    _write_json(results / "cross_layer_summary.json", _cross_layer_summary(records))
    db.close()
    manifest["finished_at"] = _now()
    _write_json(results / "study_manifest.json", manifest)
    print(f"[cj-eval] E8 complete: {results}")
    return 0


class _FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        payload = json.dumps({
            "id": "chatcmpl-cj-perf",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):  # noqa: A002
        return


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(url: str, timeout_s: float = 15.0) -> LoadSample:
    body = b'{"model":"fake","messages":[{"role":"user","content":"ping"}]}'
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            payload = response.read()
            return LoadSample(time.monotonic() - started, response.status == 200, response.status, len(payload))
    except urllib.error.HTTPError as exc:
        return LoadSample(time.monotonic() - started, False, exc.code, 0, repr(exc))
    except Exception as exc:  # noqa: BLE001
        return LoadSample(time.monotonic() - started, False, None, 0, repr(exc))


def _load(url: str, count: int, concurrency: int) -> dict[str, Any]:
    samples, wall_duration_s = _load_samples(url, count, concurrency)
    return summarize_load(samples, wall_duration_s)


def _load_samples(
    url: str,
    count: int,
    concurrency: int,
) -> tuple[list[LoadSample], float]:
    started = time.monotonic()
    samples: list[LoadSample] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(_request, url) for _ in range(count)]
        for future in as_completed(futures):
            samples.append(future.result())
    return samples, time.monotonic() - started


def _wait_url(url: str, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5):
                return
        except Exception:
            time.sleep(0.05)
    raise RuntimeError(f"endpoint did not become ready: {url}")


def _start_passthrough(port: int, upstream: str) -> subprocess.Popen:
    from chaos_jungle.scripts.llm_proxy import llm_proxy

    proc = subprocess.Popen(
        [sys.executable, llm_proxy.__file__, "--port", str(port), "--upstream", upstream, "--fault", "passthrough"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_url(f"http://127.0.0.1:{port}/_cj/health")
    return proc


def _stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def run_e9(args: argparse.Namespace) -> int:
    results = Path(args.results_dir)
    results.mkdir(parents=True, exist_ok=True)
    study_id = f"e9-{uuid.uuid4().hex[:12]}"
    upstream_port = _free_port()
    proxy_port = args.proxy_port or _free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), _FakeHandler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    direct_url = f"http://127.0.0.1:{upstream_port}/v1/chat/completions"
    proxy_url = f"http://127.0.0.1:{proxy_port}/v1/chat/completions"
    proxy: subprocess.Popen | None = None
    started_at = _now()
    manifest = {
        "study": "E9-performance-restoration",
        "study_id": study_id,
        "started_at": started_at,
        "schema": OUTPUT_SCHEMA_VERSION,
        "cj_commit": _CJ_COMMIT,
        **_CJ_SOURCE_PROVENANCE,
        "requests_per_round": args.requests,
        "rounds": args.rounds,
        "concurrency": args.concurrency,
        "warmup_requests": args.warmup_requests,
        "restoration_cycles": args.cycles,
        "canonical_studies_modified": False,
    }
    _write_json(results / "study_manifest.json", manifest)
    try:
        proxy = _start_passthrough(proxy_port, f"http://127.0.0.1:{upstream_port}")
        _load(direct_url, args.warmup_requests, min(args.concurrency, args.warmup_requests))
        _load(proxy_url, args.warmup_requests, min(args.concurrency, args.warmup_requests))

        direct_samples: list[LoadSample] = []
        control_samples: list[LoadSample] = []
        direct_wall_s = 0.0
        control_wall_s = 0.0
        round_records: list[dict[str, Any]] = []
        for round_index in range(args.rounds):
            order = ["direct", "cj_control"]
            if round_index % 2:
                order.reverse()
            round_payload: dict[str, Any] = {"round": round_index, "order": order}
            for condition in order:
                url = direct_url if condition == "direct" else proxy_url
                samples, wall_s = _load_samples(url, args.requests, args.concurrency)
                round_payload[condition] = summarize_load(samples, wall_s)
                if condition == "direct":
                    direct_samples.extend(samples)
                    direct_wall_s += wall_s
                else:
                    control_samples.extend(samples)
                    control_wall_s += wall_s
            round_records.append(round_payload)
        direct = summarize_load(direct_samples, direct_wall_s)
        control = summarize_load(control_samples, control_wall_s)
        _stop_process(proxy)
        proxy = None

        cycles: list[RestorationCycle] = []
        for index in range(args.cycles):
            activation_started = time.monotonic()
            fault = LLMLatency(
                delay_s=args.delay_s,
                port=proxy_port,
                upstream=f"http://127.0.0.1:{upstream_port}",
                base_url_env="CJ_EVAL_BASE_URL",
            )
            target = LocalTarget()
            error = ""
            active = manifested = reverted = recovered = False
            cleanup_s = None
            try:
                fault.start(target)
                active_result = fault.verify_active(target)
                active = bool(active_result.verified)
                sample = _request(proxy_url)
                manifested = sample.success and sample.duration_s >= args.delay_s * 0.8
            except Exception as exc:  # noqa: BLE001
                error = repr(exc)
            finally:
                cleanup_started = time.monotonic()
                try:
                    fault.stop(target)
                    reverted = True
                    recovered = bool(fault.verify_recovered(target).verified)
                except Exception as exc:  # noqa: BLE001
                    error = error or repr(exc)
                cleanup_s = time.monotonic() - cleanup_started
            cycles.append(RestorationCycle(
                index, active, manifested, reverted, recovered,
                time.monotonic() - activation_started - (cleanup_s or 0.0),
                cleanup_s, error,
            ))

        payload = {
            "study": "E9-performance-restoration",
            "study_id": study_id,
            "run_id": f"e9-run-{uuid.uuid4().hex[:12]}",
            "timestamp": _now(),
            "schema": OUTPUT_SCHEMA_VERSION,
            "cj_commit": _CJ_COMMIT,
            **_CJ_SOURCE_PROVENANCE,
            "requests_per_round": args.requests,
            "rounds": args.rounds,
            "requests_per_condition": args.requests * args.rounds,
            "concurrency": args.concurrency,
            "warmup_requests_discarded_per_condition": args.warmup_requests,
            "rounds_detail": round_records,
            "direct": direct,
            "cj_control": control,
            "duration_overhead_p50_ratio": (
                (control["latency_p50_ms"] - direct["latency_p50_ms"]) / direct["latency_p50_ms"]
                if direct["latency_p50_ms"] else None
            ),
            "throughput_overhead_ratio": (
                (control["throughput_rps"] - direct["throughput_rps"]) / direct["throughput_rps"]
                if direct["throughput_rps"] else None
            ),
            "restoration": summarize_restoration(cycles),
            "cycles": [asdict(cycle) for cycle in cycles],
            "canonical_studies_modified": False,
        }
        _write_json(results / "performance_restoration_summary.json", payload)
        _append_jsonl(results / "runs.jsonl", payload)
        manifest["finished_at"] = _now()
        _write_json(results / "study_manifest.json", manifest)
    finally:
        if proxy:
            _stop_process(proxy)
        upstream.shutdown()
        upstream.server_close()
    print(f"[cj-eval] E9 complete: {results}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="study", required=True)

    e7 = sub.add_parser("e7", help="distributed multi-target coordination")
    e7.set_defaults(func=run_e7, study="7")
    e7.add_argument("--docker-image", required=True)
    e7.add_argument("--results-dir", required=True)
    e7.add_argument("--roles", default="planner,coder,reviewer")
    e7.add_argument("--cycles", type=int, default=3)
    e7.add_argument("--maximum-skew-ms", type=float, default=100.0)
    e7.add_argument("--start-after", type=float, default=0.5)
    e7.add_argument("--stress-duration", type=int, default=20)
    e7.add_argument("--safety-duration", type=float, default=30.0)
    e7.add_argument("--cpus", type=float, default=1.0)
    e7.add_argument("--memory", default="1g")
    e7.add_argument("--docker-bin", default="docker")

    e8 = sub.add_parser("e8", help="cross-layer LLM and infrastructure chaos")
    e8.set_defaults(func=run_e8, study="8")
    e8.add_argument("--docker-image", required=True)
    e8.add_argument("--results-dir", required=True)
    e8.add_argument("--upstream", required=True, help="host-reachable model base URL")
    e8.add_argument("--container-upstream", required=True, help="container-reachable model /v1 URL")
    e8.add_argument("--model", required=True)
    e8.add_argument("--env-file")
    e8.add_argument("--proxy-port", type=int, default=20380)
    e8.add_argument("--cycles", type=int, default=1)
    e8.add_argument("--delay-s", type=float, default=0.5)
    e8.add_argument("--maximum-skew-ms", type=float, default=250.0)
    e8.add_argument("--start-after", type=float, default=0.5)
    e8.add_argument("--stress-duration", type=int, default=30)
    e8.add_argument("--safety-duration", type=float, default=60.0)
    e8.add_argument("--cpus", type=float, default=1.0)
    e8.add_argument("--memory", default="2g")
    e8.add_argument("--docker-bin", default="docker")

    e9 = sub.add_parser("e9", help="proxy performance and restoration")
    e9.set_defaults(func=run_e9)
    e9.add_argument("--results-dir", required=True)
    e9.add_argument("--requests", type=int, default=200)
    e9.add_argument("--rounds", type=int, default=5)
    e9.add_argument("--warmup-requests", type=int, default=10)
    e9.add_argument("--concurrency", type=int, default=8)
    e9.add_argument("--cycles", type=int, default=30)
    e9.add_argument("--delay-s", type=float, default=0.05)
    e9.add_argument("--proxy-port", type=int, default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "cycles", 1) < 1:
        raise SystemExit("--cycles must be at least 1")
    for field in ("requests", "rounds", "warmup_requests", "concurrency"):
        if hasattr(args, field) and getattr(args, field) < 1:
            raise SystemExit(f"--{field.replace('_', '-')} must be at least 1")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
