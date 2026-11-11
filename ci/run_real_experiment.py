"""Bounded CJ proxy experiment for optional local or live GitLab jobs."""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from chaos_jungle import ChaosRunner, LLMLatency, LocalTarget, Scenario
from chaos_jungle.db.session_db import SessionDB
from chaos_jungle.metrics import CollectStrategy


class LocalModelHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if request.get("model") != "ci-local-model":
            self.send_error(400)
            return
        response = json.dumps(
            {
                "id": "ci-local-response",
                "object": "chat.completion",
                "model": "ci-local-model",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "ready"},
                     "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        pass


def free_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def live_model_config() -> tuple[str, str, str]:
    base_url = os.environ.get("CJ_EVAL_BASE_URL", "").rstrip("/")
    model = os.environ.get("CJ_EVAL_MODEL", "")
    api_key = os.environ.get("CJ_EVAL_API_KEY", "")
    if not base_url or not model or not api_key:
        raise ValueError("Live mode requires CJ_EVAL_BASE_URL, CJ_EVAL_MODEL, and CJ_EVAL_API_KEY")
    parsed = urlsplit(base_url)
    if not parsed.hostname or not parsed.path.endswith("/v1") or parsed.query or parsed.fragment:
        raise ValueError("CJ_EVAL_BASE_URL must end in /v1 and have no query or fragment")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
    ):
        raise ValueError("Live model credentials require HTTPS, except for loopback endpoints")
    if parsed.username or parsed.password:
        raise ValueError("Do not embed credentials in CJ_EVAL_BASE_URL")
    return base_url, model, api_key


def run_experiment(output_dir: Path, mode: str = "local") -> None:
    server = None
    server_thread = None
    if mode == "live":
        base_url, model, api_key = live_model_config()
        upstream = base_url[:-3]
        calls_per_trial = 2
        delay_s = 0.25
    else:
        server = ThreadingHTTPServer(("127.0.0.1", 0), LocalModelHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        upstream = "http://127.0.0.1:{}".format(server.server_port)
        base_url = upstream + "/v1"
        model = "ci-local-model"
        api_key = ""
        calls_per_trial = 3
        delay_s = 0.12
    output_dir.mkdir(parents=True, exist_ok=True)
    db = SessionDB(path=str(output_dir / "session.db"))
    previous_base_url = os.environ.get("OPENAI_BASE_URL")
    os.environ["OPENAI_BASE_URL"] = base_url
    fault = LLMLatency(delay_s=delay_s, port=free_loopback_port(), upstream=upstream)
    runner = ChaosRunner(Scenario("ci-{}-proxy-latency".format(mode), [fault]), LocalTarget(), db=db)

    def workload() -> dict[str, float]:
        durations_ms = []
        for _ in range(calls_per_trial):
            body = json.dumps(
                {"model": model, "messages": [{"role": "user", "content": "Reply briefly to ping."}],
                 "max_tokens": 16}
            ).encode()
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = "Bearer " + api_key
            request = Request(
                os.environ["OPENAI_BASE_URL"] + "/chat/completions",
                data=body,
                headers=headers,
            )
            started = time.perf_counter()
            with urlopen(request, timeout=30) as response:
                payload = json.load(response)
                assert response.status == 200
                assert payload["choices"], "model returned no choices"
            durations_ms.append((time.perf_counter() - started) * 1000)
        return {
            "duration_s": sum(durations_ms) / 1000,
            "p50_latency_ms": statistics.median(durations_ms),
            "p99_latency_ms": max(durations_ms),
            "success": 1.0,
        }

    try:
        result = runner.measure(
            workload, n_baseline=2, n_fault=2, strategy=CollectStrategy.SNAPSHOT
        )
        exposure = runner.exposure_report()
        calls = db.get_llm_calls(result.session_id, phase="fault")
        session = db.get_session(result.session_id)

        report = {
            "mode": mode,
            "interpretation": "CI integration smoke test; not a statistical performance estimate",
            "scenario": result.scenario,
            "session_id": result.session_id,
            "fault_default_metrics": fault.default_metrics,
            "baseline": result.baseline,
            "fault": result.fault,
            "delta": result.delta,
            "raw_baseline": result.raw_baseline,
            "raw_fault": result.raw_fault,
            "collected_metrics": (
                asdict(result.collected_metrics) if result.collected_metrics else None
            ),
            "exposure_report": exposure,
            "fault_call_count": len(calls),
            "session_status": session["status"] if session else None,
        }
        (output_dir / "metrics.json").write_text(json.dumps(report, indent=2) + "\n")
        (output_dir / "summary.txt").write_text(
            "CI integration smoke test; not a statistical performance estimate.\n\n"
            + result.summary() + "\n"
        )
        print(result.summary())
        print("Exposure verdict:", exposure["verdict"] if exposure else "missing")
        print("Artifacts:", output_dir.resolve())

        assert exposure is not None and exposure["verdict"] == "valid", exposure
        assert all(item["recovered"] for item in exposure["faults"]), exposure
        assert len(calls) == 2 * calls_per_trial, "unexpected proxied call count"
        assert all(call["fault_triggered"] for call in calls)
        if mode == "local":
            assert result.delta["duration_s"] >= 0.25, result.summary()
        assert result.collected_metrics is not None
        assert set(fault.default_metrics) <= set(result.collected_metrics.delta)
        assert session is not None and session["status"] == "reverted", session
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
        if previous_base_url is None:
            os.environ.pop("OPENAI_BASE_URL", None)
        else:
            os.environ["OPENAI_BASE_URL"] = previous_base_url
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("local", "live"), default="local")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    run_experiment(args.output_dir, mode=args.mode)
