.. _guide-safety:

Safety Policy
=============

chaos-jungle operates at the OS and network level.  Without guardrails,
it would be possible to accidentally run destructive faults (disk fill,
process kill, storage corruption) against production targets or sensitive
paths.

:class:`~chaos_jungle.guardrails.SafetyPolicy` provides a second layer
of defence beyond the existing conflict detection — it enforces *what kinds
of faults are permitted* and optionally enables a **dry-run mode** where
nothing is actually injected.

----

Danger levels
-------------

Every fault class has a ``danger_level`` attribute:

.. list-table::
   :header-rows: 1
   :widths: 10 25 65

   * - Level
     - Name
     - Fault types
   * - ``0``
     - safe
     - ``NetworkDelay``, ``NetworkLoss``, ``NetworkCorrupt``, ``LLMLatency``,
       ``LLMRateLimit``, ``LLMTimeout``, ``LLMResponseCorrupt``,
       ``LLMUnavailable``, ``SemanticCorrupt``, all LLM/semantic faults
   * - ``1``
     - moderate
     - ``CPUStress``, ``MemoryStress``, ``IOStress`` —
       consume shared resources, may degrade co-located workloads
   * - ``2``
     - destructive
     - ``DiskFull``, ``StorageCorrupt``, ``ProcessKill``,
       ``ServiceFault``, ``ContainerKill``, ``GPUMemoryPressure`` —
       may cause data loss, service outages, or require manual cleanup

----

SafetyPolicy
------------

.. code-block:: python

   from chaos_jungle.guardrails import SafetyPolicy
   from chaos_jungle import ChaosRunner, Scenario
   from chaos_jungle.faults import NetworkDelay
   from chaos_jungle.targets import LocalTarget

   policy = SafetyPolicy(max_danger=0)   # only safe faults in CI

   runner = ChaosRunner(
       Scenario("ci-test", [NetworkDelay(delay="100ms", iface="lo")]),
       LocalTarget(),
       policy=policy,
   )
   runner.start()   # OK — NetworkDelay is danger_level=0
   runner.stop()

If a forbidden fault is included:

.. code-block:: python

   from chaos_jungle.faults import DiskFull
   from chaos_jungle.guardrails import DangerError

   try:
       runner = ChaosRunner(
           Scenario("bad", [DiskFull("/tmp")]),
           LocalTarget(),
           policy=SafetyPolicy(max_danger=0),  # DiskFull is level 2
       )
       runner.start()   # raises DangerError
   except DangerError as e:
       print(e)

Parameters
~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Parameter
     - Default
     - Description
   * - ``max_danger``
     - ``1``
     - Maximum ``danger_level`` permitted. Faults above this level raise
       :exc:`~chaos_jungle.guardrails.DangerError` before injection.
   * - ``dry_run``
     - ``False``
     - When ``True``, calls :meth:`~chaos_jungle.faults.base.Fault.dry_run`
       instead of :meth:`~chaos_jungle.faults.base.Fault.start` — nothing
       is injected, but everything else (preflight, DB session, events)
       runs normally.
   * - ``allowed_paths``
     - ``[]``
     - Allowlist of absolute path prefixes for path-based faults
       (``DiskFull``, ``StorageCorrupt``). Empty = no restriction.
   * - ``allowed_targets``
     - ``[]``
     - Allowlist of target hostnames/IPs. Empty = no restriction.

----

Dry-run mode
------------

Use ``dry_run=True`` to validate an experiment configuration without
injecting anything:

.. code-block:: python

   from chaos_jungle.guardrails import SafetyPolicy

   policy = SafetyPolicy(dry_run=True)
   runner = ChaosRunner(scenario, LocalTarget(), policy=policy)
   runner.start()
   # Prints:
   # [chaos-jungle] DRY-RUN NetworkDelay({'delay': '200ms', ...}) on LocalTarget — not executed
   runner.stop()

This is useful for:

* Validating experiment YAML/config before running in production.
* CI pipelines that parse and verify scenario definitions without a
  test machine.
* Demonstrating what a scenario does without risk.

----

Path allowlisting
-----------------

Prevent ``DiskFull`` from targeting system paths:

.. code-block:: python

   policy = SafetyPolicy(
       max_danger=2,
       allowed_paths=["/tmp", "/var/tmp", "/data/scratch"],
   )

   # This will run — /tmp is in the allowlist
   runner = ChaosRunner(
       Scenario("s", [DiskFull("/tmp", size_mb=512)]),
       LocalTarget(),
       policy=policy,
   )

   # This will raise DangerError — /etc is not in the allowlist
   runner = ChaosRunner(
       Scenario("s", [DiskFull("/etc", size_mb=1)]),
       LocalTarget(),
       policy=policy,
   )

----

Target allowlisting
-------------------

Restrict experiments to named hosts only:

.. code-block:: python

   from chaos_jungle.targets import SSHTarget

   policy = SafetyPolicy(
       max_danger=2,
       allowed_targets=["chaos-lab-01", "chaos-lab-02"],
   )

   runner = ChaosRunner(
       scenario,
       SSHTarget("chaos-lab-01", user="ubuntu"),
       policy=policy,
   )   # OK

   runner = ChaosRunner(
       scenario,
       SSHTarget("prod-db-01", user="ubuntu"),  # production machine!
       policy=policy,
   )   # raises DangerError

----

Recommended policy presets
---------------------------

.. code-block:: python

   # CI / automated testing — safe faults only
   CI_POLICY = SafetyPolicy(max_danger=0)

   # Development / staging — moderate faults allowed
   DEV_POLICY = SafetyPolicy(max_danger=1)

   # Lab / dedicated chaos node — all faults allowed, restricted to /tmp
   LAB_POLICY = SafetyPolicy(
       max_danger=2,
       allowed_paths=["/tmp", "/var/tmp"],
       allowed_targets=["chaos-lab-01"],
   )

   # Validate config without running anything
   DRY_POLICY = SafetyPolicy(dry_run=True)

----

See also
--------

* :ref:`guide-oracles` — post-run behavioural assertions
* :ref:`guide-traces` — trace event capture and replay
* :ref:`api-guardrails` — full API reference
