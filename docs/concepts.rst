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

**Infrastructure — Network** (:ref:`guide-network`)

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
   * - ``NetworkBandwidthLimit``
     - Cap interface throughput to a maximum rate
     - tc netem rate
   * - ``NetworkReorder``
     - Deliver a fraction of packets out of order
     - tc netem reorder
   * - ``NetworkReset``
     - Inject TCP RST to abruptly terminate connections
     - iptables
   * - ``NetworkPartition``
     - Drop all traffic to/from a specific IP
     - iptables
   * - ``SilentNetworkCorrupt``
     - Flip bits silently — TCP checksum still valid
     - BPF / XDP

**Infrastructure — Storage** (:ref:`guide-storage`)

.. list-table::
   :header-rows: 1
   :widths: 30 45 25

   * - Class
     - Effect
     - Tool
   * - ``StorageCorrupt``
     - Flip random bytes in files on a crontab schedule; fully revertible
     - dd + cj_storage
   * - ``StorageCorruptImmediate``
     - Corrupt specific bytes in a file instantly at ``start()``
     - dd
   * - ``SQLiteCorrupt``
     - Overwrite a SQLite page — triggers "disk image is malformed"
     - dd

**Infrastructure — Process / Service / Container** (:ref:`guide-process`)

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

**Infrastructure — Resource Exhaustion** (:ref:`guide-resources`)

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
   * - ``InodeFull``
     - Exhaust filesystem inodes with empty files
     - touch / xargs
   * - ``FDExhaust``
     - Hold open ``count`` file descriptors until ``ulimit`` is hit
     - python3
   * - ``ProcessExhaust``
     - Fork ``count`` background processes to hit kernel PID limit
     - bash / sleep

**LLM / AI — API Faults** (:ref:`guide-llm`)

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
   * - ``LLMUnauthorized``
     - Return 401 (invalid / expired API key) with realistic delay
     - HTTP proxy
   * - ``LLMForbidden``
     - Return 403 (permission boundary) with realistic delay
     - HTTP proxy
   * - ``LLMAuthExpiry``
     - First N calls succeed, then 401 (token expiry simulation)
     - HTTP proxy
   * - ``LLMContextLengthExceeded``
     - Return 400 ``context_length_exceeded`` to test chunking fallbacks
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
   * - ``LLMBudgetExceeded``
     - Return 402 once cumulative cost exceeds ``max_cost_usd``
     - HTTP proxy
   * - ``ToolFault``
     - Fail all tool calls (or a named tool)
     - HTTP proxy
   * - ``MCPFault``
     - Fail / timeout / drop MCP server calls
     - HTTP proxy

**LLM / AI — SDK Intercept Behaviors** (:ref:`guide-intercept`)

.. list-table::
   :header-rows: 1
   :widths: 30 45 25

   * - Class
     - Effect
     - Mechanism
   * - ``Latency``
     - Sleep N seconds before every matching request
     - Transport patch
   * - ``Jitter``
     - Sleep a random duration between min and max seconds
     - Transport patch
   * - ``RateLimit``
     - Return 429 after the first N requests succeed
     - Transport patch
   * - ``Unavailable``
     - Return 503 for every matching request
     - Transport patch
   * - ``Timeout``
     - Raise ``httpx.TimeoutException`` / ``requests.Timeout``
     - Transport patch
   * - ``CorruptResponse``
     - Return 200 with garbled JSON body
     - Transport patch
   * - ``Unauthorized``
     - Return 401 with realistic delay; optional ``after_n`` pass-through
     - Transport patch
   * - ``Forbidden``
     - Return 403 with realistic delay
     - Transport patch
   * - ``AuthExpiry``
     - First N calls succeed, then 401
     - Transport patch
   * - ``ToolMutate``
     - Silently corrupt tool-call results before the LLM sees them
     - Transport patch
   * - ``PromptInjection``
     - Append adversarial text to outgoing LLM messages
     - Transport patch

**LLM / AI — Semantic Faults** (:ref:`guide-semantic`)

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

**LLM / AI — State Faults** (:ref:`guide-state`)

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

**LLM / AI — Gateway Faults** (:ref:`guide-gateway`)

.. list-table::
   :header-rows: 1
   :widths: 30 45 25

   * - Class
     - Effect
     - Mechanism
   * - ``GatewayRouteMisconfig``
     - Rewrite ``model`` to a different provider/model
     - Transport patch
   * - ``GatewayFallbackBroken``
     - Both primary and fallback routes fail
     - Transport patch
   * - ``GatewayPolicyBlock``
     - False-positive content filter blocks a safe request
     - Transport patch
   * - ``GatewayPolicyBypass``
     - Safety filter disabled — unsafe request passes through
     - Transport patch
   * - ``GatewayCacheStale``
     - Outdated cached answer returned
     - Transport patch
   * - ``GatewayCachePoison``
     - Wrong cached response reused from a different query
     - Transport patch
   * - ``GatewayTenantLeak``
     - Another tenant's data injected into the response
     - Transport patch
   * - ``GatewayHeaderStrip``
     - Auth/org/routing headers removed before forwarding
     - Transport patch
   * - ``GatewayToolSchemaDrop``
     - ``tools`` array removed — agent cannot call tools
     - Transport patch
   * - ``GatewayResponseRewrite``
     - Specific response fields overwritten by gateway
     - Transport patch
   * - ``GatewayBudgetDesync``
     - Gateway returns 402 due to stale budget state
     - Transport patch
   * - ``GatewayRetryStorm``
     - Repeated 429s provoke aggressive SDK retry behaviour
     - Transport patch

**LLM / AI — Skill File Faults** (:ref:`guide-skill`)

.. list-table::
   :header-rows: 1
   :widths: 30 45 25

   * - Class
     - Effect
     - Tool
   * - ``SkillFileUnavailable``
     - Empty the skill file — agent has no instructions
     - file I/O
   * - ``SkillFileInstructionCorrupt``
     - Garble the instruction body (shuffle / truncate / contradict)
     - file I/O
   * - ``SkillFileVersionSkew``
     - Replace version field in frontmatter with an old version string
     - file I/O
   * - ``SkillFileBadOutput``
     - Corrupt examples section (empty / wrong / truncate)
     - file I/O
   * - ``SkillFileMemoryStale``
     - Replace examples/context with caller-supplied stale data
     - file I/O
   * - ``SkillFileConflict``
     - Append a contradictory override block at the end of the file
     - file I/O
   * - ``SkillFilePermissionDenied``
     - Set file permissions to 000 — agent cannot read it
     - chmod
   * - ``SkillJSONCorrupt``
     - Corrupt a field inside a JSON tool-definition file
     - file I/O

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
