.. _api-daemon:

Daemon
======

``cj-daemon`` is a lightweight FastAPI service that runs on a remote machine
and accepts fault control commands from your local machine via ``HTTPTarget``.

----

Starting the daemon
-------------------

.. code-block:: bash

   # Install and start
   pip install chaos-jungle
   cj-daemon --port 7777 --token mysecret

   # Or via environment variable
   export CJ_DAEMON_TOKEN=mysecret
   cj-daemon --port 7777

Available flags:

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Flag
     - Default
     - Description
   * - ``--port``
     - ``7777``
     - TCP port to listen on
   * - ``--host``
     - ``0.0.0.0``
     - Bind address
   * - ``--token``
     - *(none)*
     - Bearer token for authentication (also via ``CJ_DAEMON_TOKEN``)

----

HTTP endpoints
--------------

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Method + path
     - Auth required
     - Description
   * - ``GET /health``
     - No
     - Health check — returns ``{"status": "ok"}``
   * - ``POST /exec``
     - Yes
     - Run a shell command on the target machine
   * - ``POST /files/upload``
     - Yes
     - Upload a file to the target
   * - ``GET /files/download``
     - Yes
     - Download a file from the target

Authentication uses the ``Authorization: Bearer <token>`` header.  If no
token is configured the daemon accepts all requests.

----

Python API reference
--------------------

.. automodule:: chaos_jungle.daemon
   :members:
   :undoc-members: False
