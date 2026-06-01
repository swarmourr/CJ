#!/usr/bin/env python3
"""Chaos Jungle LLM / MCP proxy — injects faults into agent HTTP traffic.

A lightweight HTTP reverse-proxy that forwards requests to a real LLM
or MCP API endpoint while injecting configurable faults. Uses only Python
stdlib — no external dependencies.

Supported faults
----------------
latency         Sleep delay_s before forwarding every request.
rate_limit      Return 429 after n successful requests.
timeout         Hang the connection for timeout_s seconds then return 504.
corrupt         Forward but mangle the response body (truncate/empty/invalid_json).
unavailable     Always return 503.
tool_fault      Inject errors into tool-call requests (messages with role=tool).
hallucinate     Replace the assistant's content with injected wrong text.
stream_interrupt Forward a streaming response but cut it after N SSE events.
token_starve    Rewrite the request to set max_tokens to a tiny value.
mcp_tool_error  Return a JSON-RPC error for any MCP tool/resource call.
mcp_unavailable Always return 503 for MCP traffic.
mcp_timeout     Hang every MCP call for timeout_s seconds.

Usage examples
--------------
::

    # 3 s latency
    python llm_proxy.py --port 18000 --upstream https://api.openai.com \\
        --fault latency --latency-s 3.0

    # Rate-limit after 5 requests
    python llm_proxy.py --port 18000 --upstream https://api.openai.com \\
        --fault rate_limit --rate-limit-n 5

    # Hang every request 30 s
    python llm_proxy.py --port 18000 --upstream https://api.openai.com \\
        --fault timeout --timeout-s 30.0

    # Truncate responses
    python llm_proxy.py --port 18000 --upstream https://api.openai.com \\
        --fault corrupt --corrupt-mode truncate

    # Always 503
    python llm_proxy.py --port 18000 --upstream https://api.openai.com \\
        --fault unavailable

    # Inject tool-call errors
    python llm_proxy.py --port 18000 --upstream https://api.openai.com \\
        --fault tool_fault --tool-name search

    # Replace assistant answer with wrong text
    python llm_proxy.py --port 18000 --upstream https://api.openai.com \\
        --fault hallucinate --hallucination-text "The capital of France is Berlin."

    # Cut streaming response after 3 SSE events
    python llm_proxy.py --port 18000 --upstream https://api.openai.com \\
        --fault stream_interrupt --stream-interrupt-after 3

    # Force max_tokens=5
    python llm_proxy.py --port 18000 --upstream https://api.openai.com \\
        --fault token_starve --token-starve-max 5

    # MCP tool error
    python llm_proxy.py --port 18100 --upstream http://localhost:3000 \\
        --fault mcp_tool_error

    # MCP timeout
    python llm_proxy.py --port 18100 --upstream http://localhost:3000 \\
        --fault mcp_timeout --timeout-s 10.0
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
# Global state — set by main() before the server starts
# ---------------------------------------------------------------------------

FAULT: str = ""
FAULT_ARGS: dict = {}
_request_count: int = 0
_count_lock: Lock = Lock()

_CT_JSON = "application/json"

# ---------------------------------------------------------------------------
# Static error responses
# ---------------------------------------------------------------------------

_STATIC = {
    "unavailable": (
        503,
        b'{"error":{"message":"Service Unavailable (chaos-jungle)","type":"chaos_unavailable","code":"service_unavailable"}}',
    ),
    "mcp_unavailable": (
        503,
        b'{"jsonrpc":"2.0","id":null,"error":{"code":-32000,"message":"MCP server unavailable (chaos-jungle)"}}',
    ),
    "rate_limit": (
        429,
        b'{"error":{"message":"Rate limit exceeded (chaos-jungle)","type":"chaos_rate_limit","code":"rate_limit_exceeded"}}',
    ),
    "timeout": (
        504,
        b'{"error":{"message":"Gateway Timeout (chaos-jungle)","type":"chaos_timeout","code":"gateway_timeout"}}',
    ),
    "mcp_timeout": (
        504,
        b'{"jsonrpc":"2.0","id":null,"error":{"code":-32000,"message":"MCP call timed out (chaos-jungle)"}}',
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_body(raw: bytes) -> dict | None:
    """Parse JSON body; return None if it fails."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _is_tool_request(body: dict | None) -> bool:
    """Return True if the request contains a tool-role message."""
    if not body:
        return False
    messages = body.get("messages", [])
    return any(m.get("role") == "tool" for m in messages)


def _is_mcp_request(body: dict | None) -> bool:
    """Return True if the request looks like a JSON-RPC / MCP call."""
    if not body:
        return False
    return "jsonrpc" in body or "method" in body


