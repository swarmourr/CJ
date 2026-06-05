.. _guide-local:

Local Target
=============

Use ``LocalTarget`` when chaos-jungle and your workload run on the **same
machine**.  No SSH key or daemon is needed.

LLM / AI faults work on any OS with ``LocalTarget`` — no Linux or ``sudo``
required.  Infrastructure faults (network, storage, process, resources) require
Linux and ``sudo``.

----

LLM fault — macOS, no setup
-----------------------------

The most common use case on macOS.  Inject faults into a local Ollama model
without any Linux machine:

.. code-block:: python

   import os, time, openai
   os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:11434/v1"
   os.environ["OPENAI_API_KEY"]  = "ollama"

   from chaos_jungle import ChaosRunner, Scenario, LLMLatency, LocalTarget

   fault  = LLMLatency(delay_s=3.0, port=18001, upstream="http://127.0.0.1:11434")
   runner = ChaosRunner(Scenario("llm-latency", [fault]), LocalTarget())

   def workload():
       t0 = time.time()
       openai.OpenAI().chat.completions.create(
           model="qwen2.5:latest",
           messages=[{"role": "user", "content": "What is 2+2?"}],
           timeout=10.0,
       )
       return {"duration_s": round(time.time()-t0, 2), "success": 1}

   result = runner.measure(workload, n_baseline=3, n_fault=3)
   print(result.summary())

----

Network fault — Linux only
----------------------------

Requires ``sudo`` and ``iproute2``.  Use on a Linux machine or inside a Linux CI
container:

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, NetworkDelay, LocalTarget

   runner = ChaosRunner(
       Scenario("local-delay", [NetworkDelay("100ms", jitter="10ms")]),
       LocalTarget(),
   )
   runner.start()

   import subprocess
   subprocess.run(["ping", "-c", "5", "8.8.8.8"])

   runner.stop()

Requirements for network faults::

   sudo apt-get install -y iproute2          # Ubuntu / Debian
   # the SSH user also needs passwordless sudo for tc:
   echo "ubuntu ALL=(ALL) NOPASSWD: /sbin/tc, /usr/sbin/tc" \
       | sudo tee /etc/sudoers.d/chaos-jungle

----

Decorator style
----------------

.. code-block:: python

   from chaos_jungle.decorators import chaos
   from chaos_jungle import LLMLatency

   @chaos(LLMLatency(delay_s=3.0, port=18001, upstream="http://127.0.0.1:11434"))
   def test_with_slow_llm():
       response = my_agent.run("Summarise this document.")
       assert len(response) > 0

   test_with_slow_llm()   # chaos on → test → chaos off (always)

----

Choosing a target
------------------

.. list-table::
   :header-rows: 1
   :widths: 20 25 25 30

   * - Target
     - Fault runs on
     - Your script runs on
     - When to use
   * - ``LocalTarget()``
     - same machine
     - same machine
     - LLM faults on macOS; Linux CI containers
   * - ``SSHTarget("worker1")``
     - remote machine (SSH)
     - your machine
     - Infrastructure faults on a remote Linux node
   * - ``HTTPTarget("worker1:7777")``
     - remote machine (HTTP daemon)
     - your machine
     - Firewall-restricted nodes; no SSH key available

.. note::

   In all three cases, Python code written in your script runs **locally**.
   To run a command on a remote node use ``target.run("...")`` (SSHTarget)
   or the ``chaos-jungle exec`` CLI (HTTPTarget).

See also
---------

* :ref:`guide-ssh` — SSHTarget setup and authentication
* :ref:`guide-http` — HTTPTarget and daemon setup
* :ref:`guide-llm` — full LLM fault parameter reference
* :ref:`guide-ollama` — testing with a local Ollama model
