"""Deterministic fake model server for automated tests and dry-runs.

Starts a lightweight stdlib HTTP server on a random port that responds to
POST /chat/completions with a deterministic Python solution derived from
the task description hash.  No real model API is contacted.

Usage::

    from evaluation.agents.fake_model import FakeModelServer
    with FakeModelServer() as srv:
        os.environ["CJ_EVAL_BASE_URL"] = srv.base_url
        os.environ["CJ_EVAL_API_KEY"]  = "dummy"
        # ... run evaluation ...

The server also exposes GET /_fake/calls to return the list of received
requests (useful for asserting call counts in tests).
"""

from __future__ import annotations

import hashlib
import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


def _deterministic_solution(prompt: str) -> str:
    """Return a deterministic Python solution stub keyed on the prompt hash.

    For genuine correctness testing the real model is needed.  The fake
    server returns syntactically valid Python that passes import and exec
    checks but may not pass all benchmark tests — this is intentional and
    matches the dry-run / mock contract.
    """
    h = hashlib.sha256(prompt.encode()).hexdigest()[:8]
    # Emit a function that returns a plausible value for common patterns
    if "factorial" in prompt.lower():
        body = "    import math\n    return math.factorial(n)"
        sig  = "def factorial(n):"
    elif "fibonacci" in prompt.lower() or "fib" in prompt.lower():
        body = "    if n <= 1: return n\n    a, b = 0, 1\n    for _ in range(n - 1): a, b = b, a + b\n    return b"
        sig  = "def fib(n):"
    elif "sort" in prompt.lower():
        body = "    return sorted(lst)"
        sig  = "def sort_list(lst):"
    elif "sum" in prompt.lower() or "add" in prompt.lower():
        body = "    return sum(lst) if hasattr(lst, '__iter__') else a + b"
        sig  = "def add(a, b=0, lst=None):"
    elif "reverse" in prompt.lower():
        body = "    return s[::-1]"
        sig  = "def reverse(s):"
    elif "max" in prompt.lower():
        body = "    return max(lst)"
        sig  = "def find_max(lst):"
    elif "min" in prompt.lower():
        body = "    return min(lst)"
        sig  = "def find_min(lst):"
    else:
        body = f"    # stub-{h}\n    return None"
        sig  = "def solution(*args, **kwargs):"

    return f"```python\n{sig}\n{body}\n```"


