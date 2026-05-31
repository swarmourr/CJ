"""LLM agent chaos engineering examples.

Demonstrates how to inject every LLM fault type into a minimal OpenAI
agent using chaos-jungle.  Each example function can be run standalone.

Requirements
------------
- chaos-jungle installed (pip install chaos-jungle)
- openai installed (pip install openai)
- OPENAI_API_KEY set in the environment

Run all examples::

    python examples/llm_agent.py

Run a single example::

    python examples/llm_agent.py latency
    python examples/llm_agent.py rate_limit
    python examples/llm_agent.py timeout
    python examples/llm_agent.py corrupt
    python examples/llm_agent.py unavailable
"""

from __future__ import annotations

import os
import sys
import time

# ---------------------------------------------------------------------------
# Minimal agent helper — works without an actual API key for demo purposes
# ---------------------------------------------------------------------------

def _call_agent(prompt: str, timeout: float = 10.0) -> str:
    """Send one message to the OpenAI API and return the reply text.

    Returns an error string instead of raising so examples stay readable.
    """
    try:
        import openai  # noqa: PLC0415
        client = openai.OpenAI(timeout=timeout)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001
        return f"[ERROR] {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Example 1 — Latency
# ---------------------------------------------------------------------------

def example_latency() -> None:
    """Inject 3 s artificial latency into every LLM call.

    Tests whether the agent's timeout budget and retry logic hold up when
    the model responds slowly.
    """
    from chaos_jungle import ChaosRunner, Scenario
    from chaos_jungle.faults.llm import LLMLatency
    from chaos_jungle.targets import LocalTarget

    print("\n--- Example 1: LLMLatency (3 s per call) ---")

    fault = LLMLatency(delay_s=3.0)
    runner = ChaosRunner(
        Scenario("llm-latency", [fault]),
        target=LocalTarget(),
    )

    # Baseline: measure normal call time
    t0 = time.perf_counter()
    baseline = _call_agent("What is 2 + 2?", timeout=15.0)
    baseline_s = time.perf_counter() - t0
    print(f"  Baseline response ({baseline_s:.2f}s): {baseline[:80]}")

    # Start fault
    runner.start()
    print("  [fault active] calling agent through proxy …")
    t1 = time.perf_counter()
    chaos_reply = _call_agent("What is 2 + 2?", timeout=15.0)
    chaos_s = time.perf_counter() - t1
    print(f"  Chaos response ({chaos_s:.2f}s): {chaos_reply[:80]}")
    runner.stop()

    print(f"  Slowdown: +{chaos_s - baseline_s:.2f}s  (expected ~3s)")


# ---------------------------------------------------------------------------
# Example 2 — Rate Limit
# ---------------------------------------------------------------------------

def example_rate_limit() -> None:
    """Allow 3 requests then return HTTP 429 for all subsequent calls.

    Tests back-off strategies, retry-after handling, and error messages
    shown to the end user.
    """
    from chaos_jungle import ChaosRunner, Scenario
    from chaos_jungle.faults.llm import LLMRateLimit
    from chaos_jungle.targets import LocalTarget

    print("\n--- Example 2: LLMRateLimit (n=3) ---")

    fault = LLMRateLimit(n=3)
    runner = ChaosRunner(
        Scenario("llm-rate-limit", [fault]),
        target=LocalTarget(),
    )

    runner.start()
    prompts = [
        "Name a planet.",
        "Name a country.",
        "Name an animal.",
        "Name a colour.",   # 4th call — will be rate-limited
        "Name a fruit.",    # 5th call — will be rate-limited
    ]
    for i, prompt in enumerate(prompts, start=1):
        reply = _call_agent(prompt, timeout=10.0)
        status = "OK" if "[ERROR]" not in reply else "RATE LIMITED"
        print(f"  Call {i}: [{status}] {reply[:60]}")
    runner.stop()


# ---------------------------------------------------------------------------
# Example 3 — Timeout
# ---------------------------------------------------------------------------

def example_timeout() -> None:
    """Hold every connection for 5 s then return HTTP 504.

    Tests the agent's ability to detect hangs, cancel in-flight requests,
    and surface a meaningful error rather than waiting forever.
    """
    from chaos_jungle import ChaosRunner, Scenario
    from chaos_jungle.faults.llm import LLMTimeout
    from chaos_jungle.targets import LocalTarget

    print("\n--- Example 3: LLMTimeout (5 s hang) ---")

    fault = LLMTimeout(timeout_s=5.0)
    runner = ChaosRunner(
        Scenario("llm-timeout", [fault]),
        target=LocalTarget(),
    )

    runner.start()
    print("  [fault active] agent will hang for ~5 s …")
    t0 = time.perf_counter()
    reply = _call_agent("Hello!", timeout=8.0)
    elapsed = time.perf_counter() - t0
    print(f"  Reply after {elapsed:.1f}s: {reply[:80]}")
    runner.stop()


