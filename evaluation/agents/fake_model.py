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
