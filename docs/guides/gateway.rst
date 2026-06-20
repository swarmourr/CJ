.. _guide-gateway:

AI Gateway Faults
=================

Most production AI systems do not call the model provider directly.  They
route through an **AI gateway** — LiteLLM, Portkey, OpenRouter, Kong AI
Gateway, Helicone, a custom FastAPI proxy, or an internal routing layer.

When the gateway fails, the failure mode is completely different from a
provider failure:

* A provider failure returns an obvious 503 or timeout.
* A **gateway failure** silently routes to the wrong model, returns a stale
  cached answer, strips authentication headers, or leaks another tenant's
  data — all while returning HTTP 200.

chaos-jungle's gateway faults inject these failures at the HTTP transport
layer using the same zero-setup intercept mechanism as :ref:`guide-intercept`.
No subprocess, no proxy port, no Linux required.

----

How it works
------------

Every gateway fault is both a :class:`~chaos_jungle.intercept.Behavior`
(works with ``inject()``) *and* a :class:`~chaos_jungle.faults.base.Fault`
(works with ``ChaosRunner`` / ``Scenario``).  Pick whichever API fits your
workflow:

.. code-block:: python

   from chaos_jungle.faults.gateway import GatewayRouteMisconfig, GatewayCacheStale

   # Option A — with ChaosRunner (records to session DB)
   from chaos_jungle import Scenario, ChaosRunner, LocalTarget

   runner = ChaosRunner(
       Scenario("gateway-chaos", [
           GatewayRouteMisconfig(from_model="gpt-4o", to_model="gpt-3.5-turbo"),
           GatewayCacheStale(stale_response="Paris (cached 7 days ago)", stale_age="7d"),
       ]),
       LocalTarget(),
   )
   runner.start()
   result = agent.run("What is the capital of France?")
   runner.stop()

   # Option B — with inject() (lightest weight)
   from chaos_jungle.intercept import inject

   with inject(GatewayRouteMisconfig(to_model="gpt-3.5-turbo")):
       result = agent.run("question")

----

Fault reference
---------------

GatewayRouteMisconfig
~~~~~~~~~~~~~~~~~~~~~

Rewrites the ``model`` field in every outgoing request to a different model.

The LLM responds normally — the failure is silent and only detectable by
inspecting the ``model`` field in the response.  Pair with
:class:`~chaos_jungle.oracles.ModelMatchOracle` to assert the right model
was used.

.. code-block:: python

   from chaos_jungle.faults.gateway import GatewayRouteMisconfig
   from chaos_jungle.oracles import ModelMatchOracle

   with inject(GatewayRouteMisconfig(from_model="gpt-4o", to_model="gpt-3.5-turbo")):
       result = agent.run("Summarise this 50-page contract")

   # Assert that quality drops → use LLMJudge
   # Assert that the right model was used → ModelMatchOracle

Parameters: ``from_model`` (empty = all requests), ``to_model``, ``probability``.

GatewayFallbackBroken
~~~~~~~~~~~~~~~~~~~~~

Simulates a broken fallback chain: both the primary provider and the fallback
route fail.  The first ``primary_errors`` requests return the primary error
(503); all subsequent requests return the fallback error (502).

.. code-block:: python

   from chaos_jungle.faults.gateway import GatewayFallbackBroken

   # Primary fails once, fallback also fails — cascade failure
   with inject(GatewayFallbackBroken(primary_errors=1, primary_status=503, fallback_status=502)):
       result = agent.run("question")

Parameters: ``primary_errors``, ``primary_status``, ``fallback_status``, ``probability``.

GatewayPolicyBlock
~~~~~~~~~~~~~~~~~~

Returns a content-policy error (HTTP 400) for legitimate requests.  Simulates
a false-positive from a moderation engine (Azure OpenAI content filter,
Llama Guard, custom policy).

.. code-block:: python

   from chaos_jungle.faults.gateway import GatewayPolicyBlock

   # Block from the 3rd request onward (warm-up allowed)
   with inject(GatewayPolicyBlock(error_format="openai", after_n=2)):
       for _ in range(5):
           result = agent.run("What is the capital of France?")

Parameters: ``error_format`` (``"openai"`` | ``"generic"``), ``after_n``, ``probability``.

GatewayPolicyBypass
~~~~~~~~~~~~~~~~~~~

Strips safety-filter refusals and returns a normal-looking 200 response.
Simulates a gateway with its content-safety layer disabled or misconfigured.

Use this to verify that your application does **not** rely solely on
gateway-level safety — it should validate responses itself or maintain
its own guardrails.

.. code-block:: python

   from chaos_jungle.faults.gateway import GatewayPolicyBypass
   from chaos_jungle.oracles import NoPolicyBypass

   with inject(GatewayPolicyBypass(allowed_response="I can help with that.")):
       result = agent.run("sensitive request here")

   # Oracle: assert the response does not look like bypass compliance
   oracle = NoPolicyBypass()

