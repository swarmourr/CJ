Concepts
========

chaos-jungle is built around four simple abstractions: **Fault**, **Target**,
**Scenario**, and **ChaosRunner**.  Everything else — metrics, quality scoring,
the session database — builds on top of these four.

----

Fault
-----

A **fault** is one injectable failure mode.  Every fault implements three
methods:

* ``start(target)`` — inject the fault
* ``stop(target)`` — remove the fault
* ``revert(target)`` — undo any persistent side effects (e.g. restore corrupted files)

Faults are grouped by the layer they target:

**Infrastructure — Network**

.. list-table::
   :header-rows: 1
   :widths: 30 45 25

   * - Class
     - Effect
     - Tool
   * - ``NetworkDelay``
     - Add artificial RTT latency + jitter
     - tc netem
   * - ``NetworkLoss``
     - Drop N % of packets
     - tc netem
   * - ``NetworkCorrupt``
     - Corrupt N % of packets (checksum fixed by TC)
     - tc netem
   * - ``NetworkDuplicate``
     - Duplicate N % of packets
     - tc netem
   * - ``SilentNetworkCorrupt``
     - Flip bits silently — TCP checksum still valid
     - BPF / XDP

**Infrastructure — Storage**

.. list-table::
   :header-rows: 1
   :widths: 30 45 25

   * - Class
     - Effect
     - Tool
   * - ``StorageCorrupt``
     - Flip random bytes in files on a schedule; fully revertible
     - dd + cj_storage

**Infrastructure — Process / Service / Container**

.. list-table::
   :header-rows: 1
   :widths: 30 45 25

   * - Class
     - Effect
     - Tool
   * - ``ProcessKill``
     - Kill OS processes matching a command pattern
     - pkill
   * - ``ServiceFault``
     - Stop / restart / kill / mask a systemd service
     - systemctl
   * - ``ContainerKill``
     - Kill / stop / pause / remove a Docker container
     - docker

**Infrastructure — Resource Exhaustion**

.. list-table::
   :header-rows: 1
   :widths: 30 45 25

   * - Class
     - Effect
     - Tool
   * - ``DiskFull``
     - Fill a filesystem near capacity
     - dd
   * - ``CPUStress``
     - Saturate N CPU cores
     - stress-ng
   * - ``MemoryStress``
     - Allocate N MiB of RAM (forces swapping)
     - stress-ng
   * - ``IOStress``
     - Generate sustained disk I/O load
     - stress-ng

**LLM / AI — API Faults**

.. list-table::
   :header-rows: 1
   :widths: 30 45 25

   * - Class
     - Effect
     - Mechanism
   * - ``LLMLatency``
     - Add delay to every LLM response
     - HTTP proxy
   * - ``LLMRateLimit``
     - Return 429 after N calls
     - HTTP proxy
   * - ``LLMTimeout``
     - Hang the connection for N seconds
     - HTTP proxy
   * - ``LLMResponseCorrupt``
     - Truncate / empty / invalidate the response JSON
     - HTTP proxy
   * - ``LLMUnavailable``
     - Return 503 for every call
     - HTTP proxy
   * - ``LLMHallucination``
     - Replace the model's answer with a false statement
     - HTTP proxy
   * - ``LLMStreamInterrupt``
     - Cut a streaming response after N SSE events
     - HTTP proxy
   * - ``LLMTokenStarvation``
     - Force max_tokens to a very small value
     - HTTP proxy
   * - ``ToolFault``
     - Fail all tool calls (or a named tool)
     - HTTP proxy
   * - ``MCPFault``
     - Fail / timeout / drop MCP server calls
     - HTTP proxy

**LLM / AI — Semantic Faults**

.. list-table::
   :header-rows: 1
   :widths: 30 45 25

   * - Class
     - Effect
     - Mechanism
   * - ``SemanticCorrupt``
     - Mutate the *meaning* of the LLM request without breaking JSON
     - HTTP proxy

  Modes: ``entity_swap``, ``context_truncate``, ``inject_distractor``, ``rag_poison``

**LLM / AI — State Faults**

.. list-table::
   :header-rows: 1
   :widths: 30 45 25

   * - Class
     - Effect
     - Tool
   * - ``RedisStateCorrupt``
     - Mutate Redis keys matching a glob pattern
     - redis-cli
   * - ``JsonStateCorrupt``
     - Mutate a dot-path field in a JSON checkpoint file
     - python + jq
   * - ``PostgresStateCorrupt``
     - Run a parameterised UPDATE on a Postgres column
     - psql

  Mutation modes: ``nullify``, ``delete``, ``negate``, ``type_mismatch``, ``inject``

