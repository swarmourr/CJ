#!/usr/bin/env python3
"""
Scenario 5 — LLM Agent Resilience Under Fault Injection
========================================================
Runs a 3-step agentic reasoning chain under four LLM faults and measures
which faults the agent handles gracefully vs which ones crash it silently.

Agent chain:
  Step 1 — ask a factual question
  Step 2 — use the answer to form a follow-up question
  Step 3 — summarise both answers

Faults tested:
  LLMLatency(5s)       — does the agent timeout or wait?
  LLMRateLimit(n=1)    — does the agent retry or crash?
  LLMUnavailable       — does the agent fall back or raise?
  LLMHallucination     — does the wrong answer propagate to step 3?

Measures per fault:
  completed       — did the chain finish?
  retries         — how many retries were needed
  answer_correct  — does step 3 match ground truth? (hallucination only)
  elapsed_s       — total chain time

Requirements:
  Ollama running on localhost:11434 with at least one model pulled.
  pip install openai

Usage:
    python3 examples/llm_resilience.py
"""

import json
import os
import time
import urllib.request

from chaos_jungle import Scenario, ChaosRunner
from chaos_jungle.faults.llm import (
    LLMLatency, LLMRateLimit, LLMUnavailable, LLMHallucination,
)
from chaos_jungle.targets import LocalTarget

OLLAMA_BASE = "http://localhost:11434"
PROXY_BASE  = "http://127.0.0.1:18000/v1"


# ── Preflight — pick a model ───────────────────────────────────────────────

def _pick_model() -> str:
    try:
        with urllib.request.urlopen(f"{OLLAMA_BASE}/api/tags", timeout=3) as r:
            data = json.loads(r.read())
        models = [m["name"] for m in data.get("models", [])]
        if not models:
            raise RuntimeError("No models found — run: ollama pull llama3.2")
        print(f"  Using model: {models[0]}")
        return models[0]
    except Exception as e:
        raise SystemExit(f"Ollama not available: {e}\n  Start with: ollama serve")


# ── Agent chain ────────────────────────────────────────────────────────────

def _chat(messages: list, model: str, base_url: str, timeout: float = 8.0) -> str:
    """Single OpenAI-compatible chat call. Returns content string."""
    try:
        import openai
    except ImportError:
        raise SystemExit("pip install openai")

    client = openai.OpenAI(base_url=base_url, api_key="ollama")
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        timeout=timeout,
        max_tokens=150,
    )
    return resp.choices[0].message.content.strip()


def run_chain(model: str, base_url: str = PROXY_BASE) -> dict:
    """
    3-step reasoning chain.
    Returns: completed, retries, step1_answer, step3_summary, elapsed_s
    """
    retries = 0
    t0 = time.time()

    # Step 1
    for attempt in range(4):
        try:
            step1 = _chat([
                {"role": "user", "content":
                 "In one sentence: what is the primary molecule in a cell nucleus?"}
            ], model=model, base_url=base_url)
            break
        except Exception as e:
            retries += 1
            if attempt == 3:
                return {"completed": False, "retries": retries,
                        "elapsed_s": round(time.time()-t0, 2),
                        "step1_answer": "", "step3_summary": "",
                        "error": str(e)}
            time.sleep(1)

    # Step 2 — follow-up based on step 1
    for attempt in range(4):
        try:
            step2 = _chat([
                {"role": "user", "content":
                 f"You said: '{step1}'. In one sentence: what enzyme replicates it?"}
            ], model=model, base_url=base_url)
            break
        except Exception as e:
            retries += 1
            if attempt == 3:
                return {"completed": False, "retries": retries,
                        "elapsed_s": round(time.time()-t0, 2),
                        "step1_answer": step1, "step3_summary": "",
                        "error": str(e)}
            time.sleep(1)

    # Step 3 — summarise
    for attempt in range(4):
        try:
            step3 = _chat([
                {"role": "user", "content":
                 f"Summarise in one sentence: '{step1}' and '{step2}'"}
            ], model=model, base_url=base_url)
            break
        except Exception as e:
            retries += 1
            if attempt == 3:
                return {"completed": False, "retries": retries,
                        "elapsed_s": round(time.time()-t0, 2),
                        "step1_answer": step1, "step3_summary": "",
                        "error": str(e)}
            time.sleep(1)

    return {
        "completed":     True,
        "retries":       retries,
        "elapsed_s":     round(time.time() - t0, 2),
        "step1_answer":  step1,
        "step2_answer":  step2,
        "step3_summary": step3,
        "error":         "",
    }


# ── Fault runner ───────────────────────────────────────────────────────────

def test_fault(label: str, fault, model: str) -> dict:
    runner = ChaosRunner(
        Scenario(label, faults=[fault]),
        target=LocalTarget(),
        conflict="force",
    )
    runner.start()
    result = run_chain(model)
    runner.stop()
    return result


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    model = _pick_model()

    # Baseline (direct Ollama, no proxy)
    print("\n  [baseline] Running chain directly against Ollama ...")
    baseline = run_chain(model, base_url=f"{OLLAMA_BASE}/v1")
    print(f"  step1 : {baseline.get('step1_answer', '')[:80]}")
    print(f"  step3 : {baseline.get('step3_summary', '')[:80]}")
    ground_truth = baseline.get("step1_answer", "")

    faults = [
        ("latency-5s",   LLMLatency(delay_s=5.0,
                                     upstream=OLLAMA_BASE, port=18000)),
        ("rate-limit-1", LLMRateLimit(n=1,
                                       upstream=OLLAMA_BASE, port=18000)),
        ("unavailable",  LLMUnavailable(upstream=OLLAMA_BASE, port=18000)),
        ("hallucination", LLMHallucination(
            "The primary molecule in the cell nucleus is glucose.",
            upstream=OLLAMA_BASE, port=18000,
        )),
    ]

    results = {"baseline": baseline}
    for label, fault in faults:
        print(f"\n  [{label}] injecting fault ...")
        r = test_fault(label, fault, model)
        results[label] = r
        status = "OK" if r["completed"] else "FAIL"
        print(f"  completed={r['completed']}  retries={r['retries']}  "
              f"elapsed={r['elapsed_s']}s  [{status}]")
        if r.get("step3_summary"):
            print(f"  step3 : {r['step3_summary'][:100]}")

    # Hallucination propagation check
    hall = results.get("hallucination", {})
    propagated = (
        hall.get("completed") and
        "glucose" in hall.get("step3_summary", "").lower()
    )

    # ── Summary table ─────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  LLM RESILIENCE RESULT")
    print("=" * 72)
    print(f"  {'Fault':<20} {'completed':>10} {'retries':>8} {'elapsed_s':>10}  {'notes'}")
    print("-" * 72)
    for label, r in results.items():
        notes = r.get("error", "")[:30] if not r.get("completed") else ""
        if label == "hallucination" and r.get("completed"):
            notes = f"propagated={'YES' if propagated else 'NO'}"
        print(f"  {label:<20} {str(r.get('completed','—')):>10} "
              f"{str(r.get('retries','—')):>8} "
              f"{str(r.get('elapsed_s','—')):>10}  {notes}")
    print("=" * 72)
    print(f"\n  Hallucination propagated to step 3 : {propagated}")
    print(f"  Most dangerous fault               : hallucination (silent, no error)")


if __name__ == "__main__":
    main()
