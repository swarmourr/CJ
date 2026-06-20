chaos-jungle
============

**Chaos engineering for AI agents and distributed systems.**

chaos-jungle lets you deliberately break things — LLM APIs, agent tools,
network links, storage, services, system resources — so you can measure
exactly what breaks, by how much, and whether your system recovers
gracefully.

It works on your laptop today.  No Kubernetes.  No paid chaos platform.

----

Why chaos-jungle?
-----------------

Modern systems fail in ways that are hard to reproduce:

* The LLM API is slow at 3 AM and your agent silently loops.
* A 2 % packet loss causes your database to time out, but your service
  returns 200 anyway.
* An injected tool result convinces your AI agent to book the wrong flight.
* A Redis key gets corrupted and the agent reasons from stale state.

chaos-jungle injects these failures on demand, measures the impact
side-by-side against a healthy baseline, and tells you whether your system
passed a quality gate — all from a single ``pip install``.

----

Two-minute tour
---------------

**Testing an AI agent?**

.. code-block:: python

   from chaos_jungle.intercept import inject, RateLimit, Latency

   # Zero setup — patches httpx/requests directly.  Works on macOS.
   with inject(Latency(3.0), RateLimit(after_n=5)):
       result = my_agent.run("Book me a flight to Paris")

   # Measure baseline vs fault side-by-side
   from chaos_jungle import ChaosRunner, Scenario, LLMLatency, LocalTarget

   runner = ChaosRunner(
       Scenario("slow-llm", [LLMLatency(delay_s=3.0)]),
       LocalTarget(),
   )
   result = runner.measure(workload, n_baseline=5, n_fault=5)
   print(result.summary())

**Testing infrastructure?**

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, NetworkDelay, SSHTarget

   target = SSHTarget("192.168.1.100", user="ubuntu")
   runner = ChaosRunner(
       Scenario("wan-degraded", [NetworkDelay("200ms", jitter="20ms")]),
       target,
   )
   result = runner.measure(workload, n_baseline=5, n_fault=5)
   print(result.summary())   # fault_mean("duration_s") ≈ baseline + 0.2 s

----

What can you break?
--------------------

**LLM / AI Agent faults** — no Linux, no sudo, works on macOS

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Fault category
     - Examples
   * - API failures (:ref:`guide-llm`)
     - Latency, rate-limit (429), timeout, corrupt response, 503/401/403/402
   * - Zero-setup intercept (:ref:`guide-intercept`)
     - ``inject(Latency(3.0))``, ``inject(RateLimit(after_n=5))``,
       ``inject(PromptInjection(...))``
   * - Semantic faults (:ref:`guide-semantic`)
     - Entity swap, RAG poison, context truncation, distractor injection
   * - Agent state (:ref:`guide-state`)
     - Redis key mutation, JSON checkpoint corruption, Postgres column update
   * - AI gateway faults (:ref:`guide-gateway`)
     - Route misconfig, fallback broken, policy block/bypass, cache stale,
       cache poison, tenant leak, header strip, tool schema drop,
       response rewrite, budget desync, retry storm
   * - Skill / tool faults (:ref:`guide-skill`)
     - Skill unavailable, bad output, version skew, permission denied,
       conflicting results, instruction corruption
   * - Quality scoring (:ref:`guide-judge`)
     - Faithfulness, hallucination, coherence, guardrail violation (LLMJudge)

**Infrastructure faults** — Linux + SSH required

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Fault category
     - Examples
   * - Network (:ref:`guide-network`)
     - Delay, loss, corruption, duplicate, bandwidth cap, reorder, TCP RST,
       partition
   * - Storage (:ref:`guide-storage`)
     - Bit-flip on cron schedule, immediate byte corruption, SQLite page
       corruption
   * - Process / service (:ref:`guide-process`)
     - Process kill, systemd service stop/restart/mask, container kill/pause
   * - Resource exhaustion (:ref:`guide-resources`)
     - CPU saturation, memory pressure, disk full, inode exhaustion, FD
       exhaustion, process exhaustion
   * - GPU (:ref:`guide-gpu`)
     - GPU memory pressure, process kill, driver error injection

----

How it works
------------

