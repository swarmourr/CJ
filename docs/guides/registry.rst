.. _guide-registry:

Scenario Registry
=================

The **ScenarioRegistry** gives every scenario a permanent UUID and tracks its
lifecycle — ``pending`` → ``running`` → ``done`` / ``failed`` — whether it
runs locally, on a remote machine over SSH, or through an HTTP daemon.

Each machine stores its own registry inside the existing chaos-jungle SQLite
database (``~/.chaos-jungle/chaos_jungle.db``).  The UUID is the shared key
that links registry entries across machines.  No shared database, no extra
service, no open ports.

----

How it works
------------

.. mermaid::

   sequenceDiagram
       participant L as Local machine
       participant R as Remote machine

       L->>L: Scenario("wan-test", [...]) → id = uuid4()
       L->>L: registry.register(s, type="ssh", target_ip="worker1")<br/>local DB: id=abc  status=pending
       L->>R: push_scenario(s) — serialize + SSH/HTTP
       R->>R: registry.register(s, type="local", source_ip="local")<br/>remote DB: id=abc  status=pending
       L->>R: run_scenario(id) — nohup / POST /run
       R->>R: ChaosRunner.start() → status=running
       R->>R: workload executes under fault
       R->>R: ChaosRunner.stop() → status=done  session_id=42
       loop every poll_interval (default 5 s)
           L->>R: scenario_status(id) — SSH exec / GET /status
           R-->>L: {"status": "running", ...}
       end
       L->>R: scenario_status(id)
       R-->>L: {"status": "done", "session_id": 42}
       L->>L: registry.set_done(id, session_id=42)<br/>local DB synced

----

Scenario UUID
-------------

Every ``Scenario`` receives a UUID automatically at creation time.  You never
set it manually.

.. code-block:: python

   from chaos_jungle import Scenario, NetworkDelay

   s = Scenario("wan-test", [NetworkDelay("100ms")])
   print(s.id)   # "a3f7c2d1-4eee-4a3f-9b47-1807c5fc0eaf"

The UUID is stable across serialization:

.. code-block:: python

   d  = s.to_dict()           # {"id": "a3f7c2d1-...", "name": ..., "faults": [...]}
   s2 = Scenario.from_dict(d) # reconstructs the exact same scenario + UUID

----

Scenario lifecycle
------------------

.. mermaid::

   stateDiagram-v2
       [*] --> pending : Scenario created<br/>(ChaosRunner init / push_scenario)
       pending --> running : runner.start() / run_scenario()
       running --> done : runner.stop() — fault reverted, session closed
       running --> failed : unhandled exception during workload
       done --> [*]
       failed --> [*]

----

Normal usage — ChaosRunner handles everything
---------------------------------------------

``ChaosRunner`` automatically registers every scenario on init and updates
its status through the lifecycle.  The API is identical regardless of target:

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner, NetworkDelay
   from chaos_jungle.targets import LocalTarget, SSHTarget, HTTPTarget

   scenario = Scenario("wan-test", [NetworkDelay("200ms")])

   # local — registered as type=local
   runner = ChaosRunner(scenario, LocalTarget())

   # SSH — registered as type=ssh, target_ip=worker1
   runner = ChaosRunner(scenario, SSHTarget("worker1", user="ubuntu"))

   # HTTP — registered as type=http, target_ip=worker1:7777
   runner = ChaosRunner(scenario, HTTPTarget("http://worker1:7777", token="secret"))

   # Same in all three cases:
   runner.start()      # status → running
   run_my_workload()
   runner.stop()       # status → done

   from chaos_jungle import ScenarioRegistry
   reg = ScenarioRegistry()
   print(reg.status(scenario.id))   # "done"

----

Advanced: fire-and-forget (autonomous remote run)
--------------------------------------------------

For cases where you want the **entire experiment** (fault injection + workload)
to run on the remote machine autonomously — with no open connection held from
your local machine — you can use the lower-level push/run/watch API:

.. code-block:: python

   from chaos_jungle import Scenario, NetworkDelay, ScenarioRegistry
   from chaos_jungle.targets import SSHTarget

   scenario = Scenario("wan-test", [NetworkDelay("200ms", jitter="20ms")])
   target   = SSHTarget("192.168.1.100", user="ubuntu")

   # 1. Register on both sides (same UUID)
   target.push_scenario(scenario)

   # 2. Fire in background on remote — SSH exec returns immediately
   target.run_scenario(scenario.id)

   # 3. Watch — polls remote registry via brief SSH connections
   entry = ScenarioRegistry().watch(scenario.id, target=target, poll_interval=5)
   print("Done. Session ID:", entry["session_id"])

   # 4. Fetch results
   target.get("~/.chaos-jungle/chaos_jungle.db", "./remote.db")

The same pattern works with ``HTTPTarget``:

.. code-block:: python

   from chaos_jungle.targets import HTTPTarget

   target = HTTPTarget("http://10.0.0.5:7777", token="mysecret")
   target.push_scenario(scenario)          # POST /scenarios
   target.run_scenario(scenario.id)        # POST /scenarios/{id}/run
   entry = ScenarioRegistry().watch(scenario.id, target=target)

