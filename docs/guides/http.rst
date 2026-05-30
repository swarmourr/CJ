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

Control from your machine
--------------------------

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner, NetworkDelay, HTTPTarget

   target = HTTPTarget("http://worker1:7777", token="mysecret")
   scenario = Scenario("http-delay", faults=[NetworkDelay("100ms")])

   runner = ChaosRunner(scenario, target)
   runner.start()
   run_my_pipeline()
   runner.stop()

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
