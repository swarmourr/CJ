.. _guide-llm:

LLM Agent Fault Injection
=========================

Modern AI applications embed LLM calls deep inside agent loops, tool chains,
and multi-step workflows.  When the LLM API is slow, throttled, or down the
entire application can stall, retry infinitely, or produce silent wrong
answers.

chaos-jungle provides a set of **LLM fault types** that intercept HTTP traffic
between your agent and the model API without modifying any agent code.

.. contents:: On this page
   :local:
   :depth: 2

How it works
------------

All LLM faults share the same proxy-based mechanism:

.. code-block:: text

   ┌─────────────────────────────────────────────────────────┐
   │  Your machine                                           │
   │                                                         │
   │  [Agent] ──HTTP──▶ [LLM Proxy :18000] ──HTTP──▶ [API]  │
   │                          │                              │
   │                     injects fault                       │
   └─────────────────────────────────────────────────────────┘

1. ``fault.start()`` spawns the bundled proxy as a background subprocess.
2. The proxy listens on ``localhost:<port>`` and forwards requests to the
   real API endpoint while injecting the chosen fault.
3. The environment variable ``OPENAI_BASE_URL`` (or any variable you name)
   is set so that OpenAI / Anthropic / LiteLLM clients automatically route
   through the proxy.
4. ``fault.stop()`` kills the proxy and restores the original environment.

No agent code needs to change — just wrap your agent call.

Quick start
-----------

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner
   from chaos_jungle.faults.llm import LLMLatency
   from chaos_jungle.targets import LocalTarget

   fault = LLMLatency(delay_s=3.0)
   runner = ChaosRunner(Scenario("slow-llm", [fault]), LocalTarget())

   runner.start()
   # --- your agent runs here ---
   response = agent.run("Summarise this document")
   # ----------------------------
   runner.stop()

Or with the decorator:

.. code-block:: python

   from chaos_jungle.decorators import chaos_measure
   from chaos_jungle.faults.llm import LLMRateLimit

   @chaos_measure(LLMRateLimit(n=3), scenario_name="rate-limit-test")
   def run_agent_task():
       for question in questions:
           agent.ask(question)
       return {"answered": len(questions)}

   summary = run_agent_task()
   print(summary["duration_s"], "s — errors:", summary["result"]["errors"])

Available faults
----------------

LLMLatency
~~~~~~~~~~

Adds artificial delay before forwarding every API call.

.. code-block:: python

   from chaos_jungle.faults.llm import LLMLatency

   # 2 second delay on every call (default)
   fault = LLMLatency()

   # 5 second delay
   fault = LLMLatency(delay_s=5.0)

**What to look for**

- Does the agent time out correctly, or wait indefinitely?
- Do retries compound the latency (retry storms)?
- Is the user shown a spinner / progress indicator?

LLMRateLimit
~~~~~~~~~~~~

Allows the first *n* requests through, then returns HTTP 429 for all
subsequent calls.

.. code-block:: python

   from chaos_jungle.faults.llm import LLMRateLimit

   # Allow 5 calls, then rate-limit
   fault = LLMRateLimit(n=5)

**What to look for**

- Does the agent implement exponential back-off?
- Does it respect a ``Retry-After`` header?
- Does it surface a clear error to the user instead of looping?

LLMTimeout
~~~~~~~~~~

Hangs every connection for *timeout_s* seconds, then returns HTTP 504.
No requests are forwarded to the real API.

.. code-block:: python

   from chaos_jungle.faults.llm import LLMTimeout

   # 10-second hang per request
   fault = LLMTimeout(timeout_s=10.0)

**What to look for**

- Does the agent cancel the hung task or block the whole process?
- Is there a user-visible timeout with a friendly message?
- Does the agent resume correctly after the fault is removed?

LLMResponseCorrupt
~~~~~~~~~~~~~~~~~~

Forwards the real API call but mangles the response before the agent
sees it.  Supports three corruption modes:

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Mode
     - What happens
   * - ``"truncate"``
     - Response body cut to half its length (partial / broken JSON)
   * - ``"empty"``
     - Response body replaced with ``{}``
   * - ``"invalid_json"``
     - Response body replaced with a non-JSON string

