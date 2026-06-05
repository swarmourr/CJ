.. _guide-data:

Data & Export
=============

Every experiment is automatically recorded to a SQLite database.  No
configuration is needed — chaos-jungle creates the database on first run and
appends to it every time you start a session.

----

Database location
------------------

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - File
     - What it stores
   * - ``~/.chaos-jungle/chaos_jungle.db``
     - Session metadata, fault records, event log, metrics, commands
   * - ``~/.chaos-jungle/cj.db``
     - Bit-flip records for ``StorageCorrupt`` (original byte values for revert)

----

Database schema
----------------

``chaos_jungle.db`` has five tables:

sessions
~~~~~~~~

One row per ``ChaosRunner.start()`` call.

.. code-block:: sql

   CREATE TABLE sessions (
       id          INTEGER PRIMARY KEY AUTOINCREMENT,
       name        TEXT    NOT NULL,
       started_at  TEXT    NOT NULL,   -- ISO-8601 UTC
       stopped_at  TEXT,               -- NULL while still running
       status      TEXT    NOT NULL    -- 'running' | 'stopped' | 'reverted'
   );

faults
~~~~~~

One row per fault within a session.

.. code-block:: sql

   CREATE TABLE faults (
       id          INTEGER PRIMARY KEY AUTOINCREMENT,
       session_id  INTEGER NOT NULL REFERENCES sessions(id),
       kind        TEXT    NOT NULL,   -- e.g. "NetworkDelay", "LLMLatency"
       parameters  TEXT    NOT NULL,   -- JSON dict of fault parameters
       started_at  TEXT    NOT NULL,
       stopped_at  TEXT
   );

Example ``parameters`` values:

.. code-block:: json

   { "delay": "100ms", "jitter": "10ms", "iface": "eth0" }
   { "delay_s": 3.0, "port": 18001, "upstream": "http://127.0.0.1:11434" }
   { "pattern": "*.pdb", "directory": "/data", "interval": "10m" }
   { "cores": 4, "duration_s": 120 }
   { "service": "nginx", "action": "stop", "was_active": true }

events
~~~~~~

Append-only timestamped event log.  Every state change and every command
run on the target is written here.

.. code-block:: sql

   CREATE TABLE events (
       id          INTEGER PRIMARY KEY AUTOINCREMENT,
       session_id  INTEGER NOT NULL REFERENCES sessions(id),
       fault_id    INTEGER REFERENCES faults(id),  -- NULL for session-level
       timestamp   TEXT    NOT NULL,
       message     TEXT    NOT NULL
   );

Typical event sequence::

   Session started: net-delay
   Starting fault: NetworkDelay
   [cmd:OK] tc qdisc add dev eth0 root netem delay 100ms | exit=0
   Fault started: NetworkDelay
   Stopping fault: NetworkDelay
   [cmd:OK] tc qdisc del dev eth0 root | exit=0
   Fault stopped and reverted: NetworkDelay
   Session closed

results
~~~~~~~

Populated by ``runner.record_result()``.  Stores arbitrary JSON metrics
linked to the session.

.. code-block:: sql

   CREATE TABLE results (
       id           INTEGER PRIMARY KEY AUTOINCREMENT,
       session_id   INTEGER NOT NULL REFERENCES sessions(id),
       recorded_at  TEXT    NOT NULL,
       metrics      TEXT    NOT NULL DEFAULT '{}'
   );

.. code-block:: python

   runner.record_result({
       "duration_s":      3.21,
       "success":         1,
       "faithfulness":    0.18,
       "hallucination":   0.91,
       "retries":         3,
       "throughput_mbps": 42.1,
   })

All keys in ``metrics`` become extra columns when you export to CSV.

commands
~~~~~~~~

Captures every command executed via ``target.run()`` / ``target.sudo()``.
Useful for auditing exactly what chaos-jungle did on the target machine.

.. code-block:: sql

   CREATE TABLE commands (
       id           INTEGER PRIMARY KEY AUTOINCREMENT,
       session_id   INTEGER NOT NULL REFERENCES sessions(id),
       cmd          TEXT    NOT NULL,
       stdout       TEXT,
       stderr       TEXT,
       rc           INTEGER,
       executed_at  TEXT    NOT NULL
   );

----

Python API
-----------

.. code-block:: python

   from chaos_jungle.db.session_db import SessionDB

   db = SessionDB()   # opens ~/.chaos-jungle/chaos_jungle.db

   # list all sessions
   for row in db.list_sessions():
       print(row["id"], row["name"], row["status"])

   # export one session as a JSON-serializable dict
   data = db.export_session(session_id=3)
   print(data["session"])   # id, name, started_at, stopped_at, status
   print(data["faults"])    # list of fault dicts
   print(data["events"])    # list of event dicts

   # read metrics stored by record_result()
   for r in db.get_results(session_id=3):
       print(r["metrics"])

   # check if chaos is currently running
   active = db.active_session()
   if active:
       print(f"Session {active['id']} is running: {active['name']}")

