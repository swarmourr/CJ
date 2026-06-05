.. _architecture:

Architecture
============

chaos-jungle is organised into five planes that work together to inject,
observe, and evaluate faults across any layer of a modern system.

.. code-block:: text

   ╔══════════════════════════════════════════════════════════════════╗
   ║                       CONTROL  PLANE                            ║
   ║                                                                  ║
   ║   Scenario ──── ChaosRunner ──── ExperimentSuite                ║
   ║   @chaos · @chaos_measure · inject() · door()                   ║
   ╚═══════════════════════╤══════════════════════════════════════════╝
                           │
           ┌───────────────┼───────────────┐
           ▼               ▼               ▼
   ╔═══════════════╗ ╔════════════╗ ╔═══════════════════╗
   ║  TRANSPORT    ║ ║   TARGET   ║ ║   EVALUATION      ║
   ║    PLANE      ║ ║   PLANE    ║ ║     PLANE         ║
   ╠═══════════════╣ ╠════════════╣ ╠═══════════════════╣
   ║  HTTP proxy   ║ ║  Local     ║ ║  LLMJudge         ║
   ║  httpx patch  ║ ║  SSH       ║ ║  Metrics          ║
   ║  OS / BPF     ║ ║  HTTP      ║ ║  Quality gates    ║
   ╚═══════╤═══════╝ ╚══════╤═════╝ ╚═════════╤═════════╝
           │                │                 │
           └────────────────┼─────────────────┘
                            ▼
   ╔══════════════════════════════════════════════════════════════════╗
   ║                        DATA  PLANE                              ║
   ║                                                                  ║
   ║   SQLite DB  ──►  Web Dashboard  ──►  CSV Export  ──►  CLI      ║
   ╚══════════════════════════════════════════════════════════════════╝

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

   ╔═══════════╗     ╔═══════════╗     ╔═══════════╗
   ║ PREFLIGHT ║────►║   START   ║────►║ WORKLOAD  ║
   ║           ║     ║           ║     ║           ║
   ║ check     ║     ║ inject    ║     ║ your code ║
   ║ tools     ║     ║ faults    ║     ║ runs here ║
   ╚═══════════╝     ╚═══════════╝     ╚═════╤═════╝
                                             │
   ╔═══════════╗     ╔═══════════╗     ╔═════▼═════╗
   ║  RECORD   ║◄────║  REVERT   ║◄────║   STOP    ║
   ║           ║     ║           ║     ║           ║
   ║ write to  ║     ║ undo side ║     ║ remove    ║
   ║ SQLite    ║     ║ effects   ║     ║ faults    ║
   ╚═══════════╝     ╚═══════════╝     ╚═══════════╝

----

Transport Plane
---------------

Faults are injected at three different depths depending on what layer you want
to test.

**1. OS / Network level** (infrastructure faults)

Directly manipulates the Linux kernel via privileged tools.  Requires a Linux
target and ``sudo``.

.. code-block:: text

   ┌──────────────────────────────────────────────────────┐
   │                  YOUR  APPLICATION                   │
   └────────────────────────┬─────────────────────────────┘
              syscalls / file I/O / network packets
   ┌────────────────────────▼─────────────────────────────┐
   │               LINUX  KERNEL  LAYER                   │
   │                                                      │
   │  ╔══════════════╗  ╔═══════════╗  ╔═══════════════╗  │
   │  ║   tc/netem   ║  ║    BPF    ║  ║  stress-ng    ║  │
   │  ║              ║  ║           ║  ║  systemctl    ║  │
   │  ║ NetworkDelay ║  ║ SilentNet ║  ║  docker       ║  │
   │  ║ NetworkLoss  ║  ║ Corrupt   ║  ║  pkill        ║  │
   │  ╚══════════════╝  ╚═══════════╝  ╚═══════════════╝  │
   └──────────────────────────────────────────────────────┘
            Network · Storage · CPU · Memory · Disk