.. code-block:: python

   from chaos_jungle.faults.llm import LLMResponseCorrupt

   fault = LLMResponseCorrupt(mode="truncate")       # default
   fault = LLMResponseCorrupt(mode="empty")
   fault = LLMResponseCorrupt(mode="invalid_json")

**What to look for**

- Does the agent catch ``json.JSONDecodeError`` / ``ValidationError``?
- Does it retry on parse failure or pass the broken data downstream?
- Does partial content cause silent wrong answers?

LLMUnavailable
~~~~~~~~~~~~~~

Makes the API completely unreachable — every request returns HTTP 503.

.. code-block:: python

   from chaos_jungle.faults.llm import LLMUnavailable

   fault = LLMUnavailable()

**What to look for**

- Does the agent fail fast or retry indefinitely?
- Is there a fallback model or graceful degradation path?
- Does the user see a meaningful error message?

Combining faults
----------------

Run multiple fault scenarios back-to-back with :class:`~chaos_jungle.suite.ExperimentSuite`:

.. code-block:: python

   from chaos_jungle import ExperimentSuite
   from chaos_jungle.faults.llm import LLMLatency, LLMRateLimit, LLMUnavailable
   from chaos_jungle.targets import LocalTarget

   suite = ExperimentSuite(target=LocalTarget())
   suite.add("slow-api",        faults=[LLMLatency(delay_s=4.0)])
   suite.add("throttled",       faults=[LLMRateLimit(n=2)])
   suite.add("complete-outage", faults=[LLMUnavailable()])

   for experiment in suite.run(workload=run_agent_task):
       print(experiment["scenario"], experiment["duration_s"])

Changing the API endpoint
-------------------------

By default the proxy forwards to ``https://api.openai.com``.  Override
``upstream`` to test against Anthropic, Azure OpenAI, a local model, or any
OpenAI-compatible endpoint:

.. code-block:: python

   # Anthropic (Claude)
   LLMLatency(delay_s=2.0, upstream="https://api.anthropic.com")

   # Azure OpenAI
   LLMLatency(delay_s=2.0, upstream="https://my-resource.openai.azure.com")

   # Local Ollama
   LLMLatency(delay_s=2.0, upstream="http://localhost:11434")

Changing the base-URL environment variable
------------------------------------------

The proxy sets ``OPENAI_BASE_URL`` by default.  If your LLM client reads a
different variable, pass ``base_url_env``:

.. code-block:: python

   # Anthropic SDK reads ANTHROPIC_BASE_URL
   LLMLatency(delay_s=2.0, base_url_env="ANTHROPIC_BASE_URL",
              upstream="https://api.anthropic.com")

   # LiteLLM proxy
   LLMUnavailable(base_url_env="LITELLM_PROXY_BASE_URL")

Port conflicts
--------------

Each fault uses port ``18000`` by default.  If you run multiple fault
instances simultaneously (e.g., in a test suite), assign unique ports:

.. code-block:: python

   LLMLatency(port=18001)
   LLMRateLimit(port=18002)
   LLMUnavailable(port=18003)

Failure types reference
-----------------------

The table below maps agent failure modes to the recommended fault type:

.. list-table::
   :header-rows: 1
   :widths: 40 30 30

   * - Agent failure mode
     - Fault to inject
     - What you are testing
   * - Model responds slowly
     - :class:`LLMLatency`
     - Timeout budget, retry strategy
   * - Model is throttled (429)
     - :class:`LLMRateLimit`
     - Back-off, request queuing
   * - Connection hangs forever
     - :class:`LLMTimeout`
     - Task cancellation, hang detection
   * - Response is malformed JSON
     - :class:`LLMResponseCorrupt`
     - Parse-error handling, retry on failure
   * - Model API is completely down
     - :class:`LLMUnavailable`
     - Fallback model, graceful degradation
   * - Intermittent errors
     - :class:`LLMRateLimit` (low n)
     - Error rate tolerance, partial success
