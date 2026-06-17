.. _guide-llm:

LLM API Faults
==============

Modern AI applications embed LLM calls deep inside agent loops, tool chains,
and multi-step workflows.  When the model API is slow, throttled, or down the
entire application can stall, retry infinitely, or produce silent wrong
answers.  MCP servers add another layer: a failing tool can corrupt the whole
agent task.

chaos-jungle intercepts **all HTTP traffic** between your agent and its
backend — LLM API calls, tool results, and MCP server calls — without
modifying any agent code.

How it works
------------

Every LLM/MCP fault shares the same proxy-based mechanism:

.. mermaid::

   flowchart TD
       subgraph MACHINE["Your machine"]
           AGENT["Agent"]
           PROXY_LLM["LLM Proxy :18000\nfault injected here"]
           PROXY_MCP["MCP Proxy :18100\nfault injected here"]
       end
       LLM_API["LLM API"]
       MCP_SRV["MCP Server"]

       AGENT -->|"chat/completions"| PROXY_LLM
       PROXY_LLM --> LLM_API
       AGENT -->|"tools/call"| PROXY_MCP
       PROXY_MCP --> MCP_SRV

1. ``fault.start()`` spawns the bundled proxy as a background subprocess.
2. The proxy listens on ``localhost:<port>`` and forwards every request to
   the real endpoint while injecting the chosen fault.
3. The environment variable (``OPENAI_BASE_URL``, ``MCP_SERVER_URL``, or
   any variable you name) is set so the client routes through the proxy.
4. ``fault.stop()`` kills the proxy and restores the original env var.

No agent code needs to change.

Quick start
-----------

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner
   from chaos_jungle.faults.llm import LLMLatency
   from chaos_jungle.targets import LocalTarget

   runner = ChaosRunner(
       Scenario("slow-llm", [LLMLatency(delay_s=3.0)]),
       LocalTarget(),
   )
   runner.start()
   response = agent.run("Summarise this document")
   runner.stop()

Or with the ``@chaos_measure`` decorator:

.. code-block:: python

   from chaos_jungle.decorators import chaos_measure
   from chaos_jungle.faults.llm import LLMRateLimit

   @chaos_measure(LLMRateLimit(n=3), scenario_name="rate-limit-test")
   def run_agent_task():
       for question in questions:
           agent.ask(question)
       return {"answered": len(questions)}

   summary = run_agent_task()

LLM API faults
--------------

LLMLatency
~~~~~~~~~~

Adds artificial delay before forwarding every API call.

.. code-block:: python

   from chaos_jungle.faults.llm import LLMLatency

   fault = LLMLatency(delay_s=3.0)

**Tests:** timeout budgets, retry compounding, user-visible progress.

**Default metrics:** ``duration_s``, ``p50_latency_ms``, ``p99_latency_ms``

LLMRateLimit
~~~~~~~~~~~~

Allows the first *n* requests through, then returns HTTP 429.

.. code-block:: python

   from chaos_jungle.faults.llm import LLMRateLimit

   fault = LLMRateLimit(n=5)

**Tests:** back-off logic, ``Retry-After`` handling, request queuing.

**Default metrics:** ``error_rate``, ``http_429_count``, ``duration_s``

LLMBudgetExceeded
~~~~~~~~~~~~~~~~~

Tracks per-request cost in USD via the LLM proxy.  Once the cumulative cost
reaches ``max_cost_usd``, every subsequent request is rejected with HTTP 402
Payment Required.  Per-model pricing is configured via the built-in
``MODEL_PRICING`` table (OpenAI, Anthropic, Google, Ollama) or overridden
with explicit ``input_price_per_1k`` / ``output_price_per_1k`` parameters.

.. code-block:: python

   from chaos_jungle.faults.llm import LLMBudgetExceeded

   fault = LLMBudgetExceeded(max_cost_usd=0.10)

   # Explicit pricing override (useful for custom / private models)
   fault = LLMBudgetExceeded(
       max_cost_usd=0.05,
       input_price_per_1k=0.002,
       output_price_per_1k=0.006,
   )

**Tests:** cost-cap enforcement, graceful degradation when budget is exhausted,
whether the application surfaces a meaningful error to the user.

**Default metrics:** ``http_402_count``, ``tokens_used``, ``cost_usd``, ``error_rate``, ``completion_rate``