def _tool_error_response(body: dict | None) -> bytes:
    """Build an OpenAI-style error body for a tool fault."""
    return json.dumps({
        "error": {
            "message": "Tool execution failed (injected by chaos-jungle)",
            "type": "chaos_tool_fault",
            "code": "tool_execution_error",
        }
    }).encode()


def _mcp_tool_error_response(req_body: dict | None) -> bytes:
    """Build a JSON-RPC error response for an MCP tool/resource call."""
    req_id = req_body.get("id") if req_body else None
    return json.dumps({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": -32000,
            "message": "Tool execution failed (injected by chaos-jungle)",
            "data": {"type": "chaos_mcp_tool_error"},
        },
    }).encode()


def _inject_hallucination(resp_body: bytes, text: str) -> bytes:
    """Replace the assistant content in a chat completion response.

    Supports both OpenAI format (choices[0].message.content) and
    Ollama native format (message.content at the top level).
    """
    try:
        data = json.loads(resp_body)
        # OpenAI / OpenAI-compat format
        choices = data.get("choices", [])
        if choices and "message" in choices[0]:
            choices[0]["message"]["content"] = text
            choices[0]["finish_reason"] = "stop"
            return json.dumps(data).encode()
        # Ollama native /api/chat format
        if "message" in data and "content" in data["message"]:
            data["message"]["content"] = text
            data["done"] = True
            return json.dumps(data).encode()
    except (json.JSONDecodeError, KeyError, IndexError):
        pass
    return resp_body  # fallback: return unchanged


