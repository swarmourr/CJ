.. _guide-coordinated:

Coordinated Injection
=====================

Use an injection group when an experiment requires faults on multiple
explicit targets to be activated together. Each ``Injection`` binds one
fault to one target. A group prepares its members, activates them according
to a synchronization policy, and records per-member activation and cleanup.
An ordinary ``Scenario`` fault still uses the ``ChaosRunner`` target; a
scenario group instead carries its own targets.

.. list-table::
   :header-rows: 1
   :widths: 28 35 37

   * - Interface
     - Observation window
     - Use when
   * - ``InjectionGroupRunner``
     - Caller runs the workload between ``start()`` and ``stop()``
     - The application controls when and how long it runs
   * - ``Scenario(groups=[...])`` with ``ChaosRunner``
     - Caller runs the workload during the runner session
     - Group evidence should be stored with the CJ session
   * - ``DistributedCoordinator(DistributedScenario(...))``
     - ``run()`` blocks for the configured observation duration
     - A fixed-duration distributed intervention is sufficient

For a group, ``best_effort`` starts members as soon as possible, ``barrier``
waits until all are ready, and ``scheduled`` activates at a shared future
timestamp. The default ``maximum_skew_ms=100`` is a configurable tolerance,
not a universal guarantee. Activation skew is the latest minus earliest
recorded activation time. It measures coordination, *not* time from command
submission to CPU pressure or proof of an application-level effect.

Example: two container targets
------------------------------

The following requires two disposable running containers, each with
``iproute2`` and the capability needed for ``tc``:

.. code-block:: python

   from chaos_jungle import (
       DockerTarget, Injection, InjectionGroup, InjectionGroupRunner,
       NetworkDelay,
   )

   group = InjectionGroup(
       name="two-workers",
       synchronization="barrier",
       maximum_skew_ms=100,
       safety_maximum_duration=30,
       injections=[
           Injection("worker-a-delay", DockerTarget("cj-worker-a"),
                     NetworkDelay("100ms", iface="eth0")),
           Injection("worker-b-delay", DockerTarget("cj-worker-b"),
                     NetworkDelay("100ms", iface="eth0")),
       ],
   )
   runner = InjectionGroupRunner(group)

   runner.start()
   try:
       run_workload()
       # Record each reached path and measured effect before stop().
   finally:
       activation = runner.stop()

   print(activation.verdict, activation.activation_skew_ms)

``runner.record_member_exposure(injection_id, ...)`` attaches an observed
trigger and effect to one active member. With a ``ChaosRunner`` scenario,
use ``runner.record_group_exposure(group_name, injection_id, ...)`` instead.
Include the measurement in ``evidence``; neither an installed ``tc`` rule nor
a low skew alone proves that a workload request traversed the path. The
group's activation verdict and the session's exposure verdict answer
different questions. See :ref:`guide-measurement`.

Failure and recovery
--------------------

By default, a failed preparation cancels an atomic group and an activation
failure rolls back active members. The default skew policy marks an
over-limit group invalid but leaves its faults active until normal cleanup;
``on_skew_violation="rollback_all"`` requests immediate rollback instead.
The watchdog bounds active duration if the caller does not stop the group.
Inspect each member's activation, effect, and recovery evidence: a group
cannot be treated as a fully exposed intervention merely because its
members were started close together.
