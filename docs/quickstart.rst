Quickstart
==========

Installation
------------

**From PyPI:**

.. code-block:: bash

   pip install chaos-jungle

**Directly from GitHub (latest):**

.. code-block:: bash

   pip install git+https://github.com/swarmourr/CJ.git

   # force-reinstall to pick up the latest commit
   pip install --force-reinstall git+https://github.com/swarmourr/CJ.git

   # on systems with externally managed Python (Ubuntu 23+, Debian 12+)
   pip install --break-system-packages git+https://github.com/swarmourr/CJ.git

**With extras:**

.. code-block:: bash

   pip install "chaos-jungle[docs]"   # Sphinx docs
   pip install "chaos-jungle[dev]"    # dev tools

Requirements
~~~~~~~~~~~~

* Python 3.9+
* **macOS or Linux** — LLM / AI faults work on both
* **Linux only** — network, storage, process, and resource faults
* ``sudo`` on the target machine for privileged commands


Choose your starting point
--------------------------

.. list-table::
   :header-rows: 1
   :widths: 40 30 30

   * - What you want to test
     - Target
     - Linux required?
   * - LLM API (latency, rate-limit, corrupt response)
     - ``LocalTarget``
     - No — works on macOS
   * - Semantic fault (RAG poison, entity swap)
     - ``LocalTarget``
     - No — works on macOS
   * - Network delay / loss
     - ``SSHTarget``
     - Yes
   * - Process / service crash
     - ``SSHTarget``
     - Yes
   * - CPU / memory / disk pressure
     - ``SSHTarget``
     - Yes


Example 1 — LLM latency (macOS, no setup)
------------------------------------------

Inject a 3-second delay into every call to a local Ollama model and verify
your application's timeout logic fires correctly.

.. code-block:: python

   import os, time
   os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:11434/v1"
   os.environ["OPENAI_API_KEY"]  = "ollama"

   from chaos_jungle import ChaosRunner, Scenario, LLMLatency, LocalTarget
   import openai

   fault  = LLMLatency(delay_s=3.0, port=18001, upstream="http://127.0.0.1:11434")
   runner = ChaosRunner(Scenario("llm-latency", [fault]), LocalTarget())

   def workload():
       t0 = time.time()
       try:
           resp = openai.OpenAI().chat.completions.create(
               model="qwen2.5:latest",
               messages=[{"role": "user", "content": "What is 2+2?"}],
               timeout=5.0,
           )
           return {"success": 1, "duration_s": round(time.time()-t0, 2)}
       except Exception:
           return {"success": 0, "duration_s": round(time.time()-t0, 2)}

   result = runner.measure(workload, n_baseline=3, n_fault=3)
   print(result.summary())
   # fault_mean("duration_s") should be ≈ baseline + 3 s


Example 2 — semantic quality measurement (macOS, no setup)
-----------------------------------------------------------

Inject entity swaps into LLM context and measure the quality drop using a
local judge model.

.. code-block:: python

   import os
   os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:11434/v1"
   os.environ["OPENAI_API_KEY"]  = "ollama"

   from chaos_jungle import ChaosRunner, Scenario, SemanticCorrupt, LLMJudge, LocalTarget
   import openai

   CONTEXT  = "France is in Western Europe. Its capital is Paris."
   QUESTION = "What is the capital of France?"

   judge  = LLMJudge(model="qwen2.5:latest")
   fault  = SemanticCorrupt(mode="entity_swap", port=18050,
                            upstream="http://127.0.0.1:11434")
   runner = ChaosRunner(Scenario("semantic-qa", [fault]), LocalTarget())

   def workload():
       resp = openai.OpenAI().chat.completions.create(
           model="qwen2.5:latest",
           messages=[
               {"role": "system", "content": "Answer ONLY from the context."},
               {"role": "user",   "content": f"Context: {CONTEXT}\nQuestion: {QUESTION}"},
           ],
       )
       return {
           "question": QUESTION,
           "context":  CONTEXT,
           "response": resp.choices[0].message.content or "",
       }

   result = runner.measure(workload, n_baseline=3, n_fault=3, evaluator=judge)
   print(result.summary())
   print("Quality gate passed:", result.passed_quality(min_faithfulness=0.70))


Example 3 — network delay on a remote machine (Linux, SSH)
-----------------------------------------------------------

Measure the latency impact of a 200 ms delay on an HTTP service running on
a remote Ubuntu machine.

.. code-block:: python

   import time, requests
   from chaos_jungle import ChaosRunner, Scenario, NetworkDelay, SSHTarget

   target = SSHTarget("192.168.1.100", user="ubuntu")
   fault  = NetworkDelay("200ms", jitter="20ms")
   runner = ChaosRunner(Scenario("net-delay", [fault]), target)

   def workload():
       t0 = time.time()
       r  = requests.get("http://192.168.1.100:8080/api/ping", timeout=5.0)
       return {
           "duration_s": round(time.time()-t0, 2),
           "success":    int(r.status_code == 200),
       }

   result = runner.measure(workload, n_baseline=5, n_fault=5)
   print(result.summary())
   # fault_mean("duration_s") ≈ baseline_mean + 0.2 s


Example 4 — service crash (Linux, SSH)
---------------------------------------

Stop nginx, measure health-check failures, then restore it automatically.

.. code-block:: python

   import time, requests
   from chaos_jungle import ChaosRunner, Scenario, ServiceFault, SSHTarget

   target = SSHTarget("192.168.1.100", user="ubuntu")
   fault  = ServiceFault("nginx", action="stop")
   runner = ChaosRunner(Scenario("nginx-stop", [fault]), target)

   def workload():
       try:
           r = requests.get("http://192.168.1.100/health", timeout=2.0)
           return {"success": int(r.status_code == 200)}
       except Exception:
           return {"success": 0}

   result = runner.measure(workload, n_baseline=5, n_fault=5)
   print(result.summary())
   # fault_mean("success") should be 0.0 (nginx is down)
   # After stop() nginx is automatically restarted


Using the decorator
-------------------

.. code-block:: python

   from chaos_jungle.decorators import chaos
   from chaos_jungle import NetworkDelay, StorageCorrupt

   @chaos(NetworkDelay("100ms"), StorageCorrupt("*.pdb", "/data"))
   def my_experiment():
       run_my_pipeline()

   my_experiment()   # chaos starts, function runs, chaos stops automatically


Using the context manager
--------------------------

.. code-block:: python

   from chaos_jungle.decorators import chaos_session
   from chaos_jungle import NetworkLoss

   with chaos_session(NetworkLoss("5%"), scenario_name="loss-test") as session:
       run_my_pipeline()
       print(session.export("json"))


CLI — separate mode
--------------------

Start chaos independently from your workload:

.. code-block:: bash

   # Terminal 1 — start chaos
   chaos-jungle start --scenario net-delay --delay 100ms --jitter 10ms

   # Terminal 2 — run anything
   bash my-workflow.sh

   # Terminal 1 — stop chaos
   chaos-jungle stop

Remote machine (SSH):

.. code-block:: bash

   chaos-jungle start --scenario net-delay --delay 100ms \
       --target ssh://ubuntu@worker1

Remote machine (HTTP daemon):

.. code-block:: bash

   # On the remote machine
   cj-daemon --port 7777 --token mysecret

   # From your machine
   chaos-jungle start --scenario net-delay --delay 100ms \
       --target http://worker1:7777


What next?
----------

* **Guides** — see the fault-specific guides for detailed parameters,
  what to observe, and pass/fail criteria
* **Concepts** — understand the Fault / Target / Scenario / Runner model
* **API Reference** — full class and method documentation