----

**2. HTTP proxy level** (LLM API faults)

A local MITM proxy sits between the LLM SDK and the real API endpoint.  The
SDK is pointed at ``localhost:<port>`` and the proxy applies faults before
forwarding.

.. code-block:: text

   ┌──────────────────────────────────────────────────────┐
   │            LLM  SDK  (any provider)                  │
   └────────────────────────┬─────────────────────────────┘
              redirected to localhost:<port>
   ┌────────────────────────▼─────────────────────────────┐
   │                    CJ  PROXY                         │
   │                                                      │
   │  ① match request URL against fault rules             │
   │  ② apply fault ─► latency · 429 · 503 · corrupt      │
   │                    hallucinate · truncate · timeout   │
   │  ③ forward (or short-circuit)                        │
   └────────────────────────┬─────────────────────────────┘
                       HTTPS tunnel
   ┌────────────────────────▼─────────────────────────────┐
   │              REAL  API  ENDPOINT                     │
   │   api.openai.com · api.anthropic.com · ollama …      │
   └──────────────────────────────────────────────────────┘

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

   ┌──────────────────────────────────────────────────────────────┐
   │   LLM SDK  (OpenAI · Anthropic · LiteLLM · LangChain …)     │
   │                  uses httpx or requests internally           │
   └──────────────────────────┬───────────────────────────────────┘
                              │  patched at class level
   ┌──────────────────────────▼───────────────────────────────────┐
   │               CJ  TRANSPORT  PATCH                           │
   │                                                              │
   │  ① Behavior.before(url) ─── latency · jitter · timeout       │
   │  ② real send()          ─── actual HTTP/HTTPS request        │
   │  ③ Behavior.after(url)  ─── corrupt · 429 · 503              │
   │                                                              │
   │  probability roll ──► each behavior fires independently      │
   └──────────────────────────┬───────────────────────────────────┘
                              │  real TCP connection
   ┌──────────────────────────▼───────────────────────────────────┐
   │                   API  ENDPOINT                              │
   └──────────────────────────────────────────────────────────────┘

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

   ╔═════════════════════════════════════════════════════════╗
   ║                     ChaosRunner                         ║
   ╚══════════════╤═══════════════╤══════════════════╤════════╝
                  │               │                  │
        ┌─────────▼──────┐ ┌──────▼───────┐ ┌───────▼──────────┐
        │  LocalTarget   │ │  SSHTarget   │ │  HTTPTarget      │
        │                │ │              │ │                  │
        │ subprocess.run │ │ Paramiko SSH │ │ HTTP POST /exec  │
        └────────┬───────┘ └──────┬───────┘ └───────┬──────────┘
                 │                │                 │
                 ▼                ▼                 ▼
          same  machine     remote  Linux      cj-daemon :8642

``cj-daemon`` is a lightweight REST agent for machines that are behind a
firewall or inside a CI runner.

.. code-block:: text

   ┌──────────────────────────────┐         ┌──────────────────────────────┐
   │     TEST  RUNNER  HOST       │         │      TARGET  MACHINE         │
   │                              │         │                              │
   │  ┌────────────────────────┐  │  HTTP   │  ┌──────────────────────┐   │
   │  │  ChaosRunner           │  │ ──────► │  │   cj-daemon :8642    │   │
   │  │  + HTTPTarget          │  │         │  │                      │   │
   │  └────────────────────────┘  │ ◄────── │  │  POST /exec          │   │
   │                              │ result  │  │  → tc · stress-ng    │   │
   └──────────────────────────────┘         │  │  → systemctl · docker│   │
                                            │  └──────────────────────┘   │
                                            └──────────────────────────────┘

----

Evaluation Plane
----------------

chaos-jungle can measure whether faults actually degrade quality, not just
whether they execute.

