Data Storage and Output
=======================

chaos-jungle writes to **two SQLite databases** and never deletes either
automatically. Both live in ``~/.chaos-jungle/`` by default.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - File
     - What it stores
   * - ``~/.chaos-jungle/chaos_jungle.db``
     - Session metadata, fault records, timestamped event log *(new — managed by chaos-jungle)*
   * - ``~/.chaos-jungle/cj.db``
     - Bit-flip records: which byte was changed and what the original value was *(original chaos-jungle DB — never touched by this framework)*

----

chaos_jungle.db — session database
------------------------------------

Created automatically on first run. Contains three tables.

sessions
~~~~~~~~

One row per ``ChaosRunner.start()`` call.

.. code-block:: sql

   CREATE TABLE sessions (
       id          INTEGER PRIMARY KEY AUTOINCREMENT,
       name        TEXT    NOT NULL,           -- scenario name
       started_at  TEXT    NOT NULL,           -- ISO-8601 UTC timestamp
       stopped_at  TEXT,                       -- NULL while still running
       status      TEXT    NOT NULL            -- 'running' | 'stopped' | 'reverted'
   );

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Column
     - Type
     - Notes
   * - ``id``
     - INTEGER
     - Auto-incrementing primary key.
   * - ``name``
     - TEXT
     - The ``scenario_name`` you passed to ``Scenario()``.
   * - ``started_at``
     - TEXT
     - UTC ISO-8601 string, e.g. ``2025-05-29T14:00:00.123456+00:00``.
   * - ``stopped_at``
     - TEXT
     - ``NULL`` while chaos is still running. Set by ``runner.stop()``.
   * - ``status``
     - TEXT
     - ``"running"`` → ``"reverted"`` on clean stop. ``"stopped"`` if stop ran without full revert.

faults
~~~~~~

One row per fault *within* a session. A session with three faults gets
three rows.

.. code-block:: sql

   CREATE TABLE faults (
       id          INTEGER PRIMARY KEY AUTOINCREMENT,
       session_id  INTEGER NOT NULL REFERENCES sessions(id),
       kind        TEXT    NOT NULL,           -- fault class name
       parameters  TEXT    NOT NULL,           -- JSON dict of fault parameters
       started_at  TEXT    NOT NULL,
       stopped_at  TEXT
   );

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Column
     - Type
     - Notes
   * - ``kind``
     - TEXT
     - Class name: ``"NetworkDelay"``, ``"StorageCorrupt"``, etc.
   * - ``parameters``
     - TEXT
     - JSON object. For ``NetworkDelay("100ms", jitter="10ms")`` this is ``{"delay": "100ms", "jitter": "10ms", "iface": null}``.

Example ``parameters`` values by fault type:

.. code-block:: json

   // NetworkDelay
   {"delay": "100ms", "jitter": "10ms", "iface": null}

   // NetworkLoss
   {"rate": "5%", "iface": null}

   // NetworkCorrupt
   {"rate": "1%", "iface": null}

   // NetworkDuplicate
   {"rate": "0.5%", "iface": null}

   // StorageCorrupt
   {"pattern": "*.pdb", "directory": "/scratch/data", "interval": "10m", "recursive": true}

   // SilentNetworkCorrupt
   {"rate": 5000, "hook": "tc", "iface": null}

events
~~~~~~

Append-only timestamped log. Every state change — start, stop, error —
is written here.

.. code-block:: sql

   CREATE TABLE events (
       id          INTEGER PRIMARY KEY AUTOINCREMENT,
       session_id  INTEGER NOT NULL REFERENCES sessions(id),
       fault_id    INTEGER REFERENCES faults(id),  -- NULL for session-level events
       timestamp   TEXT    NOT NULL,
       message     TEXT    NOT NULL
   );

Typical event sequence for one session with one fault:

.. code-block:: text

   Session started: net-delay
   Starting fault: NetworkDelay
   Fault started: NetworkDelay
   Stopping fault: NetworkDelay
   Fault stopped and reverted: NetworkDelay
   Session closed

----

StorageCorrupt defaults and options
--------------------------------------

.. list-table::
   :header-rows: 1
   :widths: 25 15 15 45

   * - Parameter
     - Type
     - Default
     - Description
   * - ``pattern``
     - str
     - *(required)*
     - Glob pattern for files to corrupt, e.g. ``"*.pdb"``, ``"*.dat"``.
   * - ``directory``
     - str
     - *(required)*
     - Absolute path to the directory to watch.
   * - ``interval``
     - str
     - ``"10m"``
     - How often to flip bits. Accepts ``"5m"``, ``"1h"``, ``"30s"``.
   * - ``recursive``
     - bool
     - ``True``
     - Search ``directory`` recursively for matching files.
   * - ``cj_storage_path``
     - str
     - ``~/chaos-jungle/storage/cj_storage.py``
     - Path to ``cj_storage.py`` on the *target* machine.

``StorageCorrupt`` uses a **crontab** on the target machine to schedule
periodic bit-flips. Each corrupted byte is recorded in ``cj.db`` before
being flipped, so ``runner.stop()`` can restore every file exactly.