Every fault follows the same four-step lifecycle:

.. code-block:: text

   preflight → start → [your workload] → stop → revert

1. **preflight** — check that required tools (tc, stress-ng, iptables, …)
   are available and auto-install missing packages where possible.
2. **start** — inject the fault (add tc rule, launch stress-ng, start proxy,
   corrupt file, …).
3. **[workload]** — your code runs under the fault condition.
4. **stop / revert** — remove all rules and restore any modified files.
   ``revert()`` is always called even if the workload throws an exception.

The :class:`~chaos_jungle.runner.ChaosRunner` orchestrates this lifecycle and
writes a full audit trail — every command, every metric, every event — to a
local SQLite database.

----

Measurement and quality gates
------------------------------

``runner.measure()`` runs your workload N times under baseline conditions and
N times under fault, then computes mean, standard deviation, and delta for
every metric you return:

.. code-block:: python

   def workload():
       t0 = time.time()
       response = call_my_service()
       return {
           "duration_s": round(time.time() - t0, 3),
           "success":    int(response.ok),
       }

   result = runner.measure(workload, n_baseline=5, n_fault=5)
   print(result.summary())
   # duration_s  baseline=0.12s  fault=0.34s  delta=+183%
   # success     baseline=1.00   fault=0.60   delta=-40%

Add an LLM judge to score response quality across the same runs:

.. code-block:: python

   from chaos_jungle import LLMJudge

   judge  = LLMJudge(model="gpt-4o-mini")
   result = runner.measure(workload, n_baseline=5, n_fault=5, evaluator=judge)
   print("Quality gate:", result.passed_quality(min_faithfulness=0.70, max_hallucination=0.30))

----

Installation
------------

.. code-block:: bash

   pip install chaos-jungle

   # or latest from GitHub
   pip install git+https://github.com/swarmourr/CJ.git

Requirements: Python 3.9+.  LLM / AI faults work on **macOS and Linux**.
Infrastructure faults require a **Linux target** with ``sudo``.

----

Where to go next
-----------------

.. list-table::
   :widths: 50 50
   :header-rows: 0

   * - **Testing an AI agent?**
     - **Testing infrastructure?**
   * - Start with :ref:`guide-intercept` (zero setup) or :ref:`guide-llm`
       (proxy faults), then explore :ref:`guide-semantic`,
       :ref:`guide-skill`, and :ref:`guide-judge`.
     - Start with :doc:`quickstart`, then pick a fault guide:
       :ref:`guide-network`, :ref:`guide-storage`, :ref:`guide-process`,
       or :ref:`guide-resources`.
   * - See :doc:`concepts` for the Fault / Target / Scenario / Runner model.
     - See :doc:`architecture` for the system design.

----

.. toctree::
   :maxdepth: 2
   :caption: Getting Started

   quickstart
   architecture
   concepts
   examples

.. toctree::
   :maxdepth: 2
   :caption: Chaos Strategies

   guides/strategies

.. toctree::
   :maxdepth: 2
   :caption: Setup & Targets

   guides/local
   guides/ssh
   guides/http
   guides/separate-mode

.. toctree::
   :maxdepth: 2
   :caption: Infrastructure Faults

   guides/network
   guides/storage
   guides/process
   guides/resources
   guides/gpu

.. toctree::
   :maxdepth: 2
   :caption: LLM / AI Faults

   guides/llm
   guides/intercept
   guides/gateway
   guides/semantic
   guides/state
   guides/skill
   guides/judge
   guides/ollama
   guides/scenarios
   guides/conversation
   guides/fuzzing

.. toctree::
   :maxdepth: 2
   :caption: Measurement & Results

   guides/measurement
   guides/metrics
   guides/dashboard
   guides/data

.. toctree::
   :maxdepth: 2
   :caption: Safety & Assertions

   guides/safety
   guides/oracles
   guides/traces

.. toctree::
   :maxdepth: 2
   :caption: API Reference

   api/faults
   api/judge
   api/targets
   api/scenario
   api/runner
   api/decorators
   api/metrics
   api/guardrails
   api/suite
   api/cli
   api/daemon
   api/dashboard

.. toctree::
   :maxdepth: 1
   :caption: Project

   changelog
