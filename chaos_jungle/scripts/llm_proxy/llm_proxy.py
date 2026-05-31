#!/usr/bin/env python3
"""Chaos Jungle LLM proxy — injects faults into LLM API calls.

A lightweight HTTP reverse-proxy that forwards requests to a real LLM
API endpoint while injecting configurable faults. Uses only Python stdlib.

Faults
------
latency       Add artificial delay before forwarding each request.
rate_limit    Return HTTP 429 after N successful requests.
timeout       Hang the connection for timeout_s seconds, then return 504.
corrupt       Forward the request but mangle the response body.
unavailable   Always return HTTP 503.

Usage examples
--------------
::

    # Add 3 s latency to every OpenAI call
    python llm_proxy.py --port 18000 --upstream https://api.openai.com \\
        --fault latency --latency-s 3.0

    # Simulate rate-limiting after the 5th request
    python llm_proxy.py --port 18000 --upstream https://api.openai.com \\
        --fault rate_limit --rate-limit-n 5

    # Hang every request for 30 s (timeout simulation)
    python llm_proxy.py --port 18000 --upstream https://api.openai.com \\
        --fault timeout --timeout-s 30.0

    # Truncate every response to half its length
    python llm_proxy.py --port 18000 --upstream https://api.openai.com \\
        --fault corrupt --corrupt-mode truncate

    # Always return 503
    python llm_proxy.py --port 18000 --upstream https://api.openai.com \\
        --fault unavailable
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Lock

# ---------------------------------------------------------------------------
# Global state (set by main before the server starts)
# ---------------------------------------------------------------------------

FAULT: str = ""
FAULT_ARGS: dict = {}
_request_count: int = 0
_count_lock: Lock = Lock()

# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

_ERROR_CONTENT_TYPE = "application/json"

_RESPONSES = {
    "unavailable": (
        503,
        b'{"error":{"message":"Service Unavailable (chaos-jungle)","type":"chaos_unavailable","code":"service_unavailable"}}',
    ),
    "rate_limit": (
        429,
        b'{"error":{"message":"Rate limit exceeded (chaos-jungle)","type":"chaos_rate_limit","code":"rate_limit_exceeded"}}',
    ),
    "timeout": (
        504,
        b'{"error":{"message":"Gateway Timeout (chaos-jungle)","type":"chaos_timeout","code":"gateway_timeout"}}',
    ),
}


class _ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silence default per-request logging
        pass

    # Accept all HTTP methods
    def do_GET(self):    self._handle()
    def do_POST(self):   self._handle()
    def do_PUT(self):    self._handle()
    def do_PATCH(self):  self._handle()
    def do_DELETE(self): self._handle()

    def _handle(self):
        global _request_count

        with _count_lock:
            _request_count += 1
            count = _request_count

        fault = FAULT

        # ------------------------------------------------------------------
        # UNAVAILABLE — never forward
        # ------------------------------------------------------------------
        if fault == "unavailable":
            status, body = _RESPONSES["unavailable"]
            self._reply(status, body)
            return

        # ------------------------------------------------------------------
        # RATE LIMIT — reject after N successful requests
        # ------------------------------------------------------------------
        if fault == "rate_limit":
            n = FAULT_ARGS.get("n", 5)
            if count > n:
                status, body = _RESPONSES["rate_limit"]
                self._reply(status, body)
                return

        # ------------------------------------------------------------------
        # TIMEOUT — sleep then return 504 without forwarding
        # ------------------------------------------------------------------
        if fault == "timeout":
            timeout_s = FAULT_ARGS.get("timeout_s", 30.0)
            time.sleep(timeout_s)
            status, body = _RESPONSES["timeout"]
            self._reply(status, body)
            return

        # ------------------------------------------------------------------
        # LATENCY — sleep then forward normally
        # ------------------------------------------------------------------
        if fault == "latency":
            delay_s = FAULT_ARGS.get("delay_s", 2.0)
            time.sleep(delay_s)

        # ------------------------------------------------------------------
        # Forward request to upstream
        # ------------------------------------------------------------------
        content_length = int(self.headers.get("Content-Length", 0) or 0)
        body_in = self.rfile.read(content_length) if content_length > 0 else b""

        upstream = FAULT_ARGS.get("upstream", "https://api.openai.com")
        upstream_url = upstream.rstrip("/") + self.path

        fwd_headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in ("host", "content-length", "transfer-encoding")
        }
        if body_in:
            fwd_headers["Content-Length"] = str(len(body_in))

        req = urllib.request.Request(
            upstream_url,
            data=body_in or None,
            headers=fwd_headers,
            method=self.command,
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                resp_body = resp.read()
                resp_status = resp.status
                resp_ct = resp.headers.get("Content-Type", _ERROR_CONTENT_TYPE)

                # CORRUPT — mangle the response after receiving it
                if fault == "corrupt":
                    mode = FAULT_ARGS.get("mode", "truncate")
                    if mode == "truncate":
                        resp_body = resp_body[: max(1, len(resp_body) // 2)]
                    elif mode == "empty":
                        resp_body = b"{}"
                    elif mode == "invalid_json":
                        resp_body = b"<<chaos-jungle: response corrupted>>"

                self._reply(resp_status, resp_body, resp_ct)

        except urllib.error.HTTPError as exc:
            err_body = exc.read() or b"{}"
            self._reply(exc.code, err_body, _ERROR_CONTENT_TYPE)

        except Exception as exc:  # noqa: BLE001
            msg = json.dumps(
                {"error": {"message": str(exc), "type": "chaos_proxy_error"}}
            ).encode()
            self._reply(502, msg)

    def _reply(self, status: int, body: bytes, content_type: str = _ERROR_CONTENT_TYPE) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    global FAULT, FAULT_ARGS

    parser = argparse.ArgumentParser(
        description="Chaos Jungle LLM proxy",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--port", type=int, default=18000, help="Local proxy port")
    parser.add_argument(
        "--upstream",
        default="https://api.openai.com",
        help="Real LLM API base URL",
    )
    parser.add_argument(
        "--fault",
        required=True,
        choices=["latency", "rate_limit", "timeout", "corrupt", "unavailable"],
        help="Fault type to inject",
    )
    parser.add_argument("--latency-s", type=float, default=2.0, help="Delay in seconds (latency fault)")
    parser.add_argument("--rate-limit-n", type=int, default=5, help="Number of allowed requests (rate_limit fault)")
    parser.add_argument("--timeout-s", type=float, default=30.0, help="Seconds to hang (timeout fault)")
    parser.add_argument(
        "--corrupt-mode",
        default="truncate",
        choices=["truncate", "empty", "invalid_json"],
        help="Corruption strategy (corrupt fault)",
    )
    args = parser.parse_args()

    FAULT = args.fault
    FAULT_ARGS = {
        "upstream": args.upstream,
        "delay_s": args.latency_s,
        "n": args.rate_limit_n,
        "timeout_s": args.timeout_s,
        "mode": args.corrupt_mode,
    }

    server = HTTPServer(("127.0.0.1", args.port), _ProxyHandler)
    print(
        f"chaos-jungle LLM proxy listening on 127.0.0.1:{args.port}  "
        f"fault={FAULT}  upstream={args.upstream}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
