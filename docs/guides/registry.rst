.. _guide-registry:

Scenario Registry
=================

The **ScenarioRegistry** is chaos-jungle's lifecycle index for chaos scenarios.
It tracks scenario identity, target type, status, and result session ID so
local and remote chaos runs can be monitored and their data can be fetched later.

.. important::

   The registry does **not** execute your workload.  It tracks the lifecycle
   of the **fault injection session** — not what your application code does.
   Your workload always runs wherever your Python process is running.

``ChaosRunner`` manages the registry automatically.  You never interact with
it directly.

----

What the registry tracks
-------------------------

For every scenario, the registry records:

- **which scenario** was injected (UUID, name, fault list)
- **which target** received the fault (local / SSH / HTTP daemon)
- **current status** — ``pending`` → ``running`` → ``done`` / ``failed``
- **session_id** — links to the session DB once the run completes
- **where to fetch results** — the remote ``~/.chaos-jungle/chaos_jungle.db``
  and any logs when the target is not local

----

How it works
------------

.. mermaid::

   sequenceDiagram
       participant U as Your code
       participant CJ as ChaosRunner (local)
       participant R as Remote machine

       U->>CJ: ChaosRunner(scenario, SSHTarget("worker1"))
       Note over CJ: registry: id=abc  type=ssh  status=pending
       U->>CJ: runner.start()
       CJ->>R: inject fault over SSH (tc / stress-ng / …)
       Note over CJ: registry: status=running
       U->>U: run_my_workload()  — runs in your Python process
       U->>CJ: runner.stop()
       CJ->>R: revert fault over SSH
       Note over CJ: registry: status=done  session_id=42

The fault injection is controlled locally, through SSH, or through an HTTP
daemon.  Your workload runs wherever your Python code runs.

----

Full lifecycle
--------------

.. code-block:: text

   1. ChaosRunner created
      └─ registry entry created: status=pending

   2. runner.start()
      └─ fault commands sent to target (local / SSH / HTTP)
      └─ registry: status=running

   3. run_my_workload()
      └─ runs in your Python process — unrelated to registry

   4. runner.stop()
      └─ faults reverted on target
      └─ registry: status=done, session_id=<N>

   5. (remote targets) fetch results
      └─ chaos-jungle fetch --target ssh://ubuntu@worker1
      └─ downloads ~/.chaos-jungle/chaos_jungle.db and logs

.. mermaid::

   stateDiagram-v2
       [*] --> pending : ChaosRunner created
       pending --> running : runner.start()
       running --> done : runner.stop() — fault reverted
       running --> failed : unhandled exception
       done --> [*]
       failed --> [*]

----

ChaosRunner — same API for every target
----------------------------------------

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner, NetworkDelay
   from chaos_jungle.targets import LocalTarget, SSHTarget, HTTPTarget

   scenario = Scenario("wan-test", [NetworkDelay("200ms")])

   runner = ChaosRunner(scenario, LocalTarget())
   # or: ChaosRunner(scenario, SSHTarget("worker1", user="ubuntu"))
   # or: ChaosRunner(scenario, HTTPTarget("http://worker1:7777", token="secret"))

   runner.start()
   run_my_workload()   # this runs in your Python process
   runner.stop()

The registry is updated automatically at each step.  No extra code needed.

----

Registry vs workload vs fetching results
-----------------------------------------

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Concern
     - Answer
   * - Who tracks the registry?
     - ``ChaosRunner`` — automatically, on every ``start()`` and ``stop()``
   * - Does the registry run my workload?
     - No. Your workload runs in your Python process.  The registry only
       records what the fault session is doing.
   * - Where are results stored?
     - Locally in ``~/.chaos-jungle/chaos_jungle.db``.  For remote targets,
       the session DB is on the **remote** machine.
   * - How do I get remote results back?
     - ``chaos-jungle fetch --target ssh://ubuntu@worker1`` — downloads the
       remote DB and exports a CSV to your local machine.
   * - What is session_id?
     - The integer ID linking the registry entry to the full session record
       in the DB — events, faults, metrics, LLM calls.

----

Watching status from the CLI
-----------------------------

``chaos-jungle scenarios`` lets you observe the registry at any time.
These commands are **read-only** — they never start or stop anything.

.. code-block:: bash

   # List all scenarios
   chaos-jungle scenarios list

     ID                                    NAME        TYPE   TARGET         STATUS
     a3f7c2d1-4eee-4a3f-9b47-1807c5fc0eaf  wan-test    ssh    192.168.1.100  running
     b8e1f3a2-...                           cpu-stress  http   10.0.0.5       done
     c2d4a9b1-...                           local-test  local  —              done

   # Filter by status
   chaos-jungle scenarios list --status running

   # Check one scenario (human-readable)
   chaos-jungle scenarios status a3f7c2d1

   # Machine-readable JSON (useful for scripting)
   chaos-jungle scenarios status a3f7c2d1 --json

   # Block until a scenario finishes (polls every 5 s)
   chaos-jungle scenarios watch a3f7c2d1

   # Watch multiple at once
   chaos-jungle scenarios watch a3f7c2d1 b8e1f3a2

   # Watch a scenario on a remote machine
   chaos-jungle scenarios watch a3f7c2d1 --target ssh://ubuntu@192.168.1.100
   chaos-jungle scenarios watch a3f7c2d1 --target http://worker1:7777

----

See also
--------

* :ref:`guide-ssh` — SSHTarget authentication and fault injection
* :ref:`guide-http` — HTTPTarget and cj-daemon setup
* :doc:`../concepts` — Scenario / Target / ChaosRunner model
* :doc:`../api/cli` — full CLI reference including ``chaos-jungle scenarios``
