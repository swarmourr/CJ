"""LLM agent chaos engineering examples.

Demonstrates every LLM and MCP fault type using a minimal OpenAI agent.

Requirements
------------
- chaos-jungle installed  (pip install chaos-jungle)
- openai installed        (pip install openai)
- OPENAI_API_KEY set in the environment

Run all examples::

    python examples/llm_agent.py

Run a single example::

    python examples/llm_agent.py latency
    python examples/llm_agent.py rate_limit
    python examples/llm_agent.py timeout
    python examples/llm_agent.py corrupt
    python examples/llm_agent.py unavailable
    python examples/llm_agent.py tool_fault
    python examples/llm_agent.py hallucinate
    python examples/llm_agent.py stream_interrupt
    python examples/llm_agent.py token_starve
    python examples/llm_agent.py mcp
    python examples/llm_agent.py measure
"""

from __future__ import annotations

import os
import sys
import time


# ---------------------------------------------------------------------------
# Minimal agent helper
# ---------------------------------------------------------------------------

def _call_agent(prompt: str, timeout: float = 10.0, stream: bool = False) -> str:
    """Send one message to the OpenAI API; return reply text or error string."""
    try:
        import openai  # noqa: PLC0415
        client = openai.OpenAI(timeout=timeout)
        if stream:
            chunks = []
            with client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                stream=True,
            ) as s:
                for chunk in s:
                    delta = chunk.choices[0].delta.content
                    if delta:
                        chunks.append(delta)
            return "".join(chunks)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001
        return f"[ERROR] {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Example 1 — LLMLatency
# ---------------------------------------------------------------------------

def example_latency() -> None:
    """3 s artificial latency on every call.

    Tests timeout budgets and retry compounding.
    """
    from chaos_jungle import ChaosRunner, Scenario
    from chaos_jungle.faults.llm import LLMLatency
    from chaos_jungle.targets import LocalTarget

    print("\n--- Example 1: LLMLatency (3 s) ---")
    runner = ChaosRunner(Scenario("llm-latency", [LLMLatency(delay_s=3.0)]), LocalTarget())

    t0 = time.perf_counter()
    baseline = _call_agent("What is 2+2?", timeout=15.0)
    print(f"  Baseline ({time.perf_counter()-t0:.2f}s): {baseline[:60]}")

    runner.start()
    t1 = time.perf_counter()
    chaos = _call_agent("What is 2+2?", timeout=15.0)
    print(f"  Chaos   ({time.perf_counter()-t1:.2f}s): {chaos[:60]}")
    runner.stop()


# ---------------------------------------------------------------------------
# Example 2 — LLMRateLimit
# ---------------------------------------------------------------------------

def example_rate_limit() -> None:
    """Allow 3 calls then return 429 for all subsequent calls."""
    from chaos_jungle import ChaosRunner, Scenario
    from chaos_jungle.faults.llm import LLMRateLimit
    from chaos_jungle.targets import LocalTarget

    print("\n--- Example 2: LLMRateLimit (n=3) ---")
    runner = ChaosRunner(Scenario("llm-rate-limit", [LLMRateLimit(n=3)]), LocalTarget())
    runner.start()
    for i, q in enumerate(["Planet?", "Country?", "Animal?", "Colour?", "Fruit?"], 1):
        r = _call_agent(q, timeout=8.0)
        print(f"  Call {i}: {'OK' if '[ERROR]' not in r else 'RATE LIMITED'} — {r[:50]}")
    runner.stop()


# ---------------------------------------------------------------------------
# Example 3 — LLMTimeout
# ---------------------------------------------------------------------------

def example_timeout() -> None:
    """Hold every connection for 5 s then return 504."""
    from chaos_jungle import ChaosRunner, Scenario
    from chaos_jungle.faults.llm import LLMTimeout
    from chaos_jungle.targets import LocalTarget

    print("\n--- Example 3: LLMTimeout (5 s) ---")
    runner = ChaosRunner(Scenario("llm-timeout", [LLMTimeout(timeout_s=5.0)]), LocalTarget())
    runner.start()
    t0 = time.perf_counter()
    r = _call_agent("Hello!", timeout=8.0)
    print(f"  Reply after {time.perf_counter()-t0:.1f}s: {r[:80]}")
    runner.stop()


# ---------------------------------------------------------------------------
# Example 4 — LLMResponseCorrupt
# ---------------------------------------------------------------------------