class _Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args) -> None:  # silence default logging
        pass

    def do_GET(self) -> None:
        if self.path == "/_fake/calls":
            calls = self.server._calls  # type: ignore[attr-defined]
            body  = json.dumps(calls).encode()
            self._send(200, body, "application/json")
        elif self.path == "/_cj/health":
            self._send(200, b'{"status":"ok"}', "application/json")
        elif self.path == "/_fake/reset":
            self.server._calls.clear()  # type: ignore[attr-defined]
            self._send(200, b'{"reset":true}', "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(length)
        try:
            body = json.loads(body_bytes)
        except json.JSONDecodeError:
            self._send(400, b"bad json", "text/plain")
            return

        # Record the call for inspection
        self.server._calls.append(body)  # type: ignore[attr-defined]

        # Build deterministic response
        messages  = body.get("messages", [])
        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        content = _deterministic_solution(last_user)

        resp: dict[str, Any] = {
            "id":      "fake-completion-001",
            "object":  "chat.completion",
            "model":   body.get("model", "fake"),
            "choices": [{
                "index":         0,
                "message":       {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens":     len(last_user.split()),
                "completion_tokens": len(content.split()),
                "total_tokens":      len(last_user.split()) + len(content.split()),
            },
        }
        self._send(200, json.dumps(resp).encode(), "application/json")

    def _send(self, code: int, body: bytes, ct: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeModelServer:
    """Context manager that starts a local fake model server.

    Attributes
    ----------
    base_url : str
        The base URL (``http://127.0.0.1:<port>``) to set as CJ_EVAL_BASE_URL.
    calls : list[dict]
        Live list of all received request bodies (shared reference).
    """

    def __init__(self, port: int = 0) -> None:
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port    = port
        self.base_url = ""
        self.calls: list[dict] = []

    def start(self) -> "FakeModelServer":
        self._server = HTTPServer(("127.0.0.1", self.port), _Handler)
        self._server._calls = self.calls  # type: ignore[attr-defined]
        self.port    = self._server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="fake-model"
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def reset_calls(self) -> None:
        self.calls.clear()

    def __enter__(self) -> "FakeModelServer":
        return self.start()

    def __exit__(self, *args) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# ToolAwareFakeServer — simulates function/tool-calling for ToolFault tests
# ---------------------------------------------------------------------------

class _ToolAwareHandler(BaseHTTPRequestHandler):
    """Handler that returns ``tool_calls`` on the first request per conversation,
    then returns a text completion when it detects ``role="tool"`` messages.

    This lets integration tests verify that:
      1. The framework requests tool execution (``tool_calls`` in response).
      2. The framework sends back the tool result as ``role="tool"``.
      3. The CJ proxy intercepts that second request (ToolFault activation).
    """

    def log_message(self, fmt, *args) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/_fake/calls":
            calls = self.server._calls  # type: ignore[attr-defined]
            body  = json.dumps(calls).encode()
            self._send(200, body, "application/json")
        elif self.path == "/_fake/tool_calls":
            calls = self.server._tool_call_requests  # type: ignore[attr-defined]
            body  = json.dumps(calls).encode()
            self._send(200, body, "application/json")
        elif self.path in ("/_cj/health", "/_fake/reset"):
            if "reset" in self.path:
                self.server._calls.clear()  # type: ignore[attr-defined]
                self.server._tool_call_requests.clear()  # type: ignore[attr-defined]
            self._send(200, b'{"status":"ok"}', "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(length)
        try:
            body = json.loads(body_bytes)
        except json.JSONDecodeError:
            self._send(400, b"bad json", "text/plain")
            return

        self.server._calls.append(body)  # type: ignore[attr-defined]
        messages   = body.get("messages", [])
        tool_name  = self.server._tool_name  # type: ignore[attr-defined]
        has_tool_msg = any(m.get("role") == "tool" for m in messages)

        if has_tool_msg:
            # Second request: the framework returned a tool result.
            # Record it for test assertions and return a final text answer.
            self.server._tool_call_requests.append(body)  # type: ignore[attr-defined]
            content = "```python\ndef solution(*args, **kwargs):\n    return None\n```"
            resp: dict = {
                "id":      "fake-tool-completion",
                "object":  "chat.completion",
                "model":   body.get("model", "fake"),
                "choices": [{
                    "index":         0,
                    "message":       {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 20, "completion_tokens": 15, "total_tokens": 35},
            }
        else:
            # First request: tell the model to call the registered tool.
            resp = {
                "id":      "fake-tool-request",
                "object":  "chat.completion",
                "model":   body.get("model", "fake"),
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id":   "call_fake_001",
                            "type": "function",
                            "function": {
                                "name":      tool_name,
                                "arguments": json.dumps({"code": "print('test')"}),
                            },
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 15, "completion_tokens": 8, "total_tokens": 23},
            }

        self._send(200, json.dumps(resp).encode(), "application/json")

    def _send(self, code: int, body: bytes, ct: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ToolAwareFakeServer:
    """Fake model server that simulates function/tool calling.

    Used to test the full tool-call chain:

        Framework agent
          → request 1 (no tool messages) → server returns tool_calls response
          → framework executes tool
          → request 2 (role="tool") → CJ ToolFault intercepts OR server answers
          → framework returns final result

    Attributes
    ----------
    base_url : str
        HTTP base URL to set as ``CJ_EVAL_BASE_URL``.
    calls : list[dict]
        All received request bodies.
    tool_call_requests : list[dict]
        Only requests that contained ``role="tool"`` messages.
    """

    def __init__(self, tool_name: str = "execute_python_code", port: int = 0) -> None:
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port      = port
        self.base_url  = ""
        self.tool_name = tool_name
        self.calls: list[dict] = []
        self.tool_call_requests: list[dict] = []

    def start(self) -> "ToolAwareFakeServer":
        self._server = HTTPServer(("127.0.0.1", self.port), _ToolAwareHandler)
        self._server._calls              = self.calls               # type: ignore[attr-defined]
        self._server._tool_call_requests = self.tool_call_requests  # type: ignore[attr-defined]
        self._server._tool_name          = self.tool_name           # type: ignore[attr-defined]
        self.port     = self._server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread  = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="tool-fake-model"
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def reset(self) -> None:
        self.calls.clear()
        self.tool_call_requests.clear()

    def __enter__(self) -> "ToolAwareFakeServer":
        return self.start()

    def __exit__(self, *args) -> None:
        self.stop()