----

Target
------

A **target** is a machine.  It knows how to run commands, transfer files,
and execute privileged operations (``sudo``).

.. list-table::
   :header-rows: 1
   :widths: 20 40 40

   * - Class
     - Connects via
     - Typical use
   * - ``LocalTarget``
     - subprocess on local machine
     - LLM / AI faults on macOS; quick local tests
   * - ``SSHTarget``
     - Paramiko SSH to a remote machine
     - Infrastructure faults on Linux servers
   * - ``HTTPTarget``
     - HTTP requests to a running ``cj-daemon``
     - Firewall-protected machines; GitLab CI runners

.. note::

   **LLM / AI faults** (proxy-based) work on **any OS** with ``LocalTarget``
   — no Linux or sudo required.

   **Infrastructure faults** (network, storage, process, resources) require
   a **Linux target** and ``sudo`` for privileged commands.

----

Scenario
--------

A **scenario** is a named list of faults — pure data, no logic.

.. code-block:: python

   from chaos_jungle import Scenario, NetworkDelay, NetworkLoss

   scenario = Scenario("wan-degraded", faults=[
       NetworkDelay("200ms", jitter="20ms"),
       NetworkLoss("2%"),
   ])

Multiple faults in a scenario are injected in order and removed in reverse
order on ``stop()``.

----

ChaosRunner
-----------

The **runner** orchestrates the full fault lifecycle:

.. code-block:: text

   preflight  →  start  →  [workload]  →  stop  →  revert

It writes every action to the SQLite session database and provides three
usage styles:

**Explicit start / stop:**

.. code-block:: python

   runner = ChaosRunner(scenario, target)
   runner.start()
   run_workload()
   runner.stop()

**Decorator:**

.. code-block:: python

   @chaos(NetworkDelay("100ms"))
   def run_workload():
       ...

**Context manager:**

.. code-block:: python

   with chaos_session(NetworkLoss("5%")) as session:
       run_workload()

**Measure mode** (records baseline and fault metrics side-by-side):

.. code-block:: python

   def workload():
       t0 = time.time()
       call_my_service()
       return {"duration_s": round(time.time() - t0, 2)}

   result = runner.measure(workload, n_baseline=5, n_fault=5)
   print(result.summary())

----

Session Database
----------------

Every experiment is recorded in a SQLite database at
``~/.chaos-jungle/chaos_jungle.db``.

Schema:

.. code-block:: sql

   sessions (id, name, started_at, stopped_at, status)
   faults   (id, session_id, kind, parameters, started_at, stopped_at)
   events   (id, session_id, fault_id, timestamp, message)
   results  (id, session_id, data_json, recorded_at)
   commands (id, session_id, cmd, stdout, stderr, rc, executed_at)

``results`` rows are written by ``runner.record_result(metrics_dict)`` and
appear in exported CSV files and the web dashboard.

``commands`` rows capture every command run via ``target.run()`` /
``target.sudo()`` — useful for auditing what happened on the target machine.

Export to CSV:

.. code-block:: python

   from chaos_jungle import export_db_to_csv
   export_db_to_csv("~/.chaos-jungle/chaos_jungle.db", "./results/")

----

LLMJudge
---------

``LLMJudge`` is an optional evaluator that scores LLM responses on four
quality dimensions using a second "judge" model:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Metric
     - Meaning
   * - ``faithfulness``
     - 0–1: how closely the answer follows the provided context
   * - ``hallucination``
     - 0–1: fraction of the answer that is fabricated
   * - ``coherence``
     - 0–1: grammatical and logical coherence of the response
   * - ``guardrail_violation``
     - True / False: did the model follow an injected instruction?

Use with ``runner.measure()`` to get baseline vs. fault quality deltas:

.. code-block:: python

   from chaos_jungle import LLMJudge

   judge  = LLMJudge(model="gpt-4o-mini")
   result = runner.measure(workload, n_baseline=5, n_fault=5, evaluator=judge)
   print(result.summary())
   print("Quality gate:", result.passed_quality(min_faithfulness=0.70, max_hallucination=0.30))