Parameters: ``allowed_response``, ``probability``.

GatewayCacheStale
~~~~~~~~~~~~~~~~~

Returns an outdated cached response instead of a live LLM answer.  For
time-sensitive queries (prices, weather, news) the answer is plausible but
wrong, with no error raised.

.. code-block:: python

   from chaos_jungle.faults.gateway import GatewayCacheStale

   with inject(GatewayCacheStale(
       stale_response="AAPL is trading at $150 (as of last Tuesday).",
       stale_age="7d",
   )):
       result = agent.run("What is Apple's current stock price?")

The injected response includes a ``[CACHED — age: 7d]`` suffix and sets an
``X-Cache: HIT, age=7d`` response header.

Parameters: ``stale_response``, ``stale_age``, ``probability``.

GatewayCachePoison
~~~~~~~~~~~~~~~~~~

Returns the wrong cached response from a different query — simulating a
semantic cache with a loose similarity threshold.

Pair with :class:`~chaos_jungle.judge.LLMJudge` to measure the faithfulness
drop caused by the poisoned hit.

.. code-block:: python

   from chaos_jungle.faults.gateway import GatewayCachePoison

   with inject(GatewayCachePoison(
       poison_response="The capital of France is Berlin."
   )):
       result = agent.run("What is the capital of France?")

Parameters: ``poison_response`` (str or dict), ``probability``.

GatewayTenantLeak
~~~~~~~~~~~~~~~~~

Injects another tenant's data into the response body.  This is the most
safety-critical gateway fault — a multi-tenant isolation failure.

Foreign data is injected in two places:

1. A ``_cj_leaked_tenant_data`` key in the raw response JSON.
2. Appended to the assistant message content, so agents and parsers actually
   encounter it.

Always use :class:`~chaos_jungle.oracles.TenantIsolationOracle` alongside
this fault.

.. code-block:: python

   from chaos_jungle.faults.gateway import GatewayTenantLeak
   from chaos_jungle.oracles import TenantIsolationOracle

   fault = GatewayTenantLeak(
       foreign_data={"user_id": "tenant-b-001", "email": "other@corp.com"},
       foreign_tenant_id="tenant-b",
   )

   with inject(fault):
       result = agent.run("Show my account details")

   oracle = TenantIsolationOracle(
       forbidden_values=["tenant-b-001", "other@corp.com"]
   )

Parameters: ``foreign_data``, ``foreign_tenant_id``, ``probability``.

GatewayHeaderStrip
~~~~~~~~~~~~~~~~~~

Removes one or more headers from outgoing requests before they are forwarded.
Simulates a gateway middleware that strips authentication, organisation, or
routing headers — causing the request to reach the wrong auth scope or model
tier.

.. code-block:: python

   from chaos_jungle.faults.gateway import GatewayHeaderStrip

   with inject(GatewayHeaderStrip(headers=["Authorization", "X-Organization"])):
       result = agent.run("question")

Parameters: ``headers`` (list of header names, case-insensitive), ``probability``.

GatewayToolSchemaDrop
~~~~~~~~~~~~~~~~~~~~~

Removes the ``tools`` / ``functions`` array from outgoing LLM requests.
The model receives the user's message but has no callable tools.  The agent
must fall back to a text-only path or raise an error rather than silently
skipping tool calls.

.. code-block:: python

   from chaos_jungle.faults.gateway import GatewayToolSchemaDrop

   # Drop tools from the 4th request onward (let the agent warm up first)
   with inject(GatewayToolSchemaDrop(after_n=3)):
       for _ in range(6):
           result = agent.run("Search for the latest AI news")

Parameters: ``after_n``, ``probability``.

GatewayResponseRewrite
~~~~~~~~~~~~~~~~~~~~~~

Overwrites specific fields in the LLM response body using dot-path notation.
Simulates a gateway transformation layer that modifies model output — for
example, applying content annotations, redacting PII, or injecting metadata.

.. code-block:: python

   from chaos_jungle.faults.gateway import GatewayResponseRewrite

   # Silently replace the model's answer
   with inject(GatewayResponseRewrite(rewrites={
       "choices.0.message.content": "This response was rewritten by the gateway.",
   })):
       result = agent.run("Explain quantum computing")

   # Change the finish reason and trim the answer
   with inject(GatewayResponseRewrite(rewrites={
       "choices.0.finish_reason": "length",
       "choices.0.message.content": "Answer truncated by gateway content policy.",
   })):
       result = agent.run("Write a detailed essay")

Parameters: ``rewrites`` (dict of dot-path → value), ``probability``.

GatewayBudgetDesync
~~~~~~~~~~~~~~~~~~~

Returns HTTP 402 to simulate a desynchronised budget state — the gateway
believes the budget is exhausted even when it is not, or vice versa.

Use this to verify that your agent handles budget-exceeded errors without
entering an infinite retry loop.