Custom database path (e.g. one DB per experiment run):

.. code-block:: python

   from chaos_jungle.db.session_db import SessionDB
   from chaos_jungle import ChaosRunner, Scenario, NetworkDelay, LocalTarget

   db     = SessionDB(path="/tmp/experiment-1.db")
   runner = ChaosRunner(
       Scenario("custom-db", [NetworkDelay("100ms")]),
       LocalTarget(),
       db=db,
   )
   runner.run("5m")

----

Querying directly with SQLite
------------------------------

.. code-block:: bash

   sqlite3 ~/.chaos-jungle/chaos_jungle.db

   -- all sessions
   SELECT id, name, status, started_at, stopped_at FROM sessions;

   -- faults in session 3
   SELECT kind, parameters FROM faults WHERE session_id = 3;

   -- full event log for session 3
   SELECT timestamp, message FROM events WHERE session_id = 3 ORDER BY id;

   -- sessions still marked 'running' (may indicate a crashed run)
   SELECT * FROM sessions WHERE status = 'running';

   -- total chaos time per scenario in seconds
   SELECT name,
          ROUND(SUM((julianday(stopped_at) - julianday(started_at)) * 86400), 1) AS total_s
   FROM sessions
   WHERE stopped_at IS NOT NULL
   GROUP BY name;

----

CLI — session management
--------------------------

.. code-block:: bash

   # show the current running session
   chaos-jungle status

   # list all sessions
   chaos-jungle list

   # export session 3 → JSON file
   chaos-jungle export --session 3

   # export session 3 → CSV with metrics columns flattened
   chaos-jungle export --session 3 --format csv

   # export all sessions → chaos_sessions.csv
   chaos-jungle export --format csv

   # stop and revert the most recent session
   chaos-jungle stop

   # stop a specific session by id
   chaos-jungle stop --session 3

----

Fetching results from a remote machine
----------------------------------------

After a run with ``SSHTarget``, pull the database and logs to your machine:

.. code-block:: bash

   # fetch DB + auto-export to CSV
   chaos-jungle fetch --target ssh://ubuntu@worker1

   # fetch DB + storage log, save to ./experiment-1/
   chaos-jungle fetch --target ssh://ubuntu@worker1 \
       --files "chaos_jungle.db,cj.log" \
       --output-dir ./experiment-1/

Result::

   ./experiment-1/
     chaos_jungle.db       ← full SQLite session DB
     cj.log                ← storage bit-flip log
     chaos_sessions.csv    ← auto-generated CSV with all metrics

CSV column layout::

   session_id, name, status, started_at, stopped_at, duration_s,
   fault_kind, fault_parameters, <keys from record_result()>

In Python:

.. code-block:: python

   from chaos_jungle.targets import SSHTarget

   target = SSHTarget("worker1", user="ubuntu")
   target.connect()
   target.get("~/.chaos-jungle/chaos_jungle.db", "./results/chaos_jungle.db")
   target.disconnect()

----

Export structure (``export_session`` return value)
----------------------------------------------------

.. code-block:: python

   {
       "session": {
           "id": 3, "name": "net-delay",
           "started_at": "2026-06-04T14:00:00+00:00",
           "stopped_at": "2026-06-04T14:10:01+00:00",
           "status": "reverted"
       },
       "faults": [{
           "id": 5, "session_id": 3,
           "kind": "NetworkDelay",
           "parameters": "{\"delay\": \"100ms\", \"jitter\": \"10ms\"}",
           "started_at": "...", "stopped_at": "..."
       }],
       "events": [
           {"id": 9,  "message": "Session started: net-delay", ...},
           {"id": 10, "message": "Starting fault: NetworkDelay", ...},
           {"id": 11, "message": "Fault started: NetworkDelay", ...},
           {"id": 12, "message": "Stopping fault: NetworkDelay", ...},
           {"id": 13, "message": "Fault stopped and reverted: NetworkDelay", ...},
           {"id": 14, "message": "Session closed", ...}
       ],
       "results": [{"metrics": {"duration_s": 3.21, "success": 1}}],
       "commands": [{"cmd": "tc qdisc add ...", "rc": 0, "stdout": ""}]
   }

See also
---------

* :ref:`guide-dashboard` — web UI for browsing sessions and metrics
* :ref:`guide-measurement` — ``runner.measure()`` and quality gates
* :ref:`guide-ssh` — fetching results from remote SSH targets
