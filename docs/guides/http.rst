HTTP daemon guide
=================

Use the chaos daemon + ``HTTPTarget`` when you want to control a remote
machine over HTTP instead of SSH. The daemon runs as root on the target.

Install daemon on target machine
---------------------------------

.. code-block:: bash

   pip install chaos-jungle
   cj-daemon --port 7777 --token mysecret

To run as a systemd service:

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

Where does each piece run?
--------------------------

.. important::

   ``HTTPTarget`` only sends **fault control commands** to ``worker1``.
   Your Python script — including any ``run_my_pipeline()`` call — runs on
   **your local machine**, not on ``worker1``.

.. code-block:: text

   Your machine                        worker1:7777
   ─────────────────────────────       ────────────────────────────
   runner.start()  ── POST /start ──►  NetworkDelay injected ✓
   run_my_pipeline()                   (no involvement)
     │
     └─ only affected if it talks
        to worker1 (HTTP, TCP, etc.)
   runner.stop()   ── POST /stop  ──►  NetworkDelay removed ✓

Use ``HTTPTarget`` when your workload runs **somewhere else** but
communicates with ``worker1`` — for example, a data-transfer pipeline that
reads from or writes to ``worker1``.

Control from your machine
--------------------------

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner, NetworkDelay, HTTPTarget

   target = HTTPTarget("http://worker1:7777", token="mysecret")
   scenario = Scenario("http-delay", faults=[NetworkDelay("100ms")])

   runner = ChaosRunner(scenario, target)
   runner.start()

   # This runs on YOUR machine.
   # It is affected by the fault only if it communicates with worker1.
   run_my_pipeline()

   runner.stop()

To run a command **on worker1** through the daemon, use the CLI:

.. code-block:: bash

   chaos-jungle exec --target http://worker1:7777 \
       --cmd "python3 /home/ubuntu/pipeline.py"

CLI
---

.. code-block:: bash

   chaos-jungle start --scenario http-delay --delay 100ms \
       --target http://worker1:7777

   chaos-jungle stop --target http://worker1:7777

TLS
---

For HTTPS, use a reverse proxy (nginx, caddy) in front of the daemon:

.. code-block:: python

   target = HTTPTarget("https://worker1:7777", tls_verify=True)
