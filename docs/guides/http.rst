.. _guide-http:

HTTP Target
============

Use ``HTTPTarget`` to control a remote machine over HTTP instead of SSH.
A lightweight daemon (``cj-daemon``) runs on the target and accepts fault
control commands from your machine.

Use this when:

* The target machine is behind a firewall with no SSH access
* You want a persistent daemon instead of per-run SSH connections
* You need to control chaos from a CI runner that has no SSH key

----

Install the daemon on the target
----------------------------------

.. code-block:: bash

   pip install chaos-jungle
   cj-daemon --port 7777 --token mysecret

To run as a systemd service so the daemon starts automatically on boot:

.. code-block:: ini

   # /etc/systemd/system/cj-daemon.service
   [Unit]
   Description=chaos-jungle daemon
   After=network.target

   [Service]
   ExecStart=cj-daemon --port 7777 --token mysecret
   Restart=always
   User=root

   [Install]
   WantedBy=multi-user.target

.. code-block:: bash

   sudo systemctl enable --now cj-daemon

----

Where does each piece run?
---------------------------

.. important::

   ``HTTPTarget`` only sends **fault control commands** to the remote machine.
   Your Python script — including any ``run_my_pipeline()`` call — still runs
   on **your local machine**.

.. mermaid::

   flowchart LR
       subgraph LOCAL_H["Your machine"]
           RH["runner.start()\nrunner.stop()"]
           PH["run_my_pipeline()"]
       end
       subgraph TARGET_H["target:7777"]
           DH["NetworkDelay injected ✓\nNetworkDelay removed ✓"]
       end

       RH -->|"POST /start"| DH
       DH -->|"result"| RH
       PH -.->|"only affected if it talks to target"| TARGET_H

To run a command **on the target** through the daemon:

.. code-block:: bash

   chaos-jungle exec --target http://worker1:7777 \
       --cmd "python3 /home/ubuntu/pipeline.py"

----

Python usage
-------------

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, NetworkDelay, HTTPTarget

   target   = HTTPTarget("http://worker1:7777", token="mysecret")
   scenario = Scenario("http-delay", [NetworkDelay("100ms")])
   runner   = ChaosRunner(scenario, target)

   runner.start()
   run_my_pipeline()   # runs locally; affected if it talks to worker1
   runner.stop()

All fault types work the same as with ``SSHTarget``:

.. code-block:: python

   from chaos_jungle import LLMLatency, ServiceFault, CPUStress

   # LLM fault via daemon
   runner = ChaosRunner(
       Scenario("llm-slow", [LLMLatency(delay_s=3.0, port=18001,
                                        upstream="http://127.0.0.1:11434")]),
       HTTPTarget("http://worker1:7777", token="mysecret"),
   )

   # Service fault via daemon
   runner = ChaosRunner(
       Scenario("svc-stop", [ServiceFault("nginx", action="stop")]),
       HTTPTarget("http://worker1:7777", token="mysecret"),
   )

----

CLI
----

.. code-block:: bash

   # Start a fault
   chaos-jungle start --scenario http-delay --delay 100ms \
       --target http://worker1:7777

   # Stop
   chaos-jungle stop --target http://worker1:7777

   # Check status
   chaos-jungle status --target http://worker1:7777

----

TLS (HTTPS)
------------

Use a reverse proxy (nginx, Caddy) in front of the daemon for HTTPS:

.. code-block:: python

   target = HTTPTarget("https://worker1:7777", tls_verify=True)

   # Disable TLS verification for self-signed certs (dev only)
   target = HTTPTarget("https://worker1:7777", tls_verify=False)

----

Scenario tracking
-----------------

chaos-jungle tracks every scenario automatically.  Use the CLI to observe
status — no extra code needed:

.. code-block:: bash

   cj scenarios list
   cj scenarios watch <uuid>

See :ref:`guide-registry` for details.

----

SSHTarget vs HTTPTarget
------------------------

.. list-table::
   :header-rows: 1
   :widths: 35 30 35

   * - Feature
     - SSHTarget
     - HTTPTarget
   * - Connection
     - SSH (port 22)
     - HTTP (any port)
   * - Auth
     - SSH key / password
     - Bearer token
   * - Daemon required
     - No
     - Yes (``cj-daemon``)
   * - Firewall-friendly
     - No (SSH must be open)
     - Yes (any HTTP port)
   * - ``target.run(cmd)``
     - Yes — executes on remote
     - Via ``chaos-jungle exec`` CLI
   * - File transfer (put/get)
     - Yes (SFTP)
     - Not supported
   * - Remote scenario push
     - ``push_scenario()`` via SSH
     - ``push_scenario()`` via HTTP
   * - Remote scenario watch
     - ``watch()`` polls via SSH
     - ``watch()`` polls via HTTP

See also
---------

* :ref:`guide-ssh` — SSHTarget setup and authentication
* :ref:`guide-local` — LocalTarget for same-machine testing
* :ref:`guide-separate-mode` — start and stop chaos from different processes
* :ref:`guide-registry` — ScenarioRegistry and remote orchestration
