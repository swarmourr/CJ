.. _guide-intercept:

SDK Intercept
=============

The intercept layer injects faults **at the HTTP transport level** — it patches
``httpx`` and ``requests`` directly so every LLM SDK is affected without any
per-SDK integration code.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - SDK / Framework
     - Works automatically
   * - OpenAI Python SDK
     - Yes — uses httpx
   * - Anthropic SDK
     - Yes — uses httpx
   * - LiteLLM
     - Yes — uses httpx / requests
   * - LangChain / LangGraph
     - Yes — wraps OpenAI / Anthropic
   * - LlamaIndex
     - Yes — wraps OpenAI / Anthropic
   * - Any httpx or requests client
     - Yes

----

Quick start
-----------

.. code-block:: python

   from chaos_jungle.intercept import inject, Latency, RateLimit, Unavailable

   with inject(Latency(3.0)):
       # ALL of these are affected — no SDK-specific code
       openai_client.chat.completions.create(...)
       anthropic_client.messages.create(...)
       litellm.completion(model="gpt-4", messages=[...])
       ChatOpenAI().invoke("hello")   # LangChain

----

Available behaviors
-------------------

.. list-table::
   :header-rows: 1
   :widths: 25 35 40

   * - Behavior
     - Parameters
     - Effect
   * - ``Latency(seconds)``
     - ``seconds: float``
     - Sleep N seconds before every matching request
   * - ``Jitter(min_s, max_s)``
     - ``min_s, max_s: float``
     - Sleep a random duration between min and max
   * - ``RateLimit(after_n, retry_after_s)``
     - ``after_n=0, retry_after_s=60``
     - Return HTTP 429 after the first N requests succeed
   * - ``Unavailable()``
     - —
     - Return HTTP 503 for every matching request
   * - ``Timeout()``
     - —
     - Raise ``httpx.TimeoutException`` / ``requests.exceptions.Timeout``
   * - ``CorruptResponse()``
     - —
     - Return HTTP 200 with a garbled JSON body

----

URL filtering
-------------

By default all requests to common LLM API hostnames are intercepted
(``api.openai.com``, ``api.anthropic.com``, ``localhost``, ``127.0.0.1``, etc.).

Restrict interception to a single provider:

.. code-block:: python

   from chaos_jungle.intercept import inject, Unavailable

   # Only OpenAI calls fail — Anthropic / local models are unaffected
   with inject(Unavailable(), urls=["api.openai.com"]):
       run_pipeline()

Override the full list:

.. code-block:: python

   from chaos_jungle.intercept import inject, Latency, DEFAULT_LLM_HOSTS

   # Add a custom internal gateway to the default list
   with inject(Latency(2.0), urls=DEFAULT_LLM_HOSTS + ["llm-gateway.internal"]):
       run_pipeline()

----

Stacking faults
---------------

Pass multiple behaviors to combine effects:

.. code-block:: python

   from chaos_jungle.intercept import inject, Latency, RateLimit

   with inject(Latency(1.0), RateLimit(after_n=3)):
       # First 3 calls: slow (1 s latency)
       # Calls 4 onward: slow AND rate-limited (429)
       for _ in range(6):
           client.chat.completions.create(...)

Nest ``inject()`` blocks for layered scenarios:

.. code-block:: python

   with inject(Latency(0.5)):          # outer: all calls get 0.5 s delay
       setup_pipeline()
       with inject(RateLimit(after_n=2)):  # inner: adds rate limit on top
           run_experiment()

----

Async support
-------------

``inject()`` works transparently around ``await`` calls because asyncio runs
on a single thread and the patches are applied before the coroutine starts:

.. code-block:: python

   from chaos_jungle.intercept import inject, Latency

   async def test_async_agent():
       with inject(Latency(2.0)):
           response = await async_openai_client.chat.completions.create(
               model="gpt-4o",
               messages=[{"role": "user", "content": "hello"}],
           )
           assert response.choices[0].message.content

----

pytest integration
------------------

Install chaos-jungle and the ``@pytest.mark.chaos`` marker is available
automatically — no ``conftest.py`` changes needed.

.. code-block:: python

   import pytest

   @pytest.mark.chaos(Latency(3.0))
   def test_agent_handles_slow_llm(agent):
       result = agent.run("What is 2+2?")
       assert result is not None

   # Multiple faults
   @pytest.mark.chaos(Latency(1.0), RateLimit(after_n=3))
   def test_agent_degrades_gracefully(agent):
       results = [agent.run("ping") for _ in range(6)]
       assert any(r is not None for r in results)

   # Scope to one provider
   @pytest.mark.chaos(Unavailable(), urls=["api.openai.com"])
   def test_fallback_to_backup_provider(agent):
       result = agent.run("hello")   # OpenAI down; Anthropic fallback used
       assert result is not None

   # Async
   @pytest.mark.chaos(Latency(2.0))
   async def test_async_agent(async_agent):
       result = await async_agent.run("hello")
       assert result is not None

The plugin automatically records each test's outcome and fault configuration
to the chaos-jungle session database.  Results appear in the dashboard and
CLI:

.. code-block:: bash

   chaos-jungle list        # see test sessions
   chaos-jungle dashboard   # browse in the web UI

----

Custom behavior
---------------

Subclass :class:`~chaos_jungle.intercept.Behavior` for full control:

.. code-block:: python

   from chaos_jungle.intercept import Behavior, inject

   class SlowThenFail(Behavior):
       """Add latency on the first call, return 503 on all subsequent ones."""

       def __init__(self):
           self._first = True

       def before(self, url):
           if self._first:
               import time
               time.sleep(2.0)
               self._first = False

       def after(self, url, response):
           if not self._first:
               from chaos_jungle.intercept import _mock_response
               return _mock_response(503, {"error": "overloaded"}, response)
           return response

   with inject(SlowThenFail()):
       run_pipeline()

----

Intercept vs proxy faults
--------------------------

.. list-table::
   :header-rows: 1
   :widths: 30 35 35

   * - Feature
     - Proxy faults (``LLMLatency`` etc.)
     - Intercept (``inject()``)
   * - Works on
     - Linux + macOS
     - Any OS
   * - Requires port setup
     - Yes (``port=``, ``upstream=``)
     - No
   * - Affects all SDKs
     - Only those pointing to proxy
     - Yes — all httpx / requests
   * - Streaming faults
     - Yes
     - Partial (response-level only)
   * - Network-level realism
     - High (real TCP proxy)
     - Medium (Python-level)
   * - Best for
     - Infrastructure chaos
     - Unit / integration tests

----

See also
--------

* :ref:`guide-llm` — proxy-based LLM API faults (``LLMLatency``, ``LLMRateLimit``, …)
* :ref:`guide-measurement` — ``runner.measure()`` and quality gates
* :ref:`guide-judge` — LLM quality scoring
