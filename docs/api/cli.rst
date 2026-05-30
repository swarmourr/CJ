CLI
===

.. code-block:: text

   Usage: chaos-jungle [OPTIONS] COMMAND [ARGS]...

     chaos-jungle — inject and control chaos faults on any machine.

   Commands:
     start    Start chaos faults (separate mode — returns immediately)
     stop     Stop and revert the active chaos session
     status   Show the current active session
     list     List all sessions
     export   Export a session to JSON or CSV
     daemon   Start the chaos daemon on this machine (for HTTP target mode)

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

.. code-block:: text

   Usage: chaos-jungle export [OPTIONS]

     Export a session to JSON or CSV.

   Options:
     -i, --session INTEGER    Session id to export  [required]
     -f, --format [json|csv]  Output format (default: json)

Examples::

   chaos-jungle export --session 1
   chaos-jungle export --session 1 --format csv > session1.csv

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