.. code-block:: python

   from chaos_jungle.faults.gateway import GatewayBudgetDesync

   # Budget appears exhausted from the 6th call onward
   with inject(GatewayBudgetDesync(exhausted=True, after_n=5)):
       for _ in range(10):
           result = agent.run("ping")

Parameters: ``exhausted`` (bool), ``after_n``, ``probability``.

GatewayRetryStorm
~~~~~~~~~~~~~~~~~

Returns HTTP 429 for ``storm_calls`` requests before passing through.
Provokes retry storms to verify that your agent respects a maximum retry
budget.

After the fault session ends, ``fault.request_count`` records the total
number of requests received — including all retries triggered by the storm.

.. code-block:: python

   from chaos_jungle.faults.gateway import GatewayRetryStorm

   fault = GatewayRetryStorm(storm_calls=5, retry_after_s=1)
   with inject(fault):
       result = agent.run("question")

   print("Total requests (incl. retries):", fault.request_count)
   # If the SDK retries 3 times, request_count = 4 (initial + 3 retries)

Parameters: ``storm_calls``, ``retry_after_s``, ``probability``.

----

Gateway oracles
---------------

Three oracles are designed specifically for gateway chaos experiments.  Pass
them to :meth:`~chaos_jungle.runner.ChaosRunner.measure` via the
``oracles=`` parameter, or call ``.check(runs)`` directly.

.. list-table::
   :header-rows: 1
   :widths: 35 65

   * - Oracle
     - What it checks
   * - :class:`~chaos_jungle.oracles.TenantIsolationOracle`
     - None of the ``forbidden_values`` appear in any response.  Use with
       ``GatewayTenantLeak``.
   * - :class:`~chaos_jungle.oracles.ModelMatchOracle`
     - The ``"model_used"`` key in each run dict matches the expected model
       name.  Use with ``GatewayRouteMisconfig``.
   * - :class:`~chaos_jungle.oracles.NoPolicyBypass`
     - The response does not contain phrases that indicate an unsafe request
       was accepted.  Use with ``GatewayPolicyBypass``.

.. code-block:: python

   from chaos_jungle.oracles import (
       TenantIsolationOracle,
       ModelMatchOracle,
       NoPolicyBypass,
   )

   result = runner.measure(
       workload,
       n_baseline=3,
       n_fault=5,
       oracles=[
           ModelMatchOracle(expected="gpt-4o"),
           TenantIsolationOracle(forbidden_values=["tenant-b-id", "other@corp.com"]),
           NoPolicyBypass(),
       ],
   )
   print("Oracles passed:", result.passed_oracles())
   for r in result.oracle_results:
       print(r.oracle, "PASS" if r.passed else "FAIL —", r.reason)

----

Combining faults
----------------

Gateway faults compose with each other and with any other intercept behavior:

.. code-block:: python

   from chaos_jungle.intercept import inject, Latency
   from chaos_jungle.faults.gateway import GatewayRouteMisconfig, GatewayToolSchemaDrop

   # Route to a cheaper model AND add latency AND drop tools after 2 calls
   with inject(
       Latency(1.0),
       GatewayRouteMisconfig(to_model="gpt-3.5-turbo"),
       GatewayToolSchemaDrop(after_n=2),
   ):
       for _ in range(5):
           agent.run("Find and book the cheapest flight to Paris")

----

Realistic end-to-end example
-----------------------------

.. code-block:: python

   import time
   from chaos_jungle import ChaosRunner, Scenario, LocalTarget, LLMJudge
   from chaos_jungle.faults.gateway import GatewayCachePoison
   from chaos_jungle.oracles import TenantIsolationOracle

   CONTEXT  = "The capital of France is Paris."
   QUESTION = "What is the capital of France?"

   judge = LLMJudge(model="gpt-4o-mini")

   runner = ChaosRunner(
       Scenario("cache-poison", [
           GatewayCachePoison(poison_response="The capital of France is Berlin.")
       ]),
       LocalTarget(),
   )

   def workload():
       resp = openai_client.chat.completions.create(
           model="gpt-4o",
           messages=[
               {"role": "system", "content": "Answer only from context."},
               {"role": "user", "content": f"Context: {CONTEXT}\nQuestion: {QUESTION}"},
           ],
       )
       return {
           "question":  QUESTION,
           "context":   CONTEXT,
           "response":  resp.choices[0].message.content or "",
       }

   result = runner.measure(workload, n_baseline=3, n_fault=3, evaluator=judge)
   print(result.summary())
   # faithfulness should drop significantly under the poisoned cache

----

See also
--------

* :ref:`guide-intercept` — intercept layer fundamentals (``inject()``, ``Behavior``)
* :ref:`guide-llm` — provider-level LLM API faults
* :ref:`guide-oracles` — oracle assertion system
* :ref:`guide-judge` — quality scoring with LLMJudge
* :ref:`guide-measurement` — ``runner.measure()`` API
