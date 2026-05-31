Quickstart
==========

Installation
------------

**From PyPI:**

.. code-block:: bash

   pip install chaos-jungle

**Directly from GitHub (latest):**

.. code-block:: bash

   pip install git+https://github.com/swarmourr/CJ.git

   # force-reinstall to pick up the latest commit
   pip install --force-reinstall git+https://github.com/swarmourr/CJ.git

   # on systems with externally managed Python (Ubuntu 23+, Debian 12+)
   pip install --break-system-packages git+https://github.com/swarmourr/CJ.git

   # or with sudo
   sudo pip install git+https://github.com/swarmourr/CJ.git --break-system-packages

**With extras:**

.. code-block:: bash

   # docs
   pip install "chaos-jungle[docs]"

   # dev
   pip install "chaos-jungle[dev]"

Requirements
~~~~~~~~~~~~

* Python 3.9+
* Linux on the **target** machine (for ``tc`` and ``dd``)
* ``sudo`` access on the target for privileged commands

First example — local machine
------------------------------

Inject 100 ms network delay on your local machine while running a command:

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner, NetworkDelay, LocalTarget

   scenario = Scenario("local-delay", faults=[NetworkDelay("100ms")])
   runner = ChaosRunner(scenario, LocalTarget())

   runner.start()
   # your code here
   runner.stop()

Using the decorator
-------------------

.. code-block:: python

   from chaos_jungle.decorators import chaos
   from chaos_jungle import NetworkDelay, StorageCorrupt

   @chaos(NetworkDelay("100ms"), StorageCorrupt("*.pdb", "/data"))
   def my_experiment():
       run_my_pipeline()

   my_experiment()   # chaos starts, function runs, chaos stops automatically

Using the context manager
--------------------------

.. code-block:: python

   from chaos_jungle.decorators import chaos_session
   from chaos_jungle import NetworkLoss

   with chaos_session(NetworkLoss("5%"), scenario_name="loss-test") as session:
       run_my_pipeline()
       print(session.export("json"))

Measure style — auto-record results
-------------------------------------

``@chaos_measure`` runs the function under chaos **and** automatically
saves its return dict as workflow metrics linked to the session. The
metrics appear in the dashboard and in exported CSV files.

.. code-block:: python

   from chaos_jungle.decorators import chaos_measure
   from chaos_jungle import NetworkDelay

   @chaos_measure(NetworkDelay("100ms"), scenario_name="E1")
   def run_experiment():
       run_my_pipeline()
       # return a dict → auto-stored as results
       return {
           "files_transferred": 120,
           "retries":             3,
           "throughput_mbps":   42.1,
       }

   summary = run_experiment()
   print(summary["duration_s"], "s of chaos")
   print(summary["fn_result"])   # the dict above

Capture stdout as well:

.. code-block:: python

   @chaos_measure(NetworkDelay("100ms"), capture_output=True)
   def run_experiment():
       ...

   summary = run_experiment()
   print(summary["captured_output"])   # everything printed during the run

CLI — separate mode
--------------------

Start chaos independently from your workload:

.. code-block:: bash

   # Terminal 1 — start chaos
   chaos-jungle start --scenario net-delay --delay 100ms --jitter 10ms

   # Terminal 2 — run anything
   bash my-workflow.sh

   # Terminal 1 — stop chaos
   chaos-jungle stop

Remote machine (SSH)
---------------------

.. code-block:: bash

   chaos-jungle start --scenario net-delay --delay 100ms \
       --target ssh://ubuntu@worker1

Remote machine (HTTP daemon)
-----------------------------

On the remote machine:

.. code-block:: bash

   cj-daemon --port 7777 --token mysecret

On your machine:

.. code-block:: bash

   chaos-jungle start --scenario net-delay --delay 100ms \
       --target http://worker1:7777

Or in Python:

.. code-block:: python

   from chaos_jungle import HTTPTarget, NetworkDelay, Scenario, ChaosRunner

   target = HTTPTarget("http://worker1:7777", token="mysecret")
   runner = ChaosRunner(Scenario("net-delay", [NetworkDelay("100ms")]), target)
   runner.start()