LLMTimeout
~~~~~~~~~~

Hangs every connection for *timeout_s* seconds then returns HTTP 504.
No request is forwarded.

.. code-block:: python

   from chaos_jungle.faults.llm import LLMTimeout

   fault = LLMTimeout(timeout_s=10.0)

**Tests:** client-side timeouts, task cancellation, process blocking.

**Default metrics:** ``timeout_rate``, ``duration_s``, ``error_rate``

LLMResponseCorrupt
~~~~~~~~~~~~~~~~~~

Forwards the real API call but mangles the response.

.. code-block:: python

   from chaos_jungle.faults.llm import LLMResponseCorrupt

   fault = LLMResponseCorrupt(mode="truncate")    # partial JSON
   fault = LLMResponseCorrupt(mode="empty")       # {}
   fault = LLMResponseCorrupt(mode="invalid_json") # non-JSON string

**Tests:** ``JSONDecodeError`` handling, downstream data propagation.

**Default metrics:** ``parse_errors``, ``response_length``, ``error_rate``

LLMUnavailable
~~~~~~~~~~~~~~

Always returns HTTP 503.

.. code-block:: python

   from chaos_jungle.faults.llm import LLMUnavailable

   fault = LLMUnavailable()

**Tests:** fallback models, fail-fast behaviour, user error messages.

**Default metrics:** ``error_rate``, ``http_503_count``, ``downtime_s``, ``completion_rate``

Tool call faults
----------------

ToolFault
~~~~~~~~~

Intercepts requests that contain a ``role: tool`` message — i.e. when the
agent is returning a tool execution result to the model — and injects an
API error instead of forwarding it.

.. code-block:: python

   from chaos_jungle.faults.llm import ToolFault

   # Fail every tool call
   fault = ToolFault()

   # Fail only the "search" tool
   fault = ToolFault(tool_name="search")

**Tests:** tool-failure recovery, error propagation through agent loops,
whether the agent retries, aborts, or continues with a bad state.

**Default metrics:** ``error_rate``, ``duration_s``, ``http_status``

LLMHallucination
~~~~~~~~~~~~~~~~

Forwards the real API call and replaces ``choices[0].message.content``
with injected wrong text before returning it to the agent.

.. code-block:: python

   from chaos_jungle.faults.llm import LLMHallucination

   fault = LLMHallucination("The capital of France is Berlin.")
   fault = LLMHallucination("I cannot answer that question.")

**Tests:** downstream validation, fact-checking layers, whether wrong
model output propagates silently through the agent pipeline.

**Default metrics:** ``response_length``, ``error_rate``

LLMStreamInterrupt
~~~~~~~~~~~~~~~~~~

When the agent sends a request with ``"stream": true``, the proxy
pipes SSE events back and then abruptly closes the connection after
*interrupt_after* data events.  Non-streaming requests are unaffected.

.. code-block:: python

   from chaos_jungle.faults.llm import LLMStreamInterrupt

   fault = LLMStreamInterrupt(interrupt_after=3)

**Tests:** partial-response handling, streaming error recovery,
incomplete tool-call detection when the model is cut off mid-generation.

**Default metrics:** ``response_length``, ``error_rate``, ``duration_s``

LLMTokenStarvation
~~~~~~~~~~~~~~~~~~

Rewrites every request to set ``max_tokens`` to a tiny value before
forwarding.  The model returns a real but truncated response with
``finish_reason: "length"``.

.. code-block:: python

   from chaos_jungle.faults.llm import LLMTokenStarvation

   fault = LLMTokenStarvation(max_tokens=5)

**Tests:** truncated answer handling, context-window pressure simulation,
agents that loop when a response is incomplete.

**Default metrics:** ``tokens_used``, ``truncation_rate``, ``response_length``, ``completion_rate``

MCP faults
----------

`Model Context Protocol (MCP) <https://modelcontextprotocol.io>`_ servers
expose tools and resources to LLM agents over HTTP using JSON-RPC.
``MCPFault`` proxies MCP traffic the same way LLM faults proxy API calls.

.. code-block:: python

   from chaos_jungle.faults.llm import MCPFault

   # Return a JSON-RPC error for every tool/resource call
   fault = MCPFault(mode="tool_error")

   # Make the MCP server completely unreachable
   fault = MCPFault(mode="unavailable")

   # Hang every call for 10 s
   fault = MCPFault(mode="timeout", timeout_s=10.0)

