Separate mode guide
===================

In separate mode, chaos and your workload are completely independent.
You start chaos, run anything you want, then stop chaos.

CLI
---

.. code-block:: bash

   # start chaos (returns immediately)
   chaos-jungle start --scenario net-delay \
       --delay 100ms --jitter 10ms

   # run anything — chaos is ON
   bash my-workflow.sh
   kubectl apply -f job.yml
   python my-app.py

   # stop chaos
   chaos-jungle stop

   # check what happened
   chaos-jungle list
   chaos-jungle export --session 1

Python API
----------

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner, NetworkDelay, LocalTarget

   # Process / script 1 — start chaos
   runner = ChaosRunner(
       Scenario("net-delay", [NetworkDelay("100ms")]),
       LocalTarget()
   )
   runner.start()
   # returns immediately — chaos is now ON

   # Process / script 2 — stop chaos (from a different script)
   stopper = ChaosRunner.attach()
   stopper.stop()

How it works
------------

``start()`` writes the active session to ``~/.chaos-jungle/chaos_jungle.db``
with status ``running``.

``ChaosRunner.attach()`` reads the most recent running session from the
same database and returns a runner bound to that session.

``stop()`` on the attached runner stops all faults and marks the session
as ``reverted``.
