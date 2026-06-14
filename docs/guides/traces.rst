.. _guide-traces:

Trace Capture & Replay
======================

chaos-jungle records a structured **trace** of every LLM interaction
boundary event — prompts sent, responses received, tool calls, token
usage, retries, and oracle assertion results.

Traces are stored in a ``traces`` table in the session SQLite database
(``~/.chaos-jungle/chaos_jungle.db``) and are the foundation for:

* **Oracle replay** — re-run oracle assertions against a stored session
  without re-injecting the fault.
* **Debugging** — inspect the full prompt/response exchange after a run
  fails unexpectedly.
* **Regression testing** — verify that a bug fix changed agent behaviour
  as expected.
* **Audit** — keep an immutable record of what the agent said during
  fault conditions.

----

Recording trace events
----------------------

Events are written automatically by :meth:`ChaosRunner.measure` when
``oracle_results`` are present.  Your workload callable can also emit
trace events directly via the session database:

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario
   from chaos_jungle.faults import LLMLatency
   from chaos_jungle.targets import LocalTarget

   runner = ChaosRunner(
       Scenario("trace-demo", [LLMLatency(delay_s=2.0)]),
       LocalTarget(),
   )

   def workload():
       import time
       t0 = time.time()
       response = call_my_agent("What is 2 + 2?")
       elapsed = time.time() - t0

       # Emit a trace event manually (optional)
       if runner._session_id is not None:
           runner.db.add_trace_event(
               runner._session_id,
               "response",
               {
                   "question":    "What is 2 + 2?",
                   "response":    response,
                   "tokens_used": 120,
                   "cost_usd":    0.0002,
                   "duration_s":  elapsed,
                   "retries":     0,
               },
           )
       return {"duration_s": elapsed, "response": response}

   result = runner.measure(workload, n_fault=3)

----

Trace event kinds
-----------------

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Kind
     - Recommended ``data`` keys
   * - ``"prompt"``
     - ``question``, ``context``, ``messages`` (full message list)
   * - ``"response"``
     - ``response``, ``tokens_used``, ``cost_usd``, ``model``,
       ``finish_reason``, ``duration_s``, ``retries``
   * - ``"tool_call"``
     - ``tool_name``, ``args``
   * - ``"tool_return"``
     - ``tool_name``, ``output``, ``duration_s``
   * - ``"retry"``
     - ``attempt``, ``reason``, ``error``
   * - ``"oracle_result"``
     - ``oracle``, ``passed``, ``score``, ``reason``, ``phase``
       (written automatically by ``measure()``)

----

Reading traces
--------------

.. code-block:: python

   # After a run
   runner.stop()

   trace = runner.db.get_trace(runner._session_id)
   for event in trace:
       print(event["seq"], event["kind"], event["data"])

   # Filter to responses only
   responses = runner.db.get_trace_events_by_kind(
       runner._session_id, "response"
   )
   for r in responses:
       print(r["data"]["response"][:80])

----

Replaying oracle assertions
---------------------------

Re-run oracle assertions against a stored trace **without** re-injecting
the fault:

.. code-block:: python

   from chaos_jungle.db.session_db import SessionDB
   from chaos_jungle.oracles import NoPIILeakage, MaxCost, run_oracles

   db = SessionDB()

   # Load stored response events
   trace  = db.get_trace(session_id=12)
   events = [e["data"] for e in trace if e["kind"] == "response"]

   # Re-run oracles
   results = run_oracles(
       [NoPIILeakage(), MaxCost(max_usd=0.05)],
       events,
       phase="fault",
   )

   for r in results:
       status = "PASS" if r.passed else "FAIL"
       print(f"[{status}] {r.oracle}: {r.reason}")

This is valuable when:

* You add a new oracle after a run has already finished — replay it
  against the stored trace instead of re-running the experiment.
* A bug was fixed and you want to verify the fix against the original
  failure trace.
* You want to run a stricter oracle retroactively (lower ``MaxCost``
  budget, stricter PII patterns).

----

Exporting traces
----------------

The full session export (``runner.export()``) includes trace events:

.. code-block:: python

   import json

   data = runner.export(fmt="dict")
   print(json.dumps(data["trace_events"], indent=2))

   # Or export to JSON file:
   with open("session.json", "w") as f:
       f.write(runner.export(fmt="json"))

----

See also
--------

* :ref:`guide-oracles` — built-in oracle assertions
* :ref:`guide-safety` — ``SafetyPolicy`` and danger levels
* :ref:`guide-measurement` — ``runner.measure()`` API
