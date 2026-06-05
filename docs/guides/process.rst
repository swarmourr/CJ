.. _guide-process:

Process, Service & Container Faults
=====================================

These faults operate at the **OS and runtime layer** — killing processes,
stopping or crashing systemd services, and killing Docker containers.  They
cover the most common Chaos Monkey-style scenarios (instance death, service
crash, container restart) without requiring cloud credentials or terminating
a whole machine.

All three faults target an ``SSHTarget`` (or a ``LocalTarget`` with
appropriate permissions).  ``sudo`` is required for ``ServiceFault``.

Available faults
----------------

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Fault
     - Description
   * - ``ProcessKill``
     - Kill one or more OS processes matching a name / command pattern
   * - ``ServiceFault``
     - Stop, restart, kill, or mask a systemd service; restores on ``stop()``
   * - ``ContainerKill``
     - Kill, stop, pause, or remove a Docker container; restores on ``stop()``


ProcessKill
-----------

Uses ``pkill -f`` to match against the full command line.  The PIDs that
existed before the kill are captured and stored in ``killed_pids`` for
reporting.

.. note::

   ``ProcessKill`` is **irreversible** — ``stop()`` is a no-op because an
   arbitrary process cannot be automatically restarted.  Start the process
   in your workload's cleanup step if needed.

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, ProcessKill
   from chaos_jungle.targets import SSHTarget

   target = SSHTarget("10.0.0.5", user="ubuntu")

   # Kill all gunicorn workers
   fault = ProcessKill("gunicorn")

   runner = ChaosRunner(Scenario("kill-workers", [fault]), target)
   runner.start()
   # measure how long it takes for the process supervisor to restart workers
   runner.stop()   # no-op for ProcessKill; process is gone

Other signals::

   ProcessKill("my_agent.py", signal="TERM")   # graceful shutdown
   ProcessKill("celery", signal="STOP")         # pause without killing (SIGSTOP)
   ProcessKill("uvicorn", signal="HUP")         # reload config

**What to observe:**

* Does the process supervisor (systemd, supervisord, Kubernetes) restart the
  process automatically?
* How long does recovery take?
* Are in-flight requests handled gracefully (drain vs. crash)?

sudo requirement
^^^^^^^^^^^^^^^^

``ProcessKill`` uses ``pkill`` via the regular user — **no sudo needed**
unless the target process runs as root.


ServiceFault
------------

Stops, restarts, kills, or masks a systemd unit.  On ``stop()`` the service
is restored to its original state.

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Action
     - Effect
   * - ``stop``
     - ``systemctl stop <service>``; restarted on ``stop()``
   * - ``restart``
     - ``systemctl restart <service>``; no rollback (already restarted)
   * - ``kill``
     - ``systemctl kill <service>`` (sends SIGKILL to all processes in the unit)
   * - ``mask``
     - ``systemctl mask <service>``; prevents auto-restart; unmasked + started on ``stop()``

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, ServiceFault
   from chaos_jungle.targets import SSHTarget

   target = SSHTarget("10.0.0.5", user="ubuntu")

   # Stop nginx, measure how long health checks fail
   fault = ServiceFault("nginx", action="stop")
   runner = ChaosRunner(Scenario("nginx-stop", [fault]), target)
   runner.start()
   # run health-check workload
   runner.stop()   # nginx is restarted automatically

   # Mask postgresql to prevent restart loops
   fault2 = ServiceFault("postgresql", action="mask")
   runner2 = ChaosRunner(Scenario("pg-mask", [fault2]), target)
   runner2.start()
   # test agent behaviour when DB is permanently unavailable
   runner2.stop()   # postgresql is unmasked and started

**What to observe:**

* Does the application detect the service is down and surface a useful error?
* Does it reconnect automatically once the service is back?
* Does masking (preventing restart) trigger a circuit-breaker or fallback?

sudo requirement
^^^^^^^^^^^^^^^^

``ServiceFault`` runs ``systemctl`` via ``sudo``.  Ensure the SSH user has
passwordless sudo for ``systemctl``::

   ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl


ContainerKill
-------------

Kills, stops, pauses, or removes a Docker container.  On ``stop()`` the
container is started again (except for ``rm``).

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Action
     - Effect
   * - ``kill``
     - ``docker kill <container>`` (SIGKILL); restarted on ``stop()``
   * - ``stop``
     - ``docker stop <container>`` (graceful SIGTERM + timeout); restarted on ``stop()``
   * - ``pause``
     - ``docker pause <container>`` (freezes cgroups); unpaused on ``stop()``
   * - ``rm``
     - ``docker rm -f <container>``; **irreversible** — not restarted

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, ContainerKill
   from chaos_jungle.targets import SSHTarget

   target = SSHTarget("10.0.0.5", user="ubuntu")

   # Kill the API container — simulates container crash / OOM kill
   fault = ContainerKill("my-api", action="kill")
   runner = ChaosRunner(Scenario("container-crash", [fault]), target)
   runner.start()
   # measure restart time and error handling during the gap
   runner.stop()   # docker start my-api

   # Pause a Redis sidecar — simulates a slow/unresponsive dependency
   fault2 = ContainerKill("redis-cache", action="pause")
   runner2 = ChaosRunner(Scenario("redis-pause", [fault2]), target)
   runner2.start()
   # run workload — Redis calls will block, not fail
   runner2.stop()   # docker unpause redis-cache

**What to observe:**

* ``kill`` — does Kubernetes / Docker Compose restart the container?
* ``pause`` — does the application timeout on the blocked Redis call, or hang?
* ``stop`` — graceful shutdown: are in-flight requests drained?

No sudo requirement
^^^^^^^^^^^^^^^^^^^

``ContainerKill`` runs ``docker`` commands as the SSH user.  Add the user
to the ``docker`` group on the target::

   sudo usermod -aG docker ubuntu


Combined scenario — realistic service outage
---------------------------------------------

Combine process/service/container faults with network faults to simulate a
realistic node degradation:

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, ServiceFault, NetworkLoss
   from chaos_jungle.targets import SSHTarget

   target = SSHTarget("10.0.0.5", user="ubuntu")

   # Stop the database AND introduce packet loss — degraded node
   scenario = Scenario("degraded-node", [
       ServiceFault("postgresql", action="stop"),
       NetworkLoss("5%"),
   ])
   runner = ChaosRunner(scenario, target)
   runner.start()
   # run application workload
   runner.stop()   # postgresql restarted, loss removed

Measuring recovery time
-----------------------

.. code-block:: python

   import time
   from chaos_jungle import ChaosRunner, Scenario, ServiceFault
   from chaos_jungle.targets import SSHTarget

   target = SSHTarget("10.0.0.5", user="ubuntu")

   def workload():
       t0 = time.time()
       rc, out, _ = target.run(
           "curl -s -o /dev/null -w '%{http_code}' http://localhost/health"
       )
       return {
           "duration_s": round(time.time() - t0, 2),
           "success":    int(out.strip() == "200"),
       }

   runner = ChaosRunner(
       Scenario("service-stop", [ServiceFault("nginx", action="stop")]),
       target,
   )
   result = runner.measure(workload, n_baseline=5, n_fault=5)
   print(result.summary())

See also
--------

* :ref:`guide-resources` — CPU / memory / disk resource exhaustion
* :ref:`guide-ssh` — SSHTarget setup and authentication
