.. _guide-docker:

Docker Targets
==============

Docker is a *target choice*, not a fault type. Use an explicit container
identifier so infrastructure faults cannot silently move to the host if the
container is unavailable.

.. list-table::
   :header-rows: 1
   :widths: 28 36 36

   * - Target
     - What CJ controls
     - Typical faults
   * - ``DockerTarget(container_id)``
     - Commands and file transfers inside one running container via
       ``docker exec`` and ``docker cp``
     - Network delay/loss and resource pressure within the container
   * - ``DockerContainerControllerTarget(container_id)``
     - A restricted set of Docker lifecycle actions on that exact container
     - ``ContainerKill`` with ``kill``, ``stop``, or ``pause``

The second target acts on the container *object* from the host. It is not a
general-purpose host shell. For a process fault *inside* the container, use
``DockerTarget`` instead. A containerized workload is not a reason to use
``LocalTarget``: that would run infrastructure commands on the CJ host.

Prerequisites and scope
-----------------------

The CJ host needs access to the Docker CLI and daemon. The named experiment
container must exist and be running before ``DockerTarget`` connects. Install
each fault's required tools in that container; for example, network faults
need ``tc`` from ``iproute2`` and a suitable ``NET_ADMIN`` capability. The
target's ``sudo()`` runs as container user 0, but does not grant missing
container capabilities. Use a disposable, resource-limited container and do
not mount the host Docker socket into it. ``DockerTarget`` fails if its named
container is unavailable; it never falls back to host execution.

Example: network delay inside a container
-----------------------------------------

.. code-block:: python

   from chaos_jungle import ChaosRunner, DockerTarget, NetworkDelay, Scenario

   target = DockerTarget("cj-worker")
   fault = NetworkDelay("200ms", iface="eth0")
   runner = ChaosRunner(Scenario("worker-network-delay", [fault]), target)

   runner.start()
   try:
       run_workload()  # application traffic must cross cj-worker's eth0
       # Collect an independent latency or packet-path observation here.
   finally:
       runner.stop()

   report = runner.exposure_report()

The ``tc`` state can verify that the rule was installed. It does not by itself
prove that the workload used the affected path or experienced the requested
delay. For a target-side effect, call ``runner.record_exposure(...)`` while
the runner is active with a measured trigger, effect, and supporting evidence;
otherwise the exposure verdict may be inconclusive. ``runner.stop()`` reverts
the fault and performs the mechanism's recovery check independently of task
success. See :ref:`guide-measurement` for exposure reporting.

Container lifecycle faults
--------------------------

To pause, stop, or kill a disposable container, pair
``ContainerKill("cj-worker", action="pause")`` with
``DockerContainerControllerTarget("cj-worker")``. The identifiers must match.
``pause`` can be unpaused during cleanup; ``kill`` and ``stop`` attempt to
restart a previously running container. Do not use ``action="rm"`` when
restoration is required: removal cannot recreate the original container.
Check the post-fault container state separately from whether the agent
completed its task.

See :ref:`guide-coordinated` for simultaneous faults on multiple containers.
