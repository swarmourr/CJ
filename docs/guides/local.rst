Local machine guide
===================

Use ``LocalTarget`` when chaos-jungle and your workload run on the same machine.

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner, NetworkDelay, LocalTarget

   scenario = Scenario("local-test", faults=[NetworkDelay("50ms")])
   runner = ChaosRunner(scenario, LocalTarget())
   runner.start()

   import subprocess
   subprocess.run(["ping", "-c", "5", "8.8.8.8"])

   runner.stop()

Or with the decorator:

.. code-block:: python

   from chaos_jungle.decorators import chaos
   from chaos_jungle import NetworkDelay

   @chaos(NetworkDelay("50ms"))
   def test_with_delay():
       import subprocess
       subprocess.run(["ping", "-c", "5", "8.8.8.8"])

   test_with_delay()

Requirements
------------

* ``sudo`` access for ``tc qdisc`` commands
* ``iproute2`` installed (``apt-get install iproute2``)

Choosing a target
-----------------

.. list-table::
   :header-rows: 1
   :widths: 20 25 25 30

   * - Target
     - Fault runs on
     - Pipeline runs on
     - When to use
   * - ``LocalTarget()``
     - your machine
     - your machine
     - Single-machine testing, CI/CD
   * - ``SSHTarget("worker1")``
     - worker1 (via SSH)
     - your machine (or worker1 via ``target.run()``)
     - Remote node, no daemon needed
   * - ``HTTPTarget("worker1:7777")``
     - worker1 (via HTTP daemon)
     - your machine
     - Remote node with daemon, firewall-friendly

.. note::

   In all three cases, ``run_my_pipeline()`` written in your Python script
   runs on **your local machine**.  To run a command on the remote node use
   ``target.run("...")`` (SSHTarget) or the ``chaos-jungle exec`` CLI
   (HTTPTarget).
