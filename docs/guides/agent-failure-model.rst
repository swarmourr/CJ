.. _guide-agent-failure-model:

LLM Agent Failure Model
=======================

LLM-powered agents fail at every layer of the stack.
This page maps the full failure surface and links each layer to the Chaos
Jungle faults that cover it.

Most agent bugs surface silently — the agent returns something, but it is
wrong.  The system does not crash; it hallucates, loops, or silently reasons
from stale data.  This is why chaos engineering for agents is different from
traditional service testing: the failure mode is often **semantic**, not
structural.

----

Layer 1 — LLM API
------------------

The model itself is the first point of failure.

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Failure mode
     - What happens
   * - Latency spike
     - Agent waits, downstream tools time out, user sees a hang
   * - Rate limit (429)
     - Agent retries in a loop, budget is consumed, task never completes
   * - Corrupt response
     - Agent parses garbage, produces nonsense downstream
   * - Timeout / 503
     - Agent falls back (if it has one) or crashes silently
   * - Budget exceeded (402)
     - All calls blocked; agent cannot continue

→ See :ref:`guide-llm` and :ref:`guide-intercept`.

----

Layer 2 — AI Gateway
---------------------

Most production deployments route LLM traffic through a gateway (LiteLLM,
Azure API Management, AWS Bedrock, custom proxy).  Gateways add their own
failure modes on top of the model.

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Failure mode
     - What happens
   * - Route misconfiguration
     - Wrong model served, agent behaviour changes silently
   * - Broken fallback
     - Primary fails, fallback also fails, agent crashes
   * - Policy block / bypass
     - Safety filter incorrectly blocks or incorrectly allows
   * - Stale cache
     - Agent reads yesterday's answer for today's query
   * - Tenant leak
     - Agent receives another tenant's cached response
   * - Tool schema drop
     - Gateway strips tool definitions; agent cannot call tools
   * - Budget desync
     - Gateway and agent have different views of spend
   * - Retry storm
     - Gateway retries internally; agent sees slow 200s, not errors

→ See :ref:`guide-gateway`.

----

Layer 3 — Tool / MCP
---------------------

Agents call external tools (web search, code execution, database, MCP
servers).  Tools are the most common source of agent failure in production.

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Failure mode
     - What happens
   * - Tool unavailable
     - Agent cannot complete the task, may hallucinate a result
   * - Bad output
     - Tool returns something syntactically valid but semantically wrong
   * - Version skew
     - Tool schema changed; agent sends wrong parameters
   * - Permission denied
     - Agent cannot read or write the resource it needs
   * - Conflicting results
     - Two tools return contradictory information; agent cannot resolve

→ See :ref:`guide-skill` for skill-file faults (the most common MCP pattern).

----

Layer 4 — RAG / Context
------------------------

Retrieval-augmented agents depend on the quality of the context injected into
the prompt.  Semantic faults corrupt this context without touching the model.

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Failure mode
     - What happens
   * - RAG poison
     - Retrieved documents contain injected false facts
   * - Context truncation
     - Key information is cut off before the model sees it
   * - Entity swap
     - Names, dates, or amounts are silently replaced
   * - Distractor injection
     - Irrelevant paragraphs crowd out the relevant ones
   * - Prompt injection
     - Retrieved content contains adversarial instructions

→ See :ref:`guide-semantic`.

----

Layer 5 — Memory / State
-------------------------

Agents that persist state across turns depend on the integrity of that state.
A corrupted memory makes every subsequent turn wrong.

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Failure mode
     - What happens
   * - Redis key corruption
     - Agent reads wrong cached value, reasons from bad data
   * - JSON checkpoint corruption
     - Deserialization fails or produces wrong plan
   * - Postgres column update
     - Agent reads a mutated record, acts on wrong information
   * - Stale state
     - Value is valid but outdated; agent misses a recent change

→ See :ref:`guide-state`.

----

Layer 6 — Skill Files
----------------------

Skill files (system prompts, instruction sets, persona definitions) are
loaded at startup or per-task.  Corrupted skill files silently change agent
behaviour for every call.

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Failure mode
     - What happens
   * - Instruction corruption
     - Agent follows a mutated persona, breaks task constraints
   * - Version skew
     - Old skill file loaded, new expected capabilities missing
   * - File unavailable
     - Agent starts with no system prompt; reverts to base model behaviour
   * - Bad output format
     - Agent produces output in a format downstream cannot parse

→ See :ref:`guide-skill`.

----

Layer 7 — Planner / Workflow
-----------------------------

Multi-step agents maintain a plan or task graph.  Faults at this layer
derail the plan without any single step throwing an error.

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Failure mode
     - What happens
   * - Partial failure mid-plan
     - One step fails silently; agent continues on a broken state
   * - Infinite loop
     - Agent re-plans indefinitely without making progress
   * - Goal drift
     - Accumulated context shifts the agent off the original objective
   * - Early termination
     - Agent marks task complete before it actually is

Use :ref:`guide-intercept` with ``inject()`` wrapped around individual plan
steps to test mid-plan failures.

----

Layer 8 — Multi-agent Communication
-------------------------------------

Systems with multiple agents (orchestrator + sub-agents, supervisor + worker)
have communication channels between agents that can fail.

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Failure mode
     - What happens
   * - Sub-agent timeout
     - Orchestrator waits indefinitely or receives empty result
   * - Conflicting instructions
     - Two orchestrators give contradictory tasks to the same worker
   * - Lost message
     - A task result is dropped; orchestrator re-assigns or skips
   * - Schema mismatch
     - Sub-agent returns format the orchestrator cannot parse

Use :ref:`guide-llm` faults on the sub-agent's LLM calls, or :ref:`guide-network`
faults on the inter-agent network link.

----

Layer 9 — Infrastructure
--------------------------

The compute, network, and storage that agents run on are subject to all
standard infrastructure failure modes.  These are slower to manifest but
affect every layer above them.

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Failure mode
     - What happens
   * - Network degradation
     - All API calls slower; cascading timeouts across the agent
   * - Disk full
     - Log writes fail, checkpoints cannot be saved, tool outputs lost
   * - CPU saturation
     - Inference slower, timeouts hit before model responds
   * - Process / container kill
     - Agent process dies mid-task, no graceful shutdown

→ See :ref:`guide-network`, :ref:`guide-resources`, :ref:`guide-storage`,
:ref:`guide-process`.

----

Layer 10 — Evaluation
-----------------------

Even when the agent completes the task, the output may be wrong in ways
that only a judge can detect.

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Failure mode
     - What happens
   * - Hallucination
     - Agent states facts not present in context or tools
   * - Faithfulness failure
     - Answer does not follow from the retrieved documents
   * - Guardrail violation
     - Response contains content that should have been blocked
   * - Incoherence
     - Response is internally contradictory

→ See :ref:`guide-judge`.

----

Putting it all together
-----------------------

A single production agent call may traverse all ten layers.
Chaos Jungle lets you inject faults at any layer — independently or
simultaneously — and measure the impact against a clean baseline.

.. code-block:: python

   from chaos_jungle.intercept import inject, Latency, RateLimit
   from chaos_jungle import ChaosRunner, Scenario, NetworkDelay, SSHTarget

   # Layer 1: slow LLM + Layer 9: slow network at the same time
   runner = ChaosRunner(
       Scenario("compound", [NetworkDelay("200ms")]),
       SSHTarget("10.0.0.1", user="ubuntu"),
   )
   runner.start()
   with inject(Latency(3.0), RateLimit(after_n=5)):
       result = agent.run("Summarise this week's incidents")
   runner.stop()

Start with the layer most likely to fail in your system, then work outward.
