CLI
===

.. code-block:: text

   Usage: chaos-jungle [OPTIONS] COMMAND [ARGS]...

     chaos-jungle — inject and control chaos faults on any machine.

   Commands:
     start      Start chaos faults (separate mode — returns immediately)
     stop       Stop and revert the active chaos session
     status     Show the current active session
     list       List all sessions
     export     Export session(s) to a portable JSON or CSV file
     fetch      Fetch result files from a remote SSH host
     scenarios  Inspect and watch scenario registry entries
     dashboard  Open the experiment tracking dashboard
     suite      Run an ExperimentSuite from a YAML config file
     daemon     Start the chaos daemon on this machine (for HTTP target mode)

----

start
-----

.. code-block:: text

   Usage: chaos-jungle start [OPTIONS]

     Start chaos faults. Returns immediately (separate mode).

   Options:
     -s, --scenario TEXT           Scenario name stored in DB  [required]
     -t, --target TEXT             Target: ssh://user@host  http://host:port  (empty=local)
     --delay TEXT                  NetworkDelay, e.g. 100ms
     --jitter TEXT                 Jitter for delay, e.g. 10ms
     --loss TEXT                   NetworkLoss rate, e.g. 5%
     --corrupt TEXT                NetworkCorrupt rate, e.g. 1%
     --duplicate TEXT              NetworkDuplicate rate, e.g. 0.5%
     --storage-pattern TEXT        StorageCorrupt file pattern, e.g. '*.pdb'
     --storage-dir TEXT            StorageCorrupt directory
     --storage-interval TEXT       StorageCorrupt crontab interval. Default: 10m
     --auto-install                Auto-install missing dependencies via apt-get

Examples::

   # local machine — network delay
   chaos-jungle start --scenario net-delay --delay 100ms --jitter 10ms

   # remote via SSH
   chaos-jungle start --scenario net-delay --delay 100ms \
       --target ssh://ubuntu@worker1

   # remote via HTTP daemon
   chaos-jungle start --scenario net-delay --delay 100ms \
       --target http://worker1:7777

   # storage + network combined
   chaos-jungle start --scenario full-chaos \
       --delay 100ms --loss 5% \
       --storage-pattern "*.pdb" --storage-dir /data

   # auto-install missing deps
   chaos-jungle start --scenario net-delay --delay 100ms --auto-install

----

stop
----

.. code-block:: text

   Usage: chaos-jungle stop [OPTIONS]

     Stop and revert the active chaos session.

   Options:
     -i, --session INTEGER    Session id (default: most recent running session)
     -t, --target TEXT        Target (must match where chaos was started)

Examples::

   chaos-jungle stop
   chaos-jungle stop --session 3
   chaos-jungle stop --target ssh://ubuntu@worker1

----

status
------

.. code-block:: text

   Usage: chaos-jungle status

     Show the current active session.

Example::

   $ chaos-jungle status
   Session 2: net-delay  status=running  started=2026-05-29T10:00:01+00:00

----

list
----

.. code-block:: text

   Usage: chaos-jungle list

     List all sessions.

Example::

   $ chaos-jungle list
     ID  NAME                            STATUS      STARTED
   ----------------------------------------------------------------------
      1  baseline                        reverted    2026-05-29T09:00:00
      2  net-delay                       running     2026-05-29T10:00:01

----

export
------

Writes session data to a named file on disk. CSV includes all metrics
recorded via ``runner.record_result()`` as flattened columns alongside
session and fault metadata — the same columns used by the original
RENCI ``parse_logs.py``.

.. code-block:: text

   Usage: chaos-jungle export [OPTIONS]

     Export session(s) to a portable JSON or CSV file.

   Options:
     -i, --session INTEGER       Session id to export (default: all sessions)
     -f, --format [json|csv]     Output format. Default: json
     -o, --output PATH           Output file path. Default: auto-named

Examples::

   # single session → auto-named JSON
   chaos-jungle export --session 3

   # single session → auto-named CSV (includes metrics columns)
   chaos-jungle export --session 3 --format csv

   # all sessions → chaos_sessions.csv
   chaos-jungle export --format csv

   # custom path
   chaos-jungle export --session 3 --format json --output run3.json

CSV columns::

   session_id, name, status, started_at, stopped_at, duration_s,
   fault_kind, fault_parameters, <...metrics from record_result()>

----

fetch
-----

Downloads files from a remote SSH host (``~/.chaos-jungle/`` by default)
to a local directory via SFTP. After downloading, automatically reads the
fetched ``chaos_jungle.db`` and writes ``chaos_sessions.csv`` alongside
it — so results are portable without copying the full SQLite file.