def _forward(method: str, upstream_url: str, headers: dict, body: bytes) -> tuple[int, bytes, str]:
    """Forward a request to upstream. Returns (status, body, content_type)."""
    req = urllib.request.Request(
        upstream_url,
        data=body or None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", _CT_JSON)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b"{}", _CT_JSON
    except Exception as exc:  # noqa: BLE001
        err = json.dumps({"error": {"message": str(exc), "type": "chaos_proxy_error"}}).encode()
        return 502, err, _CT_JSON


def _build_upstream_url(path: str) -> str:
    upstream = FAULT_ARGS.get("upstream", "https://api.openai.com")
    return upstream.rstrip("/") + path


def _build_fwd_headers(src_headers, body: bytes) -> dict:
    hdrs = {
        k: v for k, v in src_headers.items()
        if k.lower() not in ("host", "content-length", "transfer-encoding")
    }
    if body:
        hdrs["Content-Length"] = str(len(body))
    return hdrs


# ---------------------------------------------------------------------------
# SSE streaming helper
# ---------------------------------------------------------------------------

def _stream_interrupt(handler: "BaseHTTPRequestHandler", upstream_url: str,
                      headers: dict, body: bytes, interrupt_after: int) -> None:
    """Forward a streaming SSE response but close after interrupt_after data events."""
    req = urllib.request.Request(upstream_url, data=body or None,
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            handler.send_response(resp.status)
            for k, v in resp.headers.items():
                if k.lower() in ("content-type", "cache-control", "x-accel-buffering"):
                    handler.send_header(k, v)
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()

            data_event_count = 0
            for raw_line in resp:
                if data_event_count >= interrupt_after:
                    # Abrupt stop — do not send [DONE]
                    break
                handler.wfile.write(raw_line)
                handler.wfile.flush()
                line = raw_line.strip()
                if line.startswith(b"data:") and line != b"data: [DONE]":
                    data_event_count += 1
    except Exception:  # noqa: BLE001
        pass  # connection was already partially written — nothing to do


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------


class _ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silence per-request logging
        pass

    def do_GET(self):    self._handle()
    def do_POST(self):   self._handle()
    def do_PUT(self):    self._handle()
    def do_PATCH(self):  self._handle()
    def do_DELETE(self): self._handle()

    def _handle(self) -> None:
        global _request_count

        with _count_lock:
            _request_count += 1
            count = _request_count

        fault = FAULT

        # Read request body
        content_length = int(self.headers.get("Content-Length", 0) or 0)
        raw_body = self.rfile.read(content_length) if content_length > 0 else b""
        req_body = _parse_body(raw_body)

        upstream_url = _build_upstream_url(self.path)

        # ------------------------------------------------------------------
        # Faults that never forward
        # ------------------------------------------------------------------

        if fault == "unavailable":
            self._reply(*_STATIC["unavailable"])
            return

        if fault == "mcp_unavailable":
            self._reply(*_STATIC["mcp_unavailable"])
            return

        if fault == "rate_limit":
            if count > FAULT_ARGS.get("n", 5):
                self._reply(*_STATIC["rate_limit"])
                return

        if fault in ("timeout", "mcp_timeout"):
            time.sleep(FAULT_ARGS.get("timeout_s", 30.0))
            self._reply(*_STATIC[fault])
            return

        # ------------------------------------------------------------------
        # Faults that modify the REQUEST before forwarding
        # ------------------------------------------------------------------

        if fault == "tool_fault" and _is_tool_request(req_body):
            tool_name = FAULT_ARGS.get("tool_name")
            # Only block if tool_name matches (or no filter set)
            if not tool_name or any(
                m.get("name") == tool_name
                for m in (req_body.get("messages", []) if req_body else [])
                if m.get("role") == "tool"
            ):
                self._reply(400, _tool_error_response(req_body))
                return

        if fault == "mcp_tool_error" and _is_mcp_request(req_body):
            self._reply(200, _mcp_tool_error_response(req_body))
            return

        if fault == "token_starve" and req_body is not None:
            n = FAULT_ARGS.get("max_tokens", 5)
            req_body["max_tokens"] = n        # OpenAI / OpenAI-compat
            req_body["num_predict"] = n       # Ollama native /api/generate + /api/chat
            raw_body = json.dumps(req_body).encode()

        if fault == "latency":
            time.sleep(FAULT_ARGS.get("delay_s", 2.0))

        # ------------------------------------------------------------------
        # Stream interrupt — requires special line-by-line handling
        # ------------------------------------------------------------------

        is_streaming = req_body is not None and req_body.get("stream") is True
        if fault == "stream_interrupt" and is_streaming:
            fwd_hdrs = _build_fwd_headers(self.headers, raw_body)
            _stream_interrupt(
                self, upstream_url, fwd_hdrs, raw_body,
                interrupt_after=FAULT_ARGS.get("interrupt_after", 3),
            )
            return

        # ------------------------------------------------------------------
        # Forward request to upstream
        # ------------------------------------------------------------------

        fwd_hdrs = _build_fwd_headers(self.headers, raw_body)
        status, resp_body, resp_ct = _forward(self.command, upstream_url, fwd_hdrs, raw_body)

        # ------------------------------------------------------------------
        # Faults that modify the RESPONSE before returning
        # ------------------------------------------------------------------

        if fault == "corrupt":
            mode = FAULT_ARGS.get("mode", "truncate")
            if mode == "truncate":
                resp_body = resp_body[: max(1, len(resp_body) // 2)]
            elif mode == "empty":
                resp_body = b"{}"
            elif mode == "invalid_json":
                resp_body = b"<<chaos-jungle: response corrupted>>"

        if fault == "hallucinate":
            text = FAULT_ARGS.get("text", "WRONG ANSWER (injected by chaos-jungle)")
            resp_body = _inject_hallucination(resp_body, text)

        self._reply(status, resp_body, resp_ct)

    def _reply(self, status: int, body: bytes,
               content_type: str = _CT_JSON) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


_ALL_FAULTS = [
    "latency", "rate_limit", "timeout", "corrupt", "unavailable",
    "tool_fault", "hallucinate", "stream_interrupt", "token_starve",
    "mcp_tool_error", "mcp_unavailable", "mcp_timeout",
]


def main() -> None:
    global FAULT, FAULT_ARGS

    p = argparse.ArgumentParser(
        description="Chaos Jungle LLM/MCP proxy",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--port", type=int, default=18000)
    p.add_argument("--upstream", default="https://api.openai.com")
    p.add_argument("--fault", required=True, choices=_ALL_FAULTS)

    # Fault-specific args
    p.add_argument("--latency-s", type=float, default=2.0)
    p.add_argument("--rate-limit-n", type=int, default=5)
    p.add_argument("--timeout-s", type=float, default=30.0)
    p.add_argument("--corrupt-mode", default="truncate",
                   choices=["truncate", "empty", "invalid_json"])
    p.add_argument("--tool-name", default="",
                   help="Tool name filter for tool_fault (empty = all tools)")
    p.add_argument("--hallucination-text",
                   default="WRONG ANSWER (injected by chaos-jungle)")
    p.add_argument("--stream-interrupt-after", type=int, default=3,
                   help="Number of SSE data events before stream is cut")
    p.add_argument("--token-starve-max", type=int, default=5,
                   help="max_tokens value injected by token_starve")

    args = p.parse_args()

    FAULT = args.fault
    FAULT_ARGS = {
        "upstream":         args.upstream,
        "delay_s":          args.latency_s,
        "n":                args.rate_limit_n,
        "timeout_s":        args.timeout_s,
        "mode":             args.corrupt_mode,
        "tool_name":        args.tool_name,
        "text":             args.hallucination_text,
        "interrupt_after":  args.stream_interrupt_after,
        "max_tokens":       args.token_starve_max,
    }

    server = HTTPServer(("127.0.0.1", args.port), _ProxyHandler)
    print(
        f"chaos-jungle proxy  fault={FAULT}  "
        f"port={args.port}  upstream={args.upstream}",
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
