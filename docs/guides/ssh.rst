.. _guide-ssh:

SSH Target
==========

Use ``SSHTarget`` to inject faults on a remote machine over SSH.

Authentication
--------------

Authentication is attempted in this order (same as the OpenSSH client):

1. **Explicit key file** — if you pass ``key="~/.ssh/id_ed25519"`` and the file exists.
2. **SSH agent** — if ``ssh-agent`` is running and has a key loaded (``ssh-add``).
3. **Default key search** — Paramiko tries ``~/.ssh/id_rsa``, ``~/.ssh/id_ecdsa``, ``~/.ssh/id_ed25519`` automatically.
4. **Password** — if ``password=`` is provided.

**Key-based (recommended) — agent or default key auto-detected:**

.. code-block:: python

   target = SSHTarget("worker1", user="ubuntu")

**Explicit key file:**

.. code-block:: python

   target = SSHTarget("worker1", user="ubuntu", key="~/.ssh/id_ed25519")

**Encrypted key with passphrase:**

.. code-block:: python

   target = SSHTarget("worker1", user="ubuntu",
                      key="~/.ssh/id_rsa", password="my-passphrase")

**Password-only (no key, useful for cloud VMs with password auth):**

.. code-block:: python

   target = SSHTarget("worker1", user="ubuntu",
                      password="hunter2",
                      allow_agent=False, look_for_keys=False)

**Custom port:**

.. code-block:: python

   target = SSHTarget("worker1", user="ubuntu", port=2222)

Setup — passwordless sudo
--------------------------

The target user needs ``sudo`` rights for ``tc`` (network faults) and
``dd``/``filefrag`` (storage faults):

.. code-block:: bash

   # on the target machine
   echo "ubuntu ALL=(ALL) NOPASSWD: /sbin/tc, /usr/sbin/tc, /bin/dd, /usr/sbin/filefrag" \
       | sudo tee /etc/sudoers.d/chaos-jungle

   sudo chmod 440 /etc/sudoers.d/chaos-jungle

Copy your SSH key to the remote machine if not already done:

.. code-block:: bash

   ssh-copy-id ubuntu@worker1
   # or for a specific key:
   ssh-copy-id -i ~/.ssh/id_ed25519.pub ubuntu@worker1

Where does each piece run?
--------------------------

.. important::

   ``SSHTarget`` runs **fault commands** on ``worker1`` over SSH.
   Your Python script — including any ``run_my_pipeline()`` call — still
   runs on **your local machine**.

   To run a command on ``worker1`` itself, use ``target.run()``.

.. mermaid::

   flowchart LR
       subgraph LOCAL["Your machine"]
           RS["runner.start()\nrunner.stop()"]
           PL["run_my_pipeline()"]
       end
       subgraph WORKER["worker1 (SSH)"]
           NL["NetworkLoss injected ✓\nNetworkLoss removed ✓"]
       end

       RS -->|"SSH cmd"| NL
       PL -.->|"affected only if it sends traffic to/from worker1"| WORKER

To run the pipeline **on worker1**:

.. code-block:: python

   runner.start()
   target.run("python3 /home/ubuntu/my_pipeline.py")   # executes ON worker1
   runner.stop()

Usage
-----

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner, NetworkLoss, SSHTarget

   target = SSHTarget("worker1", user="ubuntu")   # auto-detect key/agent
   scenario = Scenario("remote-loss", faults=[NetworkLoss("5%")])

   runner = ChaosRunner(scenario, target)
   runner.start()

   # Option A — pipeline runs locally, affected if it talks to worker1
   run_my_pipeline()

   # Option B — pipeline runs ON worker1
   # target.run("python3 /home/ubuntu/my_pipeline.py")

   runner.stop()

CLI
---

.. code-block:: bash

   # key/agent auto-detected
   chaos-jungle start --scenario remote-loss --loss 5% \
       --target ssh://ubuntu@worker1

   # custom port
   chaos-jungle start --scenario remote-loss --loss 5% \
       --target ssh://ubuntu@worker1:2222

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

----

Remote scenario orchestration
------------------------------

When chaos-jungle is installed on both machines, you can push a complete
scenario to the remote machine and watch its status without keeping an SSH
connection open for the full experiment duration.

**How it works:**

1. ``push_scenario(s)`` — serializes the scenario and registers it on the
   remote machine under the same UUID.  Two brief SSH connections: one to
   register, one returns.
2. ``run_scenario(id)`` — starts the scenario in the background.  SSH exec
   returns immediately.
3. ``registry.watch(id, target=t)`` — polls the remote registry every few
   seconds via brief SSH connections until status reaches ``done`` or
   ``failed``.

.. code-block:: python

   from chaos_jungle import Scenario, NetworkDelay, ScenarioRegistry
   from chaos_jungle.targets import SSHTarget

   scenario = Scenario("wan-test", [NetworkDelay("200ms", jitter="20ms")])
   target   = SSHTarget("worker1", user="ubuntu")

   target.push_scenario(scenario)      # register on both machines
   target.run_scenario(scenario.id)    # fire and forget

   # watch from local — no open SSH connection held
   entry = ScenarioRegistry().watch(scenario.id, target=target, poll_interval=5)
   print("session_id:", entry["session_id"])

   # fetch results when done
   target.get("~/.chaos-jungle/chaos_jungle.db", "./worker1_results.db")

**What each machine registers:**

.. list-table::
   :header-rows: 1
   :widths: 20 20 20 20 20

   * - Machine
     - ``type``
     - ``target_ip``
     - ``source_ip``
     - ``status``
   * - Local
     - ``ssh``
     - ``192.168.1.100``
     - *(empty)*
     - tracks remote progress
   * - Remote
     - ``local``
     - *(empty)*
     - local IP
     - updated by ChaosRunner

**Monitor from the CLI:**

.. code-block:: bash

   # watch until done
   cj scenarios watch <uuid> --target ssh://ubuntu@worker1

   # check status once (returns JSON)
   cj scenarios status <uuid> --target ssh://ubuntu@worker1 --json

See :ref:`guide-registry` for the full ScenarioRegistry reference.