Point ``upstream`` at your MCP server and ``base_url_env`` at the variable
your agent reads for the MCP server URL:

.. code-block:: python

   fault = MCPFault(
       mode="tool_error",
       upstream="http://localhost:3000",   # your MCP server
       base_url_env="MCP_SERVER_URL",      # env var your agent reads
       port=18100,
   )

**Tests:** tool execution failures inside agent loops, JSON-RPC error
handling, graceful degradation when a tool server goes down.

**Default metrics:** ``error_rate``, ``downtime_s``, ``duration_s``, ``http_status``

Intercepting MCP between agents
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When multiple agents communicate via MCP (agent A calls agent B's tools),
place the proxy between them by overriding the URL agent A uses to reach
agent B:

.. code-block:: python

   import os

   # Agent A reads AGENT_B_URL to find agent B's MCP server
   fault = MCPFault(
       mode="timeout",
       timeout_s=5.0,
       upstream="http://agent-b:3000",
       base_url_env="AGENT_B_URL",
       port=18200,
   )
   runner.start()
   # Agent A now routes through the proxy → fault injected
   agent_a.run_task()
   runner.stop()

Combining faults
----------------

Run multiple scenarios back-to-back with :class:`~chaos_jungle.suite.ExperimentSuite`:

.. code-block:: python

   from chaos_jungle import ExperimentSuite
   from chaos_jungle.faults.llm import (
       LLMLatency, LLMRateLimit, LLMUnavailable,
       ToolFault, MCPFault,
   )
   from chaos_jungle.targets import LocalTarget

   suite = ExperimentSuite(target=LocalTarget())
   suite.add("slow-api",       faults=[LLMLatency(delay_s=4.0)])
   suite.add("throttled",      faults=[LLMRateLimit(n=2)])
   suite.add("complete-outage",faults=[LLMUnavailable()])
   suite.add("tool-down",      faults=[ToolFault()])
   suite.add("mcp-down",       faults=[MCPFault(mode="unavailable")])

   for result in suite.run(workload=run_agent_task):
       print(result["scenario"], result["duration_s"])

Configuration reference
-----------------------

Upstream endpoint
~~~~~~~~~~~~~~~~~

.. code-block:: python

   LLMLatency(upstream="https://api.openai.com")      # default
   LLMLatency(upstream="https://api.anthropic.com")
   LLMLatency(upstream="http://localhost:11434")       # Ollama

Environment variable
~~~~~~~~~~~~~~~~~~~~

.. code-block:: python

   LLMLatency(base_url_env="OPENAI_BASE_URL")         # default (openai SDK)
   LLMLatency(base_url_env="ANTHROPIC_BASE_URL")      # anthropic SDK
   MCPFault(base_url_env="MCP_SERVER_URL")            # MCP clients

Port assignment
~~~~~~~~~~~~~~~

Each fault occupies one port.  Assign unique ports when running multiple
faults simultaneously:

.. code-block:: python

   LLMLatency(port=18000)       # default
   LLMRateLimit(port=18001)
   ToolFault(port=18002)
   MCPFault(port=18100)         # MCP default

Fault reference
---------------

.. list-table::
   :header-rows: 1
   :widths: 25 25 50

   * - Fault
     - Agent failure mode
     - What you are testing
   * - ``LLMLatency``
     - Slow model response
     - Timeout budget, retry strategy
   * - ``LLMRateLimit``
     - Throttled API (429)
     - Back-off, request queuing
   * - ``LLMBudgetExceeded``
     - Budget cap hit (402)
     - Cost-cap enforcement, graceful degradation
   * - ``LLMTimeout``
     - Hanging connection
     - Task cancellation, hang detection
   * - ``LLMResponseCorrupt``
     - Malformed JSON response
     - Parse-error handling, retry
   * - ``LLMUnavailable``
     - Complete API outage
     - Fallback model, graceful degradation
   * - ``ToolFault``
     - Tool execution error
     - Tool-failure recovery in agent loops
   * - ``LLMHallucination``
     - Wrong model answer
     - Downstream validation, fact-checking
   * - ``LLMStreamInterrupt``
     - Truncated stream
     - Partial-response handling
   * - ``LLMTokenStarvation``
     - Response cut off at length limit
     - Incomplete-answer handling
   * - ``MCPFault``
     - MCP server failure
     - Tool server resilience, agent-to-agent calls

Measurement reference
---------------------

What each fault injects, what metric to capture, and what a passing result
looks like.

.. list-table::
   :header-rows: 1
   :widths: 20 25 25 30

   * - Fault
     - What is injected
     - What to measure
     - Expected output
   * - ``LLMLatency``
     - N-second sleep before forwarding
     - ``elapsed_s`` delta vs baseline
     - ``elapsed_s >= baseline + delay_s``
   * - ``LLMRateLimit``
     - HTTP 429 after the first *n* requests
     - Count of blocked calls, index of first failure
     - Calls ``[0..n-1]`` succeed; ``[n..]`` return 429
   * - ``LLMBudgetExceeded``
     - HTTP 402 once cumulative cost ≥ ``max_cost_usd``
     - Cost accumulated, HTTP 402 count, tokens consumed
     - Requests after budget hit return 402; prior calls succeed
   * - ``LLMTimeout``
     - Connection held for *T* s then HTTP 504
     - ``elapsed_s``, error type received by agent
     - Agent raises timeout exception before *T* expires
   * - ``LLMUnavailable``
     - HTTP 503 on every call
     - Error type, retry count by agent
     - All calls return 503; agent degrades gracefully
   * - ``LLMResponseCorrupt`` ``truncate``
     - Response body cut in half
     - JSON parse error raised
     - Agent raises ``JSONDecodeError`` or receives partial text
   * - ``LLMResponseCorrupt`` ``empty``
     - Response replaced with ``{}``
     - JSON parse error or empty content field
     - Agent raises ``KeyError`` or receives empty reply
   * - ``LLMResponseCorrupt`` ``invalid_json``
     - Response replaced with a raw string
     - JSON parse error raised
     - Agent raises ``JSONDecodeError``
   * - ``LLMHallucination`` (static)
     - Hardcoded wrong text replaces answer
     - Plausibility score, detection rate, propagation
     - Injected text appears in reply verbatim
   * - ``LLMHallucination`` (generated)
     - Second LLM generates a plausible wrong answer
     - Plausibility score, similarity to real answer, detection rate, propagation to follow-up
     - Reply is wrong but convincing; undetected by naive fact-check
   * - ``LLMStreamInterrupt``
     - SSE stream closed after *n* chunks
     - Chunks received, completeness of reply
     - Reply is truncated; agent handles partial response
   * - ``LLMTokenStarvation``
     - ``max_tokens`` forced to *n* before forwarding
     - Word count of reply vs baseline, ``finish_reason``
     - Reply is very short; ``finish_reason = "length"``
   * - ``ToolFault``
     - HTTP 400 error when tool result is submitted
     - Agent retry count, fallback behaviour
     - Agent receives tool error; handles or raises
   * - ``MCPFault`` ``tool_error``
     - JSON-RPC error on every MCP call
     - Error code received, agent fallback
     - Agent gets ``-32000``; handles gracefully
   * - ``MCPFault`` ``unavailable``
     - HTTP 503 on every MCP call
     - Error type, agent fallback
     - Agent sees service down
   * - ``MCPFault`` ``timeout``
     - MCP connection held *T* s then HTTP 504
     - ``elapsed_s``, agent timeout behaviour
     - Agent times out before *T*

Hallucination outputs
~~~~~~~~~~~~~~~~~~~~~

For ``LLMHallucination`` the infrastructure metrics (status code, elapsed)
are not enough.  Capture these semantic metrics:

.. code-block:: python

   {
       "prompt":              "What is the capital of France?",
       "real_answer":         "Paris",
       "hallucinated_answer": "Lyon is the historical capital of France.",
       "plausibility":        0.82,   # 0-1 judged by a second LLM call
       "detected_by_agent":   False,  # did any downstream check flag it?
       "propagated":          True,   # did the error affect a follow-up answer?
       "follow_up_answer":    "France uses the Franc, centered in Lyon.",
       "ground_truth":        "Paris",
       "similarity_to_real":  0.12,   # low = very different from real answer
   }

``plausibility`` and ``detected_by_agent`` require a second LLM call acting
as a judge.  ``propagated`` requires feeding the hallucinated answer back as
context and checking whether the downstream answer still matches ground truth.
