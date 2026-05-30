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
