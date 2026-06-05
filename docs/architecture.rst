.. _architecture:

Architecture
============

chaos-jungle is organised into five planes that work together to inject,
observe, and evaluate faults across any layer of a modern system.

.. code-block:: text

   ┌─────────────────────────────────────────────────────────────────┐
   │                        CONTROL PLANE                           │
   │  Scenario ─── ChaosRunner ─── ExperimentSuite                  │
   │  decorators (@chaos, @chaos_measure) · inject() · door()        │
   └────────────────────┬────────────────────────────────────────────┘
                        │
          ┌─────────────┼──────────────┐
          ▼             ▼              ▼
   ┌─────────────┐ ┌──────────┐ ┌──────────────────┐
   │  TRANSPORT  │ │  TARGET  │ │  EVALUATION       │
   │   PLANE     │ │  PLANE   │ │  PLANE            │
   │             │ │          │ │                   │
   │ HTTP proxy  │ │ Local    │ │ LLMJudge          │
   │ httpx patch │ │ SSH      │ │ Metrics           │
   │ OS-level    │ │ HTTP/    │ │ Quality gates     │
   │ (tc / BPF)  │ │ cj-daemon│ │ runner.measure()  │
   └──────┬──────┘ └────┬─────┘ └────────┬──────────┘
          │             │                │
          └─────────────┼────────────────┘
                        ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │                         DATA PLANE                              │
   │  SQLite session DB  ──►  Web dashboard  ──►  CSV export         │
   └─────────────────────────────────────────────────────────────────┘

----

Control Plane
-------------

The control plane is the Python API that developers interact with directly.
It is responsible for assembling faults into scenarios, managing the lifecycle
of an experiment, and recording results.

Key objects:

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Object
     - Role
   * - ``Scenario``
     - A named, ordered list of ``Fault`` objects — pure data, no logic.
   * - ``ChaosRunner``
     - Orchestrates preflight → start → workload → stop → revert, writes every
       action to the session database.
   * - ``ExperimentSuite``
     - Run a batch of scenarios in sequence or in parallel; aggregate results.
   * - ``@chaos`` / ``@chaos_measure``
     - Decorator wrappers around ``ChaosRunner`` for single-function tests.
   * - ``inject()``
     - Lightweight context manager for HTTP-level fault injection without a
       full runner setup.
   * - ``door()``
     - Cycling runner — alternates fault-ON / fault-OFF for N cycles.

Lifecycle of a single experiment:

.. code-block:: text

   1. preflight  — check tools (tc, stress-ng, docker …) are available
   2. start      — inject all faults in scenario order
   3. [workload] — user's code runs under active faults
   4. stop       — remove faults in reverse order
   5. revert     — undo any persistent side-effects (file restores, etc.)
   6. record     — write session, events, results to SQLite

----

Transport Plane
---------------

Faults are injected at three different depths depending on what layer you want
to test.

**1. OS / Network level** (infrastructure faults)

Directly manipulates the Linux kernel via privileged tools.  Requires a Linux
target and ``sudo``.

.. code-block:: text

   Your app
      │
   [ Linux kernel — tc/netem / BPF / stress-ng / systemctl / docker ]
      │
   Network / Storage / CPU / Memory / Disk

Faults at this level: ``NetworkDelay``, ``NetworkLoss``, ``StorageCorrupt``,
``CPUStress``, ``MemoryStress``, ``IOStress``, ``ProcessKill``,
``ServiceFault``, ``ContainerKill``.

----

**2. HTTP proxy level** (LLM API faults)

A local MITM proxy sits between the LLM SDK and the real API endpoint.  The
SDK is pointed at ``localhost:<port>`` and the proxy applies faults before
forwarding.

.. code-block:: text

   LLM SDK
      │  (redirected to localhost:port)
   [ CJ proxy ]
      ├── apply fault (latency / 429 / 503 / corrupt / hallucinate …)
      └── forward to real API  ──►  api.openai.com / api.anthropic.com / …

Faults at this level: ``LLMLatency``, ``LLMRateLimit``, ``LLMTimeout``,
``LLMResponseCorrupt``, ``LLMUnavailable``, ``LLMHallucination``,
``LLMStreamInterrupt``, ``LLMTokenStarvation``, ``ToolFault``, ``MCPFault``,
``SemanticCorrupt``.

Requires telling the SDK to point at the proxy:

.. code-block:: python

   import openai, os
   os.environ["OPENAI_BASE_URL"] = f"http://localhost:{runner.proxy_port}/v1"

----

**3. HTTP transport level** (intercept layer)

Patches ``httpx`` and ``requests`` **at the class level** so every SDK that
uses them is affected automatically — no proxy port, no SDK reconfiguration.

.. code-block:: text

   LLM SDK  (OpenAI / Anthropic / LiteLLM / LangChain / …)
      │  uses httpx or requests internally
   [ CJ transport patch — _CJTransport / _CJAdapter ]
      ├── apply Behavior.before(url)   (latency / timeout / …)
      ├── call real send()
      └── apply Behavior.after(url, response)  (corrupt / 429 / 503 / …)

Faults at this level: ``Latency``, ``Jitter``, ``RateLimit``,
``Unavailable``, ``Timeout``, ``CorruptResponse`` (from ``chaos_jungle.intercept``).

Works on any OS.  No port setup needed.

----

Target Plane
------------

A **Target** is an abstraction over a machine.  The runner and faults call
``target.run(cmd)``, ``target.sudo(cmd)``, and ``target.put(file)``; the
target handles the transport.