# ---------------------------------------------------------------------------
# Example 4 — Response Corruption
# ---------------------------------------------------------------------------

def example_corrupt() -> None:
    """Return a corrupted (truncated) JSON response from the real API.

    The request reaches the real LLM but the response is cut in half before
    the agent sees it.  Tests JSON-parse error handling, partial-response
    recovery, and retry logic.
    """
    from chaos_jungle import ChaosRunner, Scenario
    from chaos_jungle.faults.llm import LLMResponseCorrupt
    from chaos_jungle.targets import LocalTarget

    print("\n--- Example 4: LLMResponseCorrupt (truncate) ---")

    # Try each corruption mode
    for mode in ("truncate", "empty", "invalid_json"):
        fault = LLMResponseCorrupt(mode=mode, port=18003)
        runner = ChaosRunner(
            Scenario(f"llm-corrupt-{mode}", [fault]),
            target=LocalTarget(),
        )
        runner.start()
        reply = _call_agent("Say hello.", timeout=10.0)
        print(f"  [{mode}] reply: {reply[:80]}")
        runner.stop()


# ---------------------------------------------------------------------------
# Example 5 — Unavailable
# ---------------------------------------------------------------------------

def example_unavailable() -> None:
    """Simulate a complete LLM API outage — always return HTTP 503.

    Tests fallback behaviour, user-facing error messages, and whether the
    agent retries indefinitely (a common bug) or fails fast.
    """
    from chaos_jungle import ChaosRunner, Scenario
    from chaos_jungle.faults.llm import LLMUnavailable
    from chaos_jungle.targets import LocalTarget

    print("\n--- Example 5: LLMUnavailable (always 503) ---")

    fault = LLMUnavailable()
    runner = ChaosRunner(
        Scenario("llm-unavailable", [fault]),
        target=LocalTarget(),
    )

    runner.start()
    for i in range(3):
        reply = _call_agent(f"Attempt {i + 1}", timeout=5.0)
        print(f"  Attempt {i + 1}: {reply[:80]}")
    runner.stop()


# ---------------------------------------------------------------------------
# Example 6 — Using @chaos_measure with LLM faults
# ---------------------------------------------------------------------------

def example_chaos_measure() -> None:
    """Measure agent performance under rate limiting with @chaos_measure.

    The decorator runs the function under chaos, collects baseline metrics,
    and records the result automatically to the local SQLite database.
    """
    from chaos_jungle.decorators import chaos_measure
    from chaos_jungle.faults.llm import LLMRateLimit

    print("\n--- Example 6: @chaos_measure with LLMRateLimit ---")

    @chaos_measure(LLMRateLimit(n=2, port=18001), scenario_name="llm-measure")
    def run_agent_task() -> dict:
        replies = []
        for q in ["What is the capital of France?", "What is 10 * 7?", "Who wrote Hamlet?"]:
            reply = _call_agent(q, timeout=8.0)
            replies.append(reply[:40])
        return {"queries": len(replies), "errors": sum(1 for r in replies if "[ERROR]" in r)}

    summary = run_agent_task()
    print(f"  Duration: {summary['duration_s']:.2f}s")
    print(f"  Result:   {summary.get('result')}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_EXAMPLES = {
    "latency":     example_latency,
    "rate_limit":  example_rate_limit,
    "timeout":     example_timeout,
    "corrupt":     example_corrupt,
    "unavailable": example_unavailable,
    "measure":     example_chaos_measure,
}

if __name__ == "__main__":
    key = sys.argv[1] if len(sys.argv) > 1 else None

    if key and key not in _EXAMPLES:
        print(f"Unknown example {key!r}. Choose from: {', '.join(_EXAMPLES)}")
        sys.exit(1)

    targets = {key: _EXAMPLES[key]} if key else _EXAMPLES

    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "WARNING: OPENAI_API_KEY is not set.  Calls to the real API will fail.\n"
            "  The fault proxy itself will still start and demonstrate the error paths.\n"
        )

    for name, fn in targets.items():
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAILED] {name}: {exc}")

    print("\nAll examples completed.")