.. code-block:: text

   runner.measure(workload, n_baseline=5, n_fault=5, evaluator=judge)
   │
   ├─► PHASE 1 ─ BASELINE  ── run workload × n_baseline ──► baseline metrics
   │
   ├─► PHASE 2 ─ FAULT ON  ── inject faults
   │
   ├─► PHASE 3 ─ FAULT     ── run workload × n_fault    ──► fault metrics
   │
   ├─► PHASE 4 ─ FAULT OFF ── stop faults
   │
   └─► PHASE 5 ─ EVALUATE  ── compute delta + LLMJudge scores
                                       │
                    ╔══════════════════▼═══════════════════════╗
                    ║          MeasurementResult               ║
                    ╠══════════════════════════════════════════╣
                    ║  baseline  │  fault  │  delta            ║
                    ║  judge scores (faithfulness, coherence)  ║
                    ║  passed_quality(min_faithfulness=0.7)    ║
                    ╚══════════════════════════════════════════╝

``LLMJudge`` calls a second "judge" model to evaluate responses — it does not
run inside your application under test.

----

Data Plane
----------

Every experiment writes structured data to a local SQLite database.

.. code-block:: text

   ╔══════════════════════════════════════════════════════════╗
   ║          ~/.chaos-jungle/chaos_jungle.db                ║
   ╠══════════════════════════════════════════════════════════╣
   ║  sessions  ── one row per ChaosRunner.start() call      ║
   ║  faults    ── one row per active fault + parameters      ║
   ║  events    ── timestamped log (started · stopped · err)  ║
   ║  results   ── JSON blobs from runner.record_result()     ║
   ║  commands  ── every shell command on every target        ║
   ╚══════════════════════════╤═══════════════════════════════╝
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
       ╔═════════════╗  ╔═══════════╗  ╔═══════════╗
       ║  Dashboard  ║  ║    CSV    ║  ║    CLI    ║
       ║   :8080     ║  ║  export   ║  ║  summary  ║
       ╚═════════════╝  ╚═══════════╝  ╚═══════════╝
       chaos-jungle      export_db      chaos-jungle
       dashboard         _to_csv()      list

----

Component Map
-------------

.. code-block:: text

   chaos_jungle/
   │
   ├── scenario.py        ── Scenario dataclass
   ├── runner.py          ── ChaosRunner · MeasurementResult · door()
   ├── suite.py           ── ExperimentSuite
   ├── decorators.py      ── @chaos · @chaos_session · @chaos_measure
   ├── intercept.py       ── inject() · door() · Behavior subclasses
   ├── pytest_plugin.py   ── @pytest.mark.chaos auto-fixture
   │
   ├── faults/
   │   ├── network.py     ── NetworkDelay · NetworkLoss · NetworkCorrupt …
   │   ├── storage.py     ── StorageCorrupt
   │   ├── llm.py         ── LLMLatency · LLMRateLimit · LLMHallucination …
   │   ├── semantic.py    ── SemanticCorrupt
   │   ├── state.py       ── RedisStateCorrupt · JsonStateCorrupt …
   │   ├── process.py     ── ProcessKill · ServiceFault · ContainerKill
   │   ├── resources.py   ── CPUStress · MemoryStress · IOStress · DiskFull
   │   └── bpf.py         ── SilentNetworkCorrupt · iface_for_ip
   │
   ├── targets/
   │   ├── local.py       ── LocalTarget
   │   ├── ssh.py         ── SSHTarget
   │   └── http.py        ── HTTPTarget
   │
   ├── metrics.py         ── PingLatency · CommandMetric · FileIntegrity …
   ├── judge.py           ── LLMJudge · JudgeScore · average_scores
   ├── session_db.py      ── SQLite schema + helpers
   ├── dashboard.py       ── FastAPI web dashboard
   ├── daemon.py          ── cj-daemon REST agent
   ├── guardrails.py      ── ConflictError / ConflictWarning
   └── preflight.py       ── tool detection + auto-install

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