def example_corrupt() -> None:
    """Mangle the real API response in three different ways."""
    from chaos_jungle import ChaosRunner, Scenario
    from chaos_jungle.faults.llm import LLMResponseCorrupt
    from chaos_jungle.targets import LocalTarget

    print("\n--- Example 4: LLMResponseCorrupt ---")
    for mode in ("truncate", "empty", "invalid_json"):
        runner = ChaosRunner(
            Scenario(f"corrupt-{mode}", [LLMResponseCorrupt(mode=mode, port=18003)]),
            LocalTarget(),
        )
        runner.start()
        r = _call_agent("Say hello.", timeout=10.0)
        print(f"  [{mode:12}] {r[:70]}")
        runner.stop()


# ---------------------------------------------------------------------------
# Example 5 — LLMUnavailable
# ---------------------------------------------------------------------------

def example_unavailable() -> None:
    """Always return HTTP 503 — simulate complete outage."""
    from chaos_jungle import ChaosRunner, Scenario
    from chaos_jungle.faults.llm import LLMUnavailable
    from chaos_jungle.targets import LocalTarget

    print("\n--- Example 5: LLMUnavailable ---")
    runner = ChaosRunner(Scenario("llm-unavailable", [LLMUnavailable()]), LocalTarget())
    runner.start()
    for i in range(3):
        r = _call_agent(f"Attempt {i+1}", timeout=5.0)
        print(f"  Attempt {i+1}: {r[:80]}")
    runner.stop()


# ---------------------------------------------------------------------------
# Example 6 — ToolFault
# ---------------------------------------------------------------------------

def example_tool_fault() -> None:
    """Inject an error whenever the agent submits a tool result.

    The proxy detects requests containing role=tool messages and returns
    an API error instead of forwarding them.  Tests tool error recovery.
    """
    from chaos_jungle import ChaosRunner, Scenario
    from chaos_jungle.faults.llm import ToolFault
    from chaos_jungle.targets import LocalTarget

    print("\n--- Example 6: ToolFault (all tools) ---")
    runner = ChaosRunner(Scenario("tool-fault", [ToolFault()]), LocalTarget())
    runner.start()

    # Simulate the agent sending back a tool result
    try:
        import openai  # noqa: PLC0415
        client = openai.OpenAI(timeout=8.0)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "user", "content": "What is the weather in Paris?"},
                {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "call_1", "type": "function",
                                 "function": {"name": "get_weather",
                                              "arguments": '{"city":"Paris"}'}}]},
                # This tool result message triggers ToolFault
                {"role": "tool", "tool_call_id": "call_1",
                 "content": "Sunny, 22°C"},
            ],
            tools=[{
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get current weather",
                    "parameters": {"type": "object",
                                   "properties": {"city": {"type": "string"}},
                                   "required": ["city"]},
                },
            }],
        )
        print(f"  Response: {response.choices[0].message.content or 'no content'}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [INJECTED ERROR] {type(exc).__name__}: {exc}")

    runner.stop()


# ---------------------------------------------------------------------------
# Example 7 — LLMHallucination
# ---------------------------------------------------------------------------

def example_hallucinate() -> None:
    """Replace the real model answer with injected wrong text."""
    from chaos_jungle import ChaosRunner, Scenario
    from chaos_jungle.faults.llm import LLMHallucination
    from chaos_jungle.targets import LocalTarget

    print("\n--- Example 7: LLMHallucination ---")
    fault = LLMHallucination(
        inject_text="The capital of France is Berlin.",
        port=18004,
    )
    runner = ChaosRunner(Scenario("hallucinate", [fault]), LocalTarget())

    baseline = _call_agent("What is the capital of France?", timeout=10.0)
    print(f"  Baseline:    {baseline[:70]}")

    runner.start()
    hallucinated = _call_agent("What is the capital of France?", timeout=10.0)
    print(f"  Hallucinated: {hallucinated[:70]}")
    runner.stop()


# ---------------------------------------------------------------------------
# Example 8 — LLMStreamInterrupt
# ---------------------------------------------------------------------------

def example_stream_interrupt() -> None:
    """Cut a streaming response after 2 SSE data events."""
    from chaos_jungle import ChaosRunner, Scenario
    from chaos_jungle.faults.llm import LLMStreamInterrupt
    from chaos_jungle.targets import LocalTarget

    print("\n--- Example 8: LLMStreamInterrupt (after 2 events) ---")
    runner = ChaosRunner(
        Scenario("stream-interrupt", [LLMStreamInterrupt(interrupt_after=2, port=18005)]),
        LocalTarget(),
    )
    runner.start()
    r = _call_agent("Write a short poem about chaos.", timeout=15.0, stream=True)
    print(f"  Partial response: {r[:120]!r}")
    runner.stop()


