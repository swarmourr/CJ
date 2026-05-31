SSH target guide
================

Use ``SSHTarget`` to inject faults on a remote machine over SSH.

Setup
-----

Ensure passwordless SSH access to the target:

.. code-block:: bash

   ssh-copy-id ubuntu@worker1

The target user needs ``sudo`` for privileged commands:

.. code-block:: bash

   # on worker1
   echo "ubuntu ALL=(ALL) NOPASSWD: /sbin/tc, /bin/dd" | sudo tee /etc/sudoers.d/chaos-jungle

Usage
-----

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner, NetworkLoss, SSHTarget

   target = SSHTarget("worker1", user="ubuntu", key="~/.ssh/id_rsa")
   scenario = Scenario("remote-loss", faults=[NetworkLoss("5%")])

   runner = ChaosRunner(scenario, target)
   runner.start()

   # run your workload on another machine or locally
   run_my_pipeline()

   runner.stop()

CLI
---

.. code-block:: bash

   chaos-jungle start --scenario remote-loss --loss 5% \
       --target ssh://ubuntu@worker1

   chaos-jungle stop --target ssh://ubuntu@worker1

Collecting results after a run
--------------------------------

After a remote chaos run, use ``chaos-jungle fetch`` to download the
session database and any log files to your local machine.

.. code-block:: bash

   # fetch DB + auto-export to CSV
   chaos-jungle fetch --target ssh://ubuntu@worker1

   # fetch DB + storage log, save to ./run-1/
   chaos-jungle fetch --target ssh://ubuntu@worker1 \
       --files "chaos_jungle.db,cj.log" \
       --output-dir ./run-1/

The fetched ``chaos_sessions.csv`` contains one row per session+fault
with all metrics from ``runner.record_result()`` as extra columns.

In Python, you can also download individual files explicitly:

.. code-block:: python

   from chaos_jungle.targets import SSHTarget

   target = SSHTarget("worker1", user="ubuntu")
   target.connect()
   target.get("~/.chaos-jungle/chaos_jungle.db", "./run-1/chaos_jungle.db")
   target.get("~/.chaos-jungle/cj.log",          "./run-1/cj.log")
   target.disconnect()

File transfer API
-----------------

``SSHTarget`` provides ``put()`` and ``get()`` for file transfers over SFTP:

.. code-block:: python

   target = SSHTarget("worker1", user="ubuntu")
   target.connect()

   # upload a file to the remote machine
   target.put("/local/path/config.yaml", "/remote/path/config.yaml")

   # download a file from the remote machine
   target.get("/remote/path/results.db", "/local/path/results.db")

   target.disconnect()
