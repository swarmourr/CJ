.. _guide-registry:

Scenario Registry
=================

Every scenario gets a unique ID automatically.  chaos-jungle tracks its
lifecycle — ``pending`` → ``running`` → ``done`` / ``failed`` — in a local
SQLite database, whether it runs on this machine, a remote SSH host, or an
HTTP daemon.

You never touch the registry directly.  ``ChaosRunner`` manages it for you.

----

How it works
------------

.. mermaid::

   sequenceDiagram
       participant U as Your code
       participant CJ as ChaosRunner (local)
       participant R as Remote machine

       U->>CJ: ChaosRunner(scenario, SSHTarget("worker1"))
       CJ->>CJ: registry: id=abc  type=ssh  status=pending
       U->>CJ: runner.start()
       CJ->>R: inject fault (tc / stress-ng / …)
       CJ->>CJ: registry: status=running
       U->>CJ: runner.stop()
       CJ->>R: revert fault
       CJ->>CJ: registry: status=done

----

Scenario lifecycle
------------------

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
   run_my_workload()
   runner.stop()

The registry is updated automatically at each step.  No extra code needed.

----

Watching status from the CLI
-----------------------------

``cj scenarios`` lets you observe the registry at any time.
These commands are **read-only** — they never start or stop anything.

.. code-block:: bash

   # List all scenarios
   cj scenarios list

     ID                                    NAME        TYPE   TARGET         STATUS
     a3f7c2d1-4eee-4a3f-9b47-1807c5fc0eaf  wan-test    ssh    192.168.1.100  running
     b8e1f3a2-...                           cpu-stress  http   10.0.0.5       done
     c2d4a9b1-...                           local-test  local  —              done

   # Filter by status
   cj scenarios list --status running

   # Check one scenario
   cj scenarios status a3f7c2d1

   # Machine-readable JSON output
   cj scenarios status a3f7c2d1 --json

   # Block until a scenario finishes (polls every 5 s)
   cj scenarios watch a3f7c2d1

   # Watch multiple at once
   cj scenarios watch a3f7c2d1 b8e1f3a2

   # Watch a scenario on a remote machine
   cj scenarios watch a3f7c2d1 --target ssh://ubuntu@192.168.1.100
   cj scenarios watch a3f7c2d1 --target http://worker1:7777

----

See also
--------

* :ref:`guide-ssh` — SSHTarget authentication and fault injection
* :ref:`guide-http` — HTTPTarget and cj-daemon setup
* :doc:`../concepts` — Scenario / Target / ChaosRunner model
* :doc:`../api/cli` — full CLI reference including ``cj scenarios``