----

Watching multiple scenarios
----------------------------

``watch_all()`` blocks until every scenario in the list reaches
``done`` or ``failed``.

.. code-block:: python

   from chaos_jungle import ScenarioRegistry

   registry = ScenarioRegistry()

   # Each scenario may run on a different target
   entry_map = registry.watch_all(
       [s1.id, s2.id, s3.id],
       targets={
           s2.id: ssh_target,
           s3.id: http_target,
           # s1 is local — no target needed
       },
       poll_interval=5,
       timeout=600,
   )

   for sid, entry in entry_map.items():
       print(sid[:8], entry["name"], "→", entry["status"])

----

CLI — checking status
---------------------

Use ``cj scenarios`` to inspect the registry from the command line.
These commands are **read-only** — they never start or stop anything.

.. code-block:: bash

   # List all scenarios in the local registry
   cj scenarios list

     ID                                    NAME             TYPE    TARGET              STATUS
     a3f7c2d1-4eee-4a3f-9b47-1807c5fc0eaf  wan-test         ssh     192.168.1.100       running
     b8e1f3a2-...                           cpu-stress       http    10.0.0.5            done
     c2d4a9b1-...                           local-test       local   -                   done

   # Check one scenario (human-readable)
   cj scenarios status a3f7c2d1

   # Check one scenario (JSON — useful for scripting)
   cj scenarios status a3f7c2d1 --json

   # Check a scenario on a remote machine
   cj scenarios status a3f7c2d1 --target ssh://ubuntu@192.168.1.100

   # Watch until done (polls every 5 s)
   cj scenarios watch a3f7c2d1

   # Watch multiple at once
   cj scenarios watch a3f7c2d1 b8e1f3a2

   # Watch a remote scenario
   cj scenarios watch a3f7c2d1 --target ssh://ubuntu@192.168.1.100

   # Custom poll interval and timeout
   cj scenarios watch a3f7c2d1 --interval 10 --timeout 1200

----

Registry entry schema
---------------------

Each registry entry is a dict with these fields:

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Field
     - Type
     - Meaning
   * - ``id``
     - str (UUID)
     - Unique identifier shared across machines
   * - ``name``
     - str
     - Human-readable scenario name
   * - ``type``
     - str
     - ``local`` | ``ssh`` | ``http``
   * - ``target_ip``
     - str
     - IP/host where the scenario runs (empty for local)
   * - ``source_ip``
     - str
     - IP of the machine that triggered it (empty if initiated here)
   * - ``status``
     - str
     - ``pending`` | ``running`` | ``done`` | ``failed``
   * - ``session_id``
     - int or None
     - Linked session in the chaos-jungle session DB (set when done)
   * - ``faults_json``
     - list
     - Serialized fault list (kind + params)
   * - ``created_at``
     - str (ISO 8601)
     - When the scenario was registered
   * - ``updated_at``
     - str (ISO 8601)
     - Last status update

----

ScenarioRegistry API
---------------------

.. code-block:: python

   from chaos_jungle import ScenarioRegistry

   reg = ScenarioRegistry()          # uses default DB at ~/.chaos-jungle/

   reg.register(scenario)            # type=local (auto-done by ChaosRunner)
   reg.get(scenario_id)              # → dict or None
   reg.status(scenario_id)           # → "pending" | "running" | "done" | "failed" | None
   reg.list(status="done")           # → list of entries
   reg.list(type="ssh")              # filter by target type
   reg.watch(scenario_id)            # block until done/failed (local)
   reg.watch(scenario_id, target=t)  # block + poll remote
   reg.watch_all([id1, id2, id3])    # wait for all
   reg.set_running(scenario_id)      # manual status override
   reg.set_done(scenario_id, session_id=42)
   reg.set_failed(scenario_id)

----

Scenario serialization
-----------------------

Scenarios can be serialized to a plain dict for storage, transfer, or
logging:

.. code-block:: python

   d = scenario.to_dict()
   # {
   #   "id": "a3f7c2d1-...",
   #   "name": "wan-test",
   #   "faults": [
   #     {"kind": "NetworkDelay", "params": {"delay": "200ms", "jitter": "20ms", "iface": ""}},
   #     {"kind": "NetworkLoss",  "params": {"rate": "2%", "iface": ""}}
   #   ]
   # }

   s2 = Scenario.from_dict(d)   # reconstruct — requires chaos-jungle on both ends

``from_dict()`` looks up each fault class by name from ``chaos_jungle.faults``.
Both machines must have the same version of chaos-jungle installed.

----

See also
--------

* :ref:`guide-ssh` — SSHTarget authentication and fault injection
* :ref:`guide-http` — HTTPTarget and cj-daemon setup
* :doc:`../concepts` — Scenario / Target / ChaosRunner model
* :doc:`../api/cli` — full CLI reference including ``cj scenarios``