# ---------------------------------------------------------------------------
# Example 9 — LLMTokenStarvation
# ---------------------------------------------------------------------------

def example_token_starve() -> None:
    """Force max_tokens=5, making the model return a truncated answer."""
    from chaos_jungle import ChaosRunner, Scenario
    from chaos_jungle.faults.llm import LLMTokenStarvation
    from chaos_jungle.targets import LocalTarget

    print("\n--- Example 9: LLMTokenStarvation (max_tokens=5) ---")
    runner = ChaosRunner(
        Scenario("token-starve", [LLMTokenStarvation(max_tokens=5, port=18006)]),
        LocalTarget(),
    )

    baseline = _call_agent("Explain quantum computing in one sentence.", timeout=10.0)
    print(f"  Baseline:  {baseline[:100]}")

    runner.start()
    starved = _call_agent("Explain quantum computing in one sentence.", timeout=10.0)
    print(f"  Starved:   {starved[:100]!r}")
    runner.stop()


# ---------------------------------------------------------------------------
# Example 10 — MCPFault
# ---------------------------------------------------------------------------

def example_mcp() -> None:
    """Inject failures into an MCP server.

    Assumes an MCP server is running on localhost:3000.
    If not running, shows the error path.
    """
    from chaos_jungle import ChaosRunner, Scenario
    from chaos_jungle.faults.llm import MCPFault
    from chaos_jungle.targets import LocalTarget

    print("\n--- Example 10: MCPFault ---")
    for mode in ("tool_error", "unavailable", "timeout"):
        fault = MCPFault(
            mode=mode,
            timeout_s=3.0,
            port=18100 + list(("tool_error", "unavailable", "timeout")).index(mode),
            upstream="http://localhost:3000",
            base_url_env="MCP_SERVER_URL",
        )
        runner = ChaosRunner(Scenario(f"mcp-{mode}", [fault]), LocalTarget())
        runner.start()

        # Simulate an MCP JSON-RPC call
        import urllib.error  # noqa: PLC0415
        import urllib.request  # noqa: PLC0415
        import json  # noqa: PLC0415
        mcp_url = os.environ.get("MCP_SERVER_URL", f"http://127.0.0.1:{fault.port}")
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {"name": "search", "arguments": {"query": "chaos engineering"}},
        }).encode()
        req = urllib.request.Request(
            mcp_url + "/rpc",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read())
                print(f"  [{mode:12}] response: {result}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [{mode:12}] {type(exc).__name__}: {str(exc)[:60]}")

        runner.stop()


# ---------------------------------------------------------------------------
# Example 11 — @chaos_measure with LLM fault
# ---------------------------------------------------------------------------

def example_measure() -> None:
    """Measure agent performance under rate limiting with @chaos_measure."""
    from chaos_jungle.decorators import chaos_measure
    from chaos_jungle.faults.llm import LLMRateLimit

    print("\n--- Example 11: @chaos_measure + LLMRateLimit ---")

    @chaos_measure(LLMRateLimit(n=2, port=18001), scenario_name="llm-measure")
    def run_agent_task() -> dict:
        replies = []
        for q in ["Capital of France?", "10 × 7?", "Who wrote Hamlet?"]:
            r = _call_agent(q, timeout=8.0)
            replies.append(r[:40])
        return {
            "queries": len(replies),
            "errors": sum(1 for r in replies if "[ERROR]" in r),
        }

    summary = run_agent_task()
    print(f"  Duration: {summary['duration_s']:.2f}s")
    print(f"  Result:   {summary.get('result')}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_EXAMPLES = {
    "latency":          example_latency,
    "rate_limit":       example_rate_limit,
    "timeout":          example_timeout,
    "corrupt":          example_corrupt,
    "unavailable":      example_unavailable,
    "tool_fault":       example_tool_fault,
    "hallucinate":      example_hallucinate,
    "stream_interrupt": example_stream_interrupt,
    "token_starve":     example_token_starve,
    "mcp":              example_mcp,
    "measure":          example_measure,
}

if __name__ == "__main__":
    key = sys.argv[1] if len(sys.argv) > 1 else None

    if key and key not in _EXAMPLES:
        print(f"Unknown example {key!r}. Choose from: {', '.join(_EXAMPLES)}")
        sys.exit(1)

    targets = {key: _EXAMPLES[key]} if key else _EXAMPLES

    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "WARNING: OPENAI_API_KEY is not set.  Calls to the real API will fail,\n"
            "  but the proxy and fault injection paths will still be demonstrated.\n"
        )

    for name, fn in targets.items():
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAILED] {name}: {exc}")

    print("\nAll examples completed.")
