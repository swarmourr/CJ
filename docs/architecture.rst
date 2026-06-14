.. _architecture:

Architecture
============

chaos-jungle is organised into five planes that work together to inject,
observe, and evaluate faults across any layer of a modern system.

.. mermaid::

   flowchart TD
       CP["CONTROL PLANE\nScenario ── ChaosRunner ── ExperimentSuite\n@chaos · @chaos_measure · inject() · door()"]
       TP["TRANSPORT PLANE\nHTTP proxy\nhttpx patch\nOS / BPF"]
       TGP["TARGET PLANE\nLocal\nSSH\nHTTP"]
       EP["EVALUATION PLANE\nLLMJudge\nMetrics\nQuality gates"]
       DP["DATA PLANE\nSQLite DB ──► Web Dashboard ──► CSV Export ──► CLI"]

       CP --> TP
       CP --> TGP
       CP --> EP
       TP --> DP
       TGP --> DP
       EP --> DP

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

.. mermaid::

   flowchart LR
       PRE["PREFLIGHT\ncheck tools"]
       START["START\ninject faults"]
       WL["WORKLOAD\nyour code runs here"]
       STOP["STOP\nremove faults"]
       REV["REVERT\nundo side effects"]
       REC["RECORD\nwrite to SQLite"]

       PRE --> START --> WL --> STOP --> REV --> REC

----

Transport Plane
---------------

Faults are injected at three different depths depending on what layer you want
to test.

**1. OS / Network level** (infrastructure faults)

Directly manipulates the Linux kernel via privileged tools.  Requires a Linux
target and ``sudo``.

.. mermaid::

   flowchart TD
       APP["YOUR APPLICATION"]
       KERNEL["LINUX KERNEL LAYER"]
       TC["tc / netem\nNetworkDelay · NetworkLoss\nNetworkCorrupt · NetworkDuplicate"]
       BPF["BPF / XDP\nSilentNetworkCorrupt\nsilent bit-flips"]
       TOOLS["stress-ng · systemctl · docker · pkill\nNetwork · Storage · CPU · Memory · Disk"]

       APP -->|"syscalls / file I/O / network packets"| KERNEL
       KERNEL --> TC
       KERNEL --> BPF
       KERNEL --> TOOLS

----

**2. HTTP proxy level** (LLM API faults)

A local MITM proxy sits between the LLM SDK and the real API endpoint.  The
SDK is pointed at ``localhost:<port>`` and the proxy applies faults before
forwarding.

.. mermaid::

   flowchart TD
       SDK["LLM SDK (any provider)"]
       PROXY["CJ PROXY\n① match request URL against fault rules\n② apply fault: latency · 429 · 503 · corrupt\n   hallucinate · truncate · timeout\n③ forward or short-circuit"]
       API["REAL API ENDPOINT\napi.openai.com · api.anthropic.com · ollama …"]

       SDK -->|"redirected to localhost:&lt;port&gt;"| PROXY
       PROXY -->|"HTTPS tunnel"| API

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

.. mermaid::

   flowchart TD
       SDK2["LLM SDK — OpenAI · Anthropic · LiteLLM · LangChain\nuses httpx or requests internally"]
       PATCH["CJ TRANSPORT PATCH\n① Behavior.before(url) — latency · jitter · timeout\n② real send() — actual HTTP/HTTPS request\n③ Behavior.after(url) — corrupt · 429 · 503\nprobability roll: each behavior fires independently"]
       EP["API ENDPOINT"]

       SDK2 -->|"patched at class level"| PATCH
       PATCH -->|"real TCP connection"| EP

Faults at this level: ``Latency``, ``Jitter``, ``RateLimit``,
``Unavailable``, ``Timeout``, ``CorruptResponse`` (from ``chaos_jungle.intercept``).

Works on any OS.  No port setup needed.

----

Target Plane
------------

A **Target** is an abstraction over a machine.  The runner and faults call
``target.run(cmd)``, ``target.sudo(cmd)``, and ``target.put(file)``; the
target handles the transport.

.. mermaid::

   flowchart TD
       RUNNER["ChaosRunner"]
       LOCAL["LocalTarget\nsubprocess.run"]
       SSH["SSHTarget\nParamiko SSH"]
       HTTP["HTTPTarget\nHTTP POST /exec"]

       RUNNER --> LOCAL
       RUNNER --> SSH
       RUNNER --> HTTP

       LOCAL --> SAME["same machine"]
       SSH --> REMOTE["remote Linux"]
       HTTP --> DAEMON["cj-daemon :8642"]

``cj-daemon`` is a lightweight REST agent for machines that are behind a
firewall or inside a CI runner.

.. mermaid::

   flowchart LR
       subgraph TEST["TEST RUNNER HOST"]
           CR["ChaosRunner + HTTPTarget"]
       end
       subgraph TARGET["TARGET MACHINE"]
           DJ["cj-daemon :8642\nPOST /exec\n→ tc · stress-ng\n→ systemctl · docker"]
       end

       CR -->|"HTTP"| DJ
       DJ -->|"result"| CR

----

Evaluation Plane
----------------

chaos-jungle can measure whether faults actually degrade quality, not just
whether they execute.

.. mermaid::

   flowchart TD
       MEAS["runner.measure(workload, n_baseline=5, n_fault=5, evaluator=judge)"]
       P1["PHASE 1 — BASELINE\nrun workload × n_baseline\n→ baseline metrics"]
       P2["PHASE 2 — FAULT ON\ninject faults"]
       P3["PHASE 3 — FAULT\nrun workload × n_fault\n→ fault metrics"]
       P4["PHASE 4 — FAULT OFF\nstop faults"]
       P5["PHASE 5 — EVALUATE\ncompute delta + LLMJudge scores"]
       RES["MeasurementResult\nbaseline | fault | delta\njudge scores (faithfulness, coherence)\npassed_quality(min_faithfulness=0.7)"]

       MEAS --> P1 --> P2 --> P3 --> P4 --> P5 --> RES

``LLMJudge`` calls a second "judge" model to evaluate responses — it does not
run inside your application under test.

----

Data Plane
----------

Every experiment writes structured data to a local SQLite database.

.. mermaid::

   flowchart TD
       DB["~/.chaos-jungle/chaos_jungle.db\nsessions · faults · events · results · commands"]
       DASH["Dashboard :8080\nchaos-jungle dashboard"]
       CSV["CSV export\nexport_db_to_csv()"]
       CLI["CLI summary\nchaos-jungle list"]

       DB --> DASH
       DB --> CSV
       DB --> CLI

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
