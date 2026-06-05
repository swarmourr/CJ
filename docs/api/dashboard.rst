.. _api-dashboard:

Dashboard
=========

The dashboard is a self-contained FastAPI web UI that reads from the local
SQLite database and shows sessions, faults, events, metrics, and which system
tools are installed.

----

Launching
---------

.. code-block:: bash

   # CLI
   chaos-jungle dashboard
   chaos-jungle dashboard --host 0.0.0.0 --port 9090

.. code-block:: python

   from chaos_jungle.dashboard import run

   run(host="127.0.0.1", port=8050)   # blocks until Ctrl-C

   # Non-blocking — embed in a test script
   import threading
   t = threading.Thread(target=run, kwargs={"port": 8050}, daemon=True)
   t.start()

Open **http://localhost:8050** in your browser.  The dashboard auto-refreshes
every 6 seconds.

----

REST API endpoints
------------------

The dashboard also exposes a JSON API used by the frontend:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Endpoint
     - Description
   * - ``GET /api/sessions``
     - List all sessions with fault summaries
   * - ``GET /api/session/{id}``
     - Full session detail: events, faults, results, commands
   * - ``GET /api/analysis/{id}``
     - Cross-reference session with live ``tc`` rules and ``cj.db`` records
   * - ``GET /api/cj-records``
     - All bit-flip records from ``cj.db``
   * - ``GET /api/logs``
     - List log files under ``~/.chaos-jungle/``
   * - ``GET /api/log-content``
     - Last N lines of a log file
   * - ``GET /api/system``
     - Which system tools (``tc``, ``stress-ng``, ``docker``, etc.) are installed

----

Python API reference
--------------------

.. automodule:: chaos_jungle.dashboard
   :members:
   :undoc-members: False
