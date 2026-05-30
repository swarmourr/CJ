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
