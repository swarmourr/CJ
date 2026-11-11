.. _guide-choosing-injection:

Choosing an Injection Interface
===============================

Chaos Jungle provides several interfaces because fault scope varies.  A
``Scenario`` is useful orchestration, but it is not automatically the best way
to inject every fault.  Start with the smallest interface that reaches the real
operation under test.

Recommended order
-----------------

.. list-table::
   :header-rows: 1
   :widths: 24 32 44

   * - Interface
     - Best for
     - Choose it when
   * - ``inject(...)``
     - Same-process HTTP and LLM calls
     - The workload uses ``httpx`` or ``requests`` in the current Python
       process.  This is the recommended starting point.
   * - ``@chaos(...)`` or ``chaos_session(...)``
     - One function, test, or explicit block
     - A CJ ``Fault`` should wrap a clearly bounded workload with automatic
       cleanup, without manually constructing a scenario.
   * - ``ChaosRunner.measure(...)``
     - Local baseline-versus-fault measurement
     - You need repeated measurements, deltas, and stored lifecycle evidence.
   * - ``Scenario`` + ``ChaosRunner``
     - Managed or reproducible experiments
     - You need multiple faults, scheduling, a remote target, a separate
       process, explicit lifecycle control, or campaign-grade evidence.
   * - Injection groups or distributed scenarios
     - Multiple real targets
     - Activation and rollback must be coordinated across hosts, containers,
       or agent services.

The decision is about **scope**, not syntax.  Do not introduce a scenario only
to make a short example look complete.

Same-process injection: preferred first
---------------------------------------

Use :func:`~chaos_jungle.inject.intercept.inject` when the agent and supported
HTTP client run in the current Python process.  There is no proxy process, port,
or target to configure.

.. code-block:: python

   from chaos_jungle import Latency, inject

   with inject(Latency(0.5), measure=True) as evidence:
       result = agent.run("Summarize the incident")

   print(evidence.http_calls, evidence.duration_s)

Use filters to keep the fault precise:

.. code-block:: python

   from chaos_jungle import ToolMutate, inject

   with inject(
       ToolMutate(mode="wrong_type"),
       only_tool="search",
       only_model="minimax-m2",
   ):
       agent.run("Find the latest deployment record")

``inject()`` cannot affect traffic from another process or container.  If the
workload is outside the current process, use a managed proxy or inject inside
that workload process.

Function and test scope
-----------------------

The decorator and context-manager interfaces hide scenario construction while
preserving CJ's managed start/stop lifecycle.

.. code-block:: python

   from chaos_jungle import LLMLatency, chaos

   @chaos(LLMLatency(delay_s=0.5, port=18021))
   def test_agent_under_latency():
       return run_agent()

Use ``chaos_session(...)`` when assertions or setup must remain outside the
fault window.  Use ``chaos_measure(...)`` when a decorated workload returns
metrics CJ should record.

When a scenario is the right choice
-----------------------------------

Use an explicit scenario when its orchestration is part of the requirement:

.. code-block:: python

   from chaos_jungle import ChaosRunner, LLMLatency, LocalTarget, Scenario

   fault = LLMLatency(
       delay_s=0.5,
       port=18022,
       upstream="https://provider.example/v1",
   )
   runner = ChaosRunner(Scenario("managed-llm-latency", [fault]), LocalTarget())

   runner.start()
   try:
       result = run_external_workload()
   finally:
       runner.stop()

This form is appropriate when CJ must manage a proxy, retain session evidence,
coordinate faults, operate on another target, or expose activation and recovery
verification.  Construct model clients after ``runner.start()`` when an SDK
captures its base URL at initialization.

Choose the target independently
-------------------------------

The injection interface controls *scope*.  The target controls *where commands
execute*.

.. list-table::
   :header-rows: 1
   :widths: 28 32 40

   * - Subject
     - Target
     - Rule
   * - Current host
     - ``LocalTarget``
     - Use only when host impact is intentional.
   * - Running experiment container
     - ``DockerTarget(container_id)``
     - Network, resource, and process commands execute inside that container.
   * - Container object
     - ``DockerContainerControllerTarget(container_id)``
     - Use for container pause, stop, or kill on one exact container.
   * - Authorized remote host
     - ``SSHTarget``
     - Bound the remote blast radius and credentials explicitly.
   * - Deliberate CJ HTTP worker
     - ``HTTPTarget``
     - Use only with a restricted control endpoint.

Running Docker locally does not make a container workload a ``LocalTarget``.
Never fall back to the host when a container target is unavailable.
See :ref:`guide-docker` for prerequisites and the distinction between
commands inside a container and container lifecycle actions. For faults
across multiple targets, see :ref:`guide-coordinated`.

Reachability before severity
----------------------------

Before increasing fault severity, prove that a healthy trace reaches the
injection point:

* an LLM client actually uses the proxy or intercepted transport;
* a tool fault observes a real matching tool-result message;
* a role selector receives run, role, and step metadata;
* container traffic traverses the modified interface;
* an application reads the checkpoint after state corruption.

An active fault that never sees its matching operation is **untriggered**.  It
is not evidence that the agent is resilient or that CJ failed to inject.

Evidence and cleanup
--------------------

Keep these lifecycle facts separate:

``configured`` → ``activated`` → ``triggered`` → ``manifested`` →
``reverted`` → ``recovered``

A successful method call is not sufficient evidence of manifestation.  A
successful ``stop()`` is not sufficient evidence of recovery.  Use proxy
records for LLM and tool faults, ``tc`` plus measured network behavior for
network faults, resource measurements for stress faults, process/container
state for termination faults, and checksums plus application reads for state
faults.

Bundled AI-assistant skill
--------------------------

The repository includes ``skills/chaos-jungle-injection`` for AI coding
assistants that support skill instructions.  It encodes the same interface,
target, routing, safety, and evidence decisions with additional practical
recipes.  The skill assists experiment construction; it does not grant
permission to disrupt a host or replace runtime verification.

**Download:** :download:`chaos-jungle-injection-skill.zip
<../_downloads/chaos-jungle-injection-skill.zip>`

Install it for Codex:

.. code-block:: bash

   unzip chaos-jungle-injection-skill.zip -d ~/.codex/skills

Then invoke it explicitly with ``$chaos-jungle-injection``.  The skill remains
ordinary Markdown and YAML, so other assistants with compatible skill or
instruction mechanisms can import the same folder.

Next steps
----------

* :ref:`guide-intercept` for same-process LLM and HTTP behaviors.
* :ref:`guide-llm` for managed proxy faults.
* :doc:`local`, :doc:`ssh`, and :doc:`http` for target configuration.
* :ref:`guide-measurement` for baseline and fault measurements.
* :doc:`network` and :doc:`resources` for infrastructure faults.