.. code-block:: text

   Usage: chaos-jungle fetch [OPTIONS]

     Fetch result files from a remote SSH target.

   Options:
     -t, --target TEXT           SSH target: ssh://user@host  [required]
     -o, --output-dir TEXT       Local directory to save files. Default: ./chaos-results
     --remote-dir TEXT           Remote directory to fetch from. Default: ~/.chaos-jungle
     -f, --files TEXT            Comma-separated filenames to fetch. Default: chaos_jungle.db
     --export-csv / --no-export-csv
                                 Auto-export fetched DB to CSV. Default: yes

Examples::

   # fetch DB and auto-export to CSV (default)
   chaos-jungle fetch --target ssh://ubuntu@10.0.0.5

   # fetch DB + cj.log, save to ./results/
   chaos-jungle fetch --target ssh://ubuntu@10.0.0.5 \
       --files "chaos_jungle.db,cj.log" --output-dir ./results/

   # fetch without auto CSV
   chaos-jungle fetch --target ssh://ubuntu@10.0.0.5 --no-export-csv

----

scenarios
---------

Read-only commands for inspecting the :ref:`guide-registry`.
These never start or stop anything — they only report status.

.. code-block:: text

   Usage: chaos-jungle scenarios [OPTIONS] COMMAND [ARGS]...

     Inspect and watch scenario registry entries.

   Commands:
     list    List all scenarios in the local registry.
     status  Check status of a scenario (local or remote).
     watch   Watch one or more scenarios until they finish.

**scenarios list**

.. code-block:: text

   Usage: chaos-jungle scenarios list [OPTIONS]

   Options:
     -s, --status TEXT   Filter: pending|running|done|failed
     -t, --type TEXT     Filter: local|ssh|http
     --json              Output as JSON array

Examples::

   cj scenarios list
   cj scenarios list --status running
   cj scenarios list --type ssh --json

**scenarios status**

.. code-block:: text

   Usage: chaos-jungle scenarios status [OPTIONS] SCENARIO_ID

   Options:
     -t, --target TEXT   Remote target: ssh://user@host  http://host:port
     --json              Output as JSON

Examples::

   cj scenarios status a3f7c2d1-4eee-4a3f-9b47-1807c5fc0eaf
   cj scenarios status a3f7c2d1 --json
   cj scenarios status a3f7c2d1 --target ssh://ubuntu@worker1
   cj scenarios status a3f7c2d1 --target http://worker1:7777 --json

Example JSON output::

   {"id": "a3f7c2d1-...", "name": "wan-test", "type": "ssh",
    "target_ip": "192.168.1.100", "status": "done", "session_id": 42, ...}

**scenarios watch**

Polls until all specified scenarios reach ``done`` or ``failed``.

.. code-block:: text

   Usage: chaos-jungle scenarios watch [OPTIONS] SCENARIO_IDS...

   Options:
     -t, --target TEXT     Remote target (applied to all IDs)
     --interval FLOAT      Poll interval in seconds. Default: 5
     --timeout FLOAT       Max wait in seconds. Default: 600

Examples::

   # watch a local scenario
   cj scenarios watch a3f7c2d1

   # watch multiple scenarios
   cj scenarios watch a3f7c2d1 b8e1f3a2

   # watch a remote scenario via SSH
   cj scenarios watch a3f7c2d1 --target ssh://ubuntu@worker1

   # watch a remote scenario via HTTP daemon
   cj scenarios watch a3f7c2d1 --target http://worker1:7777

   # custom poll interval and timeout
   cj scenarios watch a3f7c2d1 --interval 10 --timeout 1200

Sample output::

   [14:23:01] a3f7c2d1  wan-test    → running
   [14:23:06] a3f7c2d1  wan-test    → running
   [14:23:11] a3f7c2d1  wan-test    → done
   all done

----

dashboard
---------

.. code-block:: text

   Usage: chaos-jungle dashboard [OPTIONS]

     Open the experiment tracking dashboard in your browser.

   Options:
     --host TEXT      Bind address. Default: 127.0.0.1
     --port INTEGER   Port. Default: 8050

Examples::

   chaos-jungle dashboard
   chaos-jungle dashboard --host 0.0.0.0 --port 9000

----

daemon
------

.. code-block:: text

   Usage: chaos-jungle daemon [OPTIONS]

     Start the chaos daemon on this machine (for HTTP target mode).

   Options:
     --host TEXT      Bind address (default: 0.0.0.0)
     --port INTEGER   Port (default: 7777)
     --token TEXT     Bearer token for auth (optional)

Examples::

   chaos-jungle daemon --port 7777
   chaos-jungle daemon --port 7777 --token mysecret