.. code-block:: text

   ChaosRunner
       │
       ├── LocalTarget  ──►  subprocess.run()  (same machine)
       │
       ├── SSHTarget    ──►  Paramiko SSH  ──►  remote Linux server
       │
       └── HTTPTarget   ──►  HTTP POST /exec  ──►  cj-daemon (remote agent)

``cj-daemon`` is a lightweight REST agent that you install on machines that
are behind a firewall or in a CI runner.  It receives ``/exec`` commands from
``HTTPTarget`` and executes them locally.

.. code-block:: text

   ┌─────────────────────┐          ┌───────────────────────┐
   │   Test runner host  │  HTTP    │   Target machine      │
   │                     │ ───────► │                       │
   │  ChaosRunner        │          │  cj-daemon  :8642     │
   │  + HTTPTarget       │ ◄─────── │  → runs tc / stress   │
   └─────────────────────┘  result  └───────────────────────┘

----

Evaluation Plane
----------------

chaos-jungle can measure whether faults actually degrade quality, not just
whether they execute.

.. code-block:: text

   runner.measure(workload, n_baseline=5, n_fault=5, evaluator=judge)
        │
        ├── run workload N times  (no fault)  → baseline metrics
        ├── inject fault
        ├── run workload N times  (with fault) → fault metrics
        └── compute delta → MeasurementResult

``MeasurementResult`` contains:

* raw metric dicts for baseline and fault phases
* ``delta`` — difference in every numeric metric
* optional ``LLMJudge`` quality scores (faithfulness, hallucination,
  coherence, guardrail_violation)
* ``passed_quality(...)`` — boolean quality gate

``LLMJudge`` calls a second "judge" model to evaluate responses — it does not
run inside your application under test.

----

Data Plane
----------

Every experiment writes structured data to a local SQLite database:

.. code-block:: text

   ~/.chaos-jungle/chaos_jungle.db
   │
   ├── sessions  — one row per ChaosRunner.start() call
   ├── faults    — one row per active fault, with kind + parameters
   ├── events    — timestamped log messages (fault started, stopped, errors)
   ├── results   — arbitrary JSON blobs from runner.record_result()
   └── commands  — every shell command executed on every target

Data flows downstream to:

.. code-block:: text

   SQLite DB
      ├── Web dashboard   (chaos-jungle dashboard)
      ├── CSV export      (export_db_to_csv)
      └── CLI summary     (chaos-jungle list)

----

Component Map
-------------

.. code-block:: text

   chaos_jungle/
   ├── scenario.py        Scenario dataclass
   ├── runner.py          ChaosRunner, MeasurementResult, door()
   ├── suite.py           ExperimentSuite
   ├── decorators.py      @chaos, @chaos_session, @chaos_measure
   ├── intercept.py       inject(), door(), Behavior subclasses
   ├── pytest_plugin.py   @pytest.mark.chaos auto-fixture
   │
   ├── faults/
   │   ├── network.py     NetworkDelay, NetworkLoss, NetworkCorrupt …
   │   ├── storage.py     StorageCorrupt
   │   ├── llm.py         LLMLatency, LLMRateLimit, LLMHallucination …
   │   ├── semantic.py    SemanticCorrupt
   │   ├── state.py       RedisStateCorrupt, JsonStateCorrupt …
   │   ├── process.py     ProcessKill, ServiceFault, ContainerKill
   │   ├── resources.py   CPUStress, MemoryStress, IOStress, DiskFull
   │   └── bpf.py         SilentNetworkCorrupt, iface_for_ip
   │
   ├── targets/
   │   ├── local.py       LocalTarget
   │   ├── ssh.py         SSHTarget
   │   └── http.py        HTTPTarget
   │
   ├── metrics.py         PingLatency, CommandMetric, FileIntegrity …
   ├── judge.py           LLMJudge, JudgeScore, average_scores
   ├── session_db.py      SQLite schema + helpers
   ├── dashboard.py       FastAPI web dashboard
   ├── daemon.py          cj-daemon REST agent
   ├── guardrails.py      ConflictError / ConflictWarning
   └── preflight.py       tool detection + auto-install

----

Design Principles
-----------------

**No vendor lock-in.**
Faults work with any LLM provider.  The intercept layer patches ``httpx`` and
``requests`` at the class level so OpenAI, Anthropic, LiteLLM, LangChain, and
any other SDK that relies on those libraries is covered automatically.

**Layered, composable faults.**
Multiple faults can be active simultaneously.  The runner injects them in
order and removes them in reverse.  The intercept layer supports nested
``inject()`` contexts that stack their effects.

**Revertible by default.**
Every fault implements ``revert()`` to undo persistent side-effects.
``StorageCorrupt`` keeps a backup of every file it touches.  ``DiskFull``
removes the padding file on stop.

**Zero infrastructure for LLM tests.**
``inject()`` works on any OS — macOS, Windows, Linux — with no ``sudo``, no
port forwarding, and no proxy process.  Just ``pip install chaos-jungle`` and
wrap your code.

**Observability first.**
Every action is written to SQLite.  The dashboard, CSV export, and CLI all
read the same database, so you always have a full audit trail of what happened
and when.

----

See also
--------

* :doc:`concepts` — full fault catalogue and API abstractions
* :ref:`guide-intercept` — HTTP transport intercept in depth
* :ref:`guide-measurement` — ``runner.measure()`` and quality gates
* :ref:`guide-strategies` — when and how to apply faults
