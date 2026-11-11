chaos-jungle
============

Chaos Jungle (CJ) is a Python toolkit for controlled fault injection in
agentic and distributed applications. It can perturb model requests, tool
results, network paths, containers, processes, resources, and stored state.
Experiments are defined in code and can be scoped to a block, a function, a
target, or a coordinated group of targets.

CJ records what was configured and what happened at the injection boundary.
An application-specific success metric, such as code correctness or completed
jobs, remains the experimenter's choice. A failed task alone is not evidence
that an intended fault was experienced.

Get started
-----------

1. :doc:`Install and verify <guides/installation>` the current source.
2. Run the :doc:`quickstart` without an external model or privileged target.
3. Use :ref:`guide-choosing-injection` to choose an interface for your workload.

.. code-block:: python

   from chaos_jungle.inject.intercept import Latency, inject

   with inject(Latency(0.5), urls=["api.example.com"]):
       result = run_agent()

``inject(...)`` intercepts supported ``httpx`` and ``requests`` calls in the
current process and removes the interception on exit. The URL filter above is
illustrative; the application must actually send a matching request for the
fault to be reached. For external processes or role-specific model traffic,
use a managed proxy or an explicit target instead.

Experiment model
----------------

A :doc:`scenario <concepts>` names one or more faults. A target specifies
where a fault acts; a runner manages preflight, activation, observation, and
cleanup. The session record keeps lifecycle events, commands, proxy records,
and available verification measurements.

.. code-block:: text

   configure -> activate -> reach operation -> observe effect -> revert -> check recovery

These stages answer different questions. Activation says CJ installed a fault.
Triggering says the workload reached the selected operation. Manifestation
says the intended effect was observed. Reversion and recovery concern the
environment after the experiment. CJ's exposure verdicts distinguish valid,
untriggered, invalid, and inconclusive interventions; none is a task score.
See :ref:`guide-choosing-injection` and :doc:`guides/measurement` for the
evidence and outcome workflow.

Fault placement
---------------

.. list-table::
   :header-rows: 1
   :widths: 23 35 42

   * - Boundary
     - Examples
     - Evidence to inspect
   * - Model and tool traffic
     - Delay, HTTP error, response mutation, matching tool-result failure
     - Matched request, status, timing, or mutation in proxy/intercept records
   * - Network and resources
     - Packet delay or loss, CPU or memory pressure
     - Target-local rule or process state and measured network or resource effect
   * - Process and stored state
     - Container or service interruption, file or data corruption
     - Observed state transition or before/after value and restoration check

The supported mechanisms are broader than those repeatedly exercised in any
one evaluation. Always verify effects at the boundary being perturbed.

Documentation
-------------

The guides below cover the available interfaces, fault families, targets,
measurement options, and API. Infrastructure faults can require Linux tools,
container access, or elevated permissions on the selected target; installing
the Python package does not grant those permissions.

.. toctree::
   :maxdepth: 2
   :caption: Getting Started

   guides/installation
   quickstart
   guides/choosing-injection
   concepts
   architecture
   examples

.. toctree::
   :maxdepth: 2
   :caption: Agent and Model Faults

   guides/agent-failure-model
   guides/intercept
   guides/llm
   guides/semantic
   guides/conversation
   guides/gateway

.. toctree::
   :maxdepth: 2
   :caption: Tool, Skill, and State Faults

   guides/skill
   guides/state

.. toctree::
   :maxdepth: 2
   :caption: Infrastructure Faults

   guides/network
   guides/process
   guides/resources
   guides/storage
   guides/gpu

.. toctree::
   :maxdepth: 2
   :caption: Targets and Deployment

   guides/local
   guides/ssh
   guides/http
   guides/separate-mode
   guides/registry
   guides/ollama

.. toctree::
   :maxdepth: 2
   :caption: Experiment Design

   guides/strategies
   guides/hypothesis
   guides/measurement
   guides/metrics
   guides/oracles
   guides/judge
   guides/fuzzing
   guides/pytest

.. toctree::
   :maxdepth: 2
   :caption: Operations and Observability

   guides/traces
   guides/dashboard
   guides/data
   guides/exporters
   guides/scheduler
   guides/safety

.. toctree::
   :maxdepth: 2
   :caption: API Reference

   api/scenario
   api/runner
   api/suite
   api/scheduler
   api/faults
   api/targets
   api/decorators
   api/intercept
   api/fuzzing
   api/metrics
   api/hypothesis
   api/judge
   api/conversation
   api/guardrails
   api/preflight
   api/cli
   api/daemon
   api/dashboard

.. toctree::
   :maxdepth: 1
   :caption: Project

   changelog
   publishing