cj.db — bit-flip records
~~~~~~~~~~~~~~~~~~~~~~~~~~

Managed by the original ``cj_storage.py`` script. chaos-jungle never reads
or writes to it directly. The schema (as created by cj_storage) is:

.. code-block:: sql

   CREATE TABLE corrupt (
       id          INTEGER PRIMARY KEY AUTOINCREMENT,
       filepath    TEXT NOT NULL,      -- absolute path to the corrupted file
       offset      INTEGER NOT NULL,   -- byte offset that was flipped
       original    BLOB NOT NULL,      -- original byte value (1 byte)
       timestamp   TEXT NOT NULL       -- when the corruption was injected
   );

When ``runner.stop()`` calls ``cj_storage.py --revert``, each row in
``corrupt`` is used to write the original byte back with ``dd``.

----

Querying the database directly
---------------------------------

The SQLite file can be queried with any SQLite client.

.. code-block:: bash

   sqlite3 ~/.chaos-jungle/chaos_jungle.db

   -- all sessions
   SELECT id, name, status, started_at, stopped_at FROM sessions;

   -- faults in session 3
   SELECT kind, parameters, started_at, stopped_at
   FROM faults WHERE session_id = 3;

   -- full event log for session 3
   SELECT timestamp, message FROM events WHERE session_id = 3 ORDER BY id;

   -- sessions still marked 'running' (may indicate a crashed run)
   SELECT * FROM sessions WHERE status = 'running';

   -- total chaos time per scenario (seconds)
   SELECT name,
          SUM(
            (julianday(stopped_at) - julianday(started_at)) * 86400
          ) AS total_s
   FROM sessions
   WHERE stopped_at IS NOT NULL
   GROUP BY name;

----

Python API — reading session data
------------------------------------

.. code-block:: python

   from chaos_jungle.db.session_db import SessionDB

   db = SessionDB()   # opens ~/.chaos-jungle/chaos_jungle.db

   # list all sessions
   for row in db.list_sessions():
       print(row["id"], row["name"], row["status"])

   # export one session as a plain dict (JSON-serializable)
   data = db.export_session(session_id=3)

   print(data["session"])   # dict with id, name, started_at, stopped_at, status
   print(data["faults"])    # list of fault dicts
   print(data["events"])    # list of event dicts

   # check if chaos is currently running
   active = db.active_session()
   if active:
       print(f"Session {active['id']} is running: {active['name']}")

Export structure (``export_session`` return value):

.. code-block:: python

   {
       "session": {
           "id": 3,
           "name": "net-delay",
           "started_at": "2025-05-29T14:00:00.000000+00:00",
           "stopped_at": "2025-05-29T14:10:01.234567+00:00",
           "status": "reverted"
       },
       "faults": [
           {
               "id": 5,
               "session_id": 3,
               "kind": "NetworkDelay",
               "parameters": "{\"delay\": \"100ms\", \"jitter\": \"10ms\", \"iface\": null}",
               "started_at": "2025-05-29T14:00:00.456789+00:00",
               "stopped_at": "2025-05-29T14:10:00.987654+00:00"
           }
       ],
       "events": [
           {"id": 9,  "session_id": 3, "fault_id": null, "timestamp": "...", "message": "Session started: net-delay"},
           {"id": 10, "session_id": 3, "fault_id": 5,    "timestamp": "...", "message": "Starting fault: NetworkDelay"},
           {"id": 11, "session_id": 3, "fault_id": 5,    "timestamp": "...", "message": "Fault started: NetworkDelay"},
           {"id": 12, "session_id": 3, "fault_id": 5,    "timestamp": "...", "message": "Stopping fault: NetworkDelay"},
           {"id": 13, "session_id": 3, "fault_id": 5,    "timestamp": "...", "message": "Fault stopped and reverted: NetworkDelay"},
           {"id": 14, "session_id": 3, "fault_id": null, "timestamp": "...", "message": "Session closed"}
       ]
   }

----

CLI — session management
--------------------------

.. code-block:: bash

   # show current running session
   chaos-jungle status

   # list all sessions (all time)
   chaos-jungle list

   # export session 3 as JSON
   chaos-jungle export --session 3

   # export as CSV (events only)
   chaos-jungle export --session 3 --format csv

   # stop and revert the most recent session
   chaos-jungle stop

   # stop a specific session by id
   chaos-jungle stop --session 3

----

Custom database path
---------------------

If you need multiple independent databases (e.g. one per experiment run):

.. code-block:: python

   from chaos_jungle.db.session_db import SessionDB
   from chaos_jungle import Scenario, ChaosRunner
   from chaos_jungle.faults import NetworkDelay
   from chaos_jungle.targets import LocalTarget

   db = SessionDB(path="/tmp/my-experiment.db")

   runner = ChaosRunner(
       Scenario("custom-db", [NetworkDelay("100ms")]),
       LocalTarget(),
       db=db,
   )
   runner.run("5m")

   data = db.export_session(runner._session_id)
