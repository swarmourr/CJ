.. _guide-separate-mode:

Separate Mode
=============

In separate mode, chaos and your workload run **independently**.  You start
the fault injection, run any command or script you want without modifying
it, then stop chaos when you're done.

This is the most flexible mode — no code changes required in your
application.

When to use separate mode
--------------------------

* Your workload is a shell script, binary, or third-party tool you cannot
  modify
* You want to inject faults into a long-running service (not a single
  function call)
* You want to start and stop chaos from different terminals or scripts
* You are running a manual test and want full control over timing


CLI — quick start
------------------

.. code-block:: bash

   # Terminal 1 — start chaos (returns immediately)
   chaos-jungle start --scenario net-delay --delay 100ms --jitter 10ms

   # Terminal 2 — run your workload under chaos (unchanged)
   bash my-workflow.sh
   # or: python my_app.py
   # or: kubectl apply -f job.yml

   # Terminal 1 — stop chaos when done
   chaos-jungle stop

   # Inspect what happened
   chaos-jungle list
   chaos-jungle export --session 1 --format json


CLI — all fault types
----------------------

**Network faults (Linux, requires sudo):**

.. code-block:: bash

   # 200 ms delay
   chaos-jungle start --scenario net-delay --delay 200ms

   # 5 % packet loss
   chaos-jungle start --scenario net-loss --loss 5%

   # Remote machine via SSH
   chaos-jungle start --scenario remote-delay --delay 100ms \
       --target ssh://ubuntu@worker1

   chaos-jungle stop --target ssh://ubuntu@worker1

**LLM faults (macOS, no sudo):**

.. code-block:: bash

   # Slow LLM API — every call is delayed 3 s
   chaos-jungle start --scenario llm-slow --llm-latency 3.0

   # Rate limit — return 429 after 5 calls
   chaos-jungle start --scenario llm-ratelimit --llm-rate-limit 5

   # Full outage — every call returns 503
   chaos-jungle start --scenario llm-outage --llm-unavailable

   # Now run your AI application normally — it will see the degraded API
   python my_agent.py

   chaos-jungle stop

**Process / service faults (Linux, SSH):**

.. code-block:: bash

   # Stop nginx on a remote machine
   chaos-jungle start --scenario svc-stop \
       --service nginx --service-action stop \
       --target ssh://ubuntu@worker1

   # Run your health-check script
   bash check_health.sh

   # Restore nginx
   chaos-jungle stop --target ssh://ubuntu@worker1

**Resource exhaustion (Linux, SSH):**

.. code-block:: bash

   # Saturate 4 CPU cores for 5 minutes
   chaos-jungle start --scenario cpu-stress \
       --cpu-cores 4 --cpu-duration 300 \
       --target ssh://ubuntu@worker1

   # Run your workload under CPU pressure
   python my_inference_job.py

   chaos-jungle stop --target ssh://ubuntu@worker1


Python API
-----------

**Single process — start then stop later:**

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, NetworkDelay, LocalTarget

   runner = ChaosRunner(
       Scenario("net-delay", [NetworkDelay("100ms")]),
       LocalTarget(),
   )
   runner.start()   # returns immediately — chaos is ON

   # ... run your workload ...

   runner.stop()    # chaos OFF — faults reverted

**Two separate scripts — start in one, stop in another:**

.. code-block:: python

   # script_a.py — start chaos
   from chaos_jungle import ChaosRunner, Scenario, NetworkLoss, LocalTarget

   runner = ChaosRunner(
       Scenario("loss-test", [NetworkLoss("5%")]),
       LocalTarget(),
   )
   runner.start()
   print("Chaos started. Run your workload, then run script_b.py to stop.")

.. code-block:: python

   # script_b.py — attach to the running session and stop it
   from chaos_jungle import ChaosRunner

   stopper = ChaosRunner.attach()   # finds the most recent running session
   stopper.stop()
   print("Chaos stopped and reverted.")

**LLM fault in separate mode:**

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, LLMLatency, LocalTarget

   runner = ChaosRunner(
       Scenario("llm-slow", [LLMLatency(delay_s=3.0, port=18001,
                                        upstream="http://127.0.0.1:11434")]),
       LocalTarget(),
   )
   runner.start()

   # Your AI app is now running through a slow proxy — no code changes needed
   import subprocess
   subprocess.run(["python", "my_agent.py"])

   runner.stop()

**SSH target — separate mode on a remote machine:**

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, CPUStress, SSHTarget

   target = SSHTarget("10.0.0.5", user="ubuntu")
   runner = ChaosRunner(
       Scenario("cpu-pressure", [CPUStress(cores=4, duration_s=300)]),
       target,
   )
   runner.start()

   # Run any remote command under CPU pressure
   target.run("python3 /app/inference_job.py")

   runner.stop()


How it works
------------

.. mermaid::

   flowchart TD
       START_S["runner.start()"]
       DB_S["writes session to\n~/.chaos-jungle/chaos_jungle.db\nstatus=running"]
       INJ_S["injects fault on target\ntc rule / proxy process / pkill / dd"]
       RET_S["returns immediately"]

       ATTACH_S["ChaosRunner.attach()"]
       READS_S["reads most recent session\nwith status=running from DB\nreturns runner bound to that session"]

       STOP_S["stopper.stop()"]
       REM_S["removes fault from target\ntc del / kill proxy / systemctl start"]
       MARK_S["marks session as\nstatus=reverted in DB"]

       START_S --> DB_S
       START_S --> INJ_S
       START_S --> RET_S

       ATTACH_S --> READS_S

       STOP_S --> REM_S
       STOP_S --> MARK_S

Checking the status
--------------------

.. code-block:: bash

   # List all sessions (most recent first)
   chaos-jungle list

   # Show events for a specific session
   chaos-jungle export --session 3 --format json

.. code-block:: python

   from chaos_jungle import ChaosRunner

   runner = ChaosRunner.attach()
   print(runner.session_id)     # active session ID
   print(runner.export("json")) # full event log


Duration-based automatic stop
-------------------------------

Let chaos stop itself after a fixed time — no second terminal needed:

.. code-block:: bash

   # Stop automatically after 10 minutes
   chaos-jungle start --scenario net-delay --delay 100ms --duration 10m

.. code-block:: python

   runner.start(duration="10m")   # returns immediately; stops after 10 min

   # blocking version — waits until duration expires
   runner.run("10m")


Recording results
------------------

Attach metrics to the session for later export:

.. code-block:: python

   runner.start()
   # ... workload ...
   runner.record_result({
       "jobs_completed": 120,
       "jobs_failed":      3,
       "retries":          7,
   })
   runner.stop()

.. code-block:: bash

   # Export to CSV (includes the recorded metrics as columns)
   chaos-jungle export --format csv

See also
--------

* :ref:`guide-network` — network fault parameters
* :ref:`guide-process` — process / service / container faults
* :ref:`guide-resources` — CPU / memory / disk exhaustion
* :ref:`guide-llm` — LLM API fault parameters
* :ref:`guide-ssh` — SSHTarget setup and authentication
