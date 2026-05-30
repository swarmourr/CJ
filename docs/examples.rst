Examples
========

This page walks through complete, copy-paste-ready examples covering every
feature of chaos-jungle — from a single network delay to a full multi-node
parallel suite.

1. Network faults — local machine
----------------------------------

The simplest case: inject a 100 ms delay on the machine you are sitting at.

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner
   from chaos_jungle.faults import NetworkDelay
   from chaos_jungle.targets import LocalTarget

   scenario = Scenario("local-delay", faults=[NetworkDelay("100ms")])
   runner   = ChaosRunner(scenario, LocalTarget())

   runner.start()
   # ── run your workload ──────────────────────────────
   import time; time.sleep(30)
   # ──────────────────────────────────────────────────
   runner.stop()

CLI equivalent:

.. code-block:: bash

   chaos-jungle start --scenario local-delay --delay 100ms
   # ... workload ...
   chaos-jungle stop


2. All network fault types
---------------------------

.. code-block:: python

   from chaos_jungle.faults import (
       NetworkDelay,
       NetworkLoss,
       NetworkCorrupt,
       NetworkDuplicate,
   )

   # 100 ms delay with ±10 ms jitter
   NetworkDelay("100ms", jitter="10ms")

   # drop 5 % of packets
   NetworkLoss("5%")

   # corrupt 1 % of packets (breaks checksum — application sees an error)
   NetworkCorrupt("1%")

   # duplicate 0.5 % of packets
   NetworkDuplicate("0.5%")

   # target a specific network interface instead of the default route
   NetworkDelay("200ms", iface="eth1")

You can combine any of them in one scenario (different fault types on the
same interface are fine; the same fault type twice is not):

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner
   from chaos_jungle.faults import NetworkDelay, NetworkLoss
   from chaos_jungle.targets import LocalTarget

   scenario = Scenario("delay-and-loss", faults=[
       NetworkDelay("100ms", jitter="10ms"),
       NetworkLoss("3%"),
   ])

   # ChaosRunner detects the conflict automatically via guardrails
   # — only one root qdisc per interface is allowed.
   # Combine faults in a single NetworkDelay call instead:

   scenario = Scenario("combined", faults=[
       NetworkDelay("100ms", jitter="10ms"),
   ])


3. Storage fault
-----------------

Inject silent bit-flips into files at the block-device level.
Requires ``sudo``, ``filefrag`` (e2fsprogs), and ``dd``.

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner
   from chaos_jungle.faults import StorageCorrupt
   from chaos_jungle.targets import LocalTarget

   fault = StorageCorrupt(
       pattern="*.pdb",          # glob — which files to corrupt
       directory="/data/output", # directory to watch
       interval="10m",           # flip a bit every 10 minutes
       recursive=False,
   )

   scenario = Scenario("storage-chaos", faults=[fault])
   runner   = ChaosRunner(scenario, LocalTarget())

   runner.start()
   run_my_workflow()
   runner.stop()

CLI:

.. code-block:: bash

   chaos-jungle start --scenario storage-test \
       --storage-pattern "*.pdb" \
       --storage-dir /data/output \
       --storage-interval 10m


4. Silent network corruption (BPF)
------------------------------------

:class:`~chaos_jungle.faults.bpf.SilentNetworkCorrupt` manges 1-in-N
packets at the XDP/TC hook level. Unlike ``NetworkCorrupt`` (which breaks
TCP checksums), this fault **preserves checksums** — the receiver accepts
the packet and only discovers the corruption when the application validates
its data. This is the original technique from the PEARC '19 paper.

Requires ``python3-bpfcc`` on the target.

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner
   from chaos_jungle.faults import SilentNetworkCorrupt
   from chaos_jungle.targets import SSHTarget

   fault = SilentNetworkCorrupt(
       rate=5000,   # mangle 1 in 5000 packets
       hook="tc",   # "tc" or "xdp"
   )

   scenario = Scenario("silent-corrupt", faults=[fault])
   target   = SSHTarget("worker1", user="ubuntu")
   runner   = ChaosRunner(scenario, target)

   runner.start()
   run_my_workflow()
   runner.stop()

CLI:

.. code-block:: bash

   chaos-jungle start --scenario silent-corrupt \
       --silent-corrupt 5000 --bpf-hook tc \
       --target ssh://ubuntu@worker1


5. Duration-based chaos
------------------------

Run chaos for a fixed time then stop automatically.

**Blocking** (``runner.run``):

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner
   from chaos_jungle.faults import NetworkDelay
   from chaos_jungle.targets import LocalTarget

   runner = ChaosRunner(
       Scenario("timed-delay", [NetworkDelay("100ms")]),
       LocalTarget(),
   )
   runner.run("10m")   # blocks for 10 minutes, then stops and reverts
   print("Done — chaos has been reverted.")

**Non-blocking with auto-stop** (``runner.start(duration=...)``):

.. code-block:: python

   runner.start(duration="30s")
   # returns immediately — chaos runs in the background
   # a daemon thread auto-stops after 30 s

**CLI**:

.. code-block:: bash

   # blocking — process exits after 10 min
   chaos-jungle start --scenario timed --delay 100ms --duration 10m

   # fire-and-forget
   chaos-jungle start --scenario fire-and-forget --delay 100ms
   chaos-jungle stop   # call whenever you're ready


6. SSH target
--------------

Inject faults on a remote machine over SSH.
Requires ``paramiko`` (``pip install chaos-jungle``) and SSH key access.

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner
   from chaos_jungle.faults import NetworkDelay, NetworkLoss
   from chaos_jungle.targets import SSHTarget

   target = SSHTarget(
       host="worker1.example.com",
       user="ubuntu",
       key="/home/me/.ssh/id_rsa",  # optional, uses ~/.ssh/id_rsa by default
       port=22,
   )

   scenario = Scenario("remote-delay", faults=[NetworkDelay("150ms")])
   runner   = ChaosRunner(scenario, target, auto_install=True)

   runner.run("5m")

CLI:

.. code-block:: bash

   chaos-jungle start --scenario remote-delay --delay 150ms \
       --target ssh://ubuntu@worker1.example.com \
       --auto-install --duration 5m


7. HTTP daemon target
----------------------

Start the chaos daemon on the remote machine once, then control it over
HTTP from anywhere — no SSH keys needed.

**On the remote machine:**

.. code-block:: bash

   pip install chaos-jungle
   cj-daemon --port 7777 --token mysecrettoken

**On your machine (Python):**

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner
   from chaos_jungle.faults import NetworkLoss
   from chaos_jungle.targets import HTTPTarget

   target = HTTPTarget("http://worker1:7777", token="mysecrettoken")

   runner = ChaosRunner(
       Scenario("http-loss", [NetworkLoss("5%")]),
       target,
   )
   runner.run("10m")

**On your machine (CLI):**

.. code-block:: bash

   chaos-jungle start --scenario http-loss --loss 5% \
       --target http://worker1:7777 --duration 10m


8. Decorator style
-------------------

Wrap any function — chaos starts before it, and always stops after
(even if the function raises).

.. code-block:: python

   from chaos_jungle.decorators import chaos
   from chaos_jungle.faults import NetworkDelay, StorageCorrupt
   from chaos_jungle.targets import SSHTarget

   @chaos(
       NetworkDelay("100ms"),
       StorageCorrupt("*.pdb", "/scratch/data"),
       target=SSHTarget("worker1", user="ubuntu"),
       scenario_name="decorator-test",
   )
   def run_experiment():
       import subprocess
       subprocess.run(["pegasus-run", "my_workflow"], check=True)

   run_experiment()   # chaos on → function → chaos off (always)

With duration:

.. code-block:: python

   @chaos(NetworkDelay("100ms"), duration="5m")
   def run_experiment():
       ...


9. Context manager style
-------------------------

Use ``chaos_session`` when you want the runner object available (e.g. to
export results while chaos is still active).

.. code-block:: python

   from chaos_jungle.decorators import chaos_session
   from chaos_jungle.faults import NetworkLoss
   from chaos_jungle.targets import SSHTarget

   target = SSHTarget("worker1", user="ubuntu")

   with chaos_session(
       NetworkLoss("5%"),
       target=target,
       scenario_name="ctx-loss",
   ) as runner:
       run_workflow()
       # inspect live session while chaos is still ON
       print(runner.export("json"))
   # chaos reverted here, outside the with block


10. Separate mode — two processes
-----------------------------------

Start chaos in one terminal, run your workload in another, stop when
you're ready. Useful for scripts that cannot be modified.

.. code-block:: bash

   # Terminal 1
   chaos-jungle start --scenario net-delay --delay 100ms --loss 2%
   # → prints: Session started: net-delay  (session id: 3)

   # Terminal 2
   bash my_workflow.sh   # runs under chaos

   # Terminal 1 (or anywhere on the same machine)
   chaos-jungle stop
   # → Session 3 stopped and reverted.

In Python:

.. code-block:: python

   # Process A
   from chaos_jungle import Scenario, ChaosRunner
   from chaos_jungle.faults import NetworkDelay
   from chaos_jungle.targets import LocalTarget

   runner = ChaosRunner(Scenario("sep-delay", [NetworkDelay("100ms")]), LocalTarget())
   runner.start()   # returns immediately

   # ... later in Process B ...
   from chaos_jungle.runner import ChaosRunner
   runner = ChaosRunner.attach()
   runner.stop()


11. Auto-install dependencies
------------------------------

If the target machine is missing ``tc``, ``filefrag``, or ``dd``,
``preflight()`` raises ``PreflightError`` by default — or installs
them automatically with ``auto_install=True``.

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner
   from chaos_jungle.faults import NetworkDelay
   from chaos_jungle.targets import SSHTarget

   runner = ChaosRunner(
       Scenario("auto", [NetworkDelay("100ms")]),
       SSHTarget("worker1", user="ubuntu"),
       auto_install=True,   # runs: sudo apt-get install -y iproute2
   )
   runner.start()

CLI:

.. code-block:: bash

   chaos-jungle start --scenario auto --delay 100ms \
       --target ssh://ubuntu@worker1 --auto-install


12. Guardrails and conflict handling
--------------------------------------

chaos-jungle checks for conflicts before starting any fault.

**Scenario-level** — caught before connecting to the target:

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner
   from chaos_jungle.faults import NetworkDelay, NetworkLoss
   from chaos_jungle.targets import LocalTarget
   from chaos_jungle import ConflictError

   # This raises ConflictError — two tc-netem faults on the same interface
   scenario = Scenario("bad", [NetworkDelay("100ms"), NetworkLoss("5%")])

   try:
       runner = ChaosRunner(scenario, LocalTarget())
       runner.start()
   except ConflictError as e:
       print(e)   # explains the conflict and suggests a fix

**Runtime-level** — caught by checking live state on the target:

.. code-block:: python

   # If a previous session left tc rules behind, ChaosRunner raises:
   # ConflictError: tc netem rule already active on eth0.
   #   Fix A: sudo tc qdisc del dev eth0 root
   #   Fix B: chaos-jungle stop --force
   #   Fix C: Use conflict='force' to skip this check.

**Conflict modes:**

.. code-block:: python

   # "raise" (default) — raise ConflictError
   runner = ChaosRunner(scenario, target, conflict="raise")

   # "warn" — emit ConflictWarning and continue
   runner = ChaosRunner(scenario, target, conflict="warn")

   # "force" — skip all guardrail checks entirely
   runner = ChaosRunner(scenario, target, conflict="force")

CLI:

.. code-block:: bash

   chaos-jungle start --scenario test --delay 100ms --conflict warn


13. Exporting session data
---------------------------

Every run is stored in ``~/.chaos-jungle/chaos_jungle.db``.

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner
   from chaos_jungle.faults import NetworkDelay
   from chaos_jungle.targets import LocalTarget

   runner = ChaosRunner(Scenario("export-demo", [NetworkDelay("50ms")]), LocalTarget())
   runner.start()
   run_workflow()
   runner.stop()

   # export after stopping
   data = runner.export("dict")
   print(data["session"])
   print(data["events"])

   # or as JSON
   print(runner.export("json"))

CLI:

.. code-block:: bash

   chaos-jungle list                        # show all sessions
   chaos-jungle export --session 3          # JSON
   chaos-jungle export --session 3 --format csv


14. ExperimentSuite — parallel multi-node chaos
-------------------------------------------------

Run different fault scenarios on different machines simultaneously.

.. code-block:: python

   from chaos_jungle import Scenario, ExperimentSuite
   from chaos_jungle.faults import NetworkDelay, NetworkLoss, StorageCorrupt
   from chaos_jungle.targets import SSHTarget, LocalTarget

   suite = ExperimentSuite(duration="10m", conflict="raise")

   # each experiment targets a different machine
   suite.add(
       Scenario("baseline",  []),
       LocalTarget(),
   )
   suite.add(
       Scenario("net-delay", [NetworkDelay("100ms", jitter="10ms")]),
       SSHTarget("node1", user="ubuntu"),
   )
   suite.add(
       Scenario("net-loss",  [NetworkLoss("5%")]),
       SSHTarget("node2", user="ubuntu"),
   )
   suite.add(
       Scenario("storage",   [StorageCorrupt("*.pdb", "/scratch/data")]),
       SSHTarget("node3", user="ubuntu"),
       duration="5m",  # this experiment stops after 5 m, others after 10 m
   )

   # start all four simultaneously
   results = suite.run(parallel=True)

   # print summary table
   ExperimentSuite.print_summary(results)

   # inspect individual results
   for name, r in results.items():
       if r.error:
           print(f"{name} FAILED: {r.error}")

Output::

   NAME                            STATUS    DURATION  ERROR
   ------------------------------------------------------------------------
   baseline                        ok            0.1s
   net-delay                       ok          600.0s
   net-loss                        ok          600.0s
   storage                         ok          300.1s

   4/4 experiments passed.

Sequential mode (one after the other):

.. code-block:: python

   results = suite.run(parallel=False)


15. YAML-based suite config
-----------------------------

Define your entire suite in a YAML file — no Python required.

**my-suite.yml:**

.. code-block:: yaml

   duration: 10m
   conflict: raise
   auto_install: false

   experiments:
     - name: baseline
       target: local
       faults: []

     - name: net-delay
       target: ssh://ubuntu@node1
       faults:
         - kind: NetworkDelay
           delay: 100ms
           jitter: 10ms

     - name: net-loss
       target: ssh://ubuntu@node2
       faults:
         - kind: NetworkLoss
           rate: 5%

     - name: corruption
       target: ssh://ubuntu@node3
       faults:
         - kind: NetworkCorrupt
           rate: 1%

     - name: storage-corrupt
       target: ssh://ubuntu@node4
       duration: 5m           # this experiment stops after 5 m
       faults:
         - kind: StorageCorrupt
           pattern: "*.pdb"
           directory: /scratch/data
           interval: 10m

     - name: silent-corrupt
       target: ssh://ubuntu@node5
       faults:
         - kind: SilentNetworkCorrupt
           rate: 5000
           hook: tc

Run it:

.. code-block:: bash

   chaos-jungle suite --config my-suite.yml

   # sequential
   chaos-jungle suite --config my-suite.yml --sequential

In Python:

.. code-block:: python

   from chaos_jungle import ExperimentSuite

   suite   = ExperimentSuite.from_yaml("my-suite.yml")
   results = suite.run()
   ExperimentSuite.print_summary(results)


16. End-to-end: Pegasus WMS experiment
----------------------------------------

A realistic example: inject faults while a Pegasus workflow runs on a
remote cluster, then collect results and export the chaos log.

.. code-block:: python

   import subprocess
   from chaos_jungle import Scenario, ChaosRunner
   from chaos_jungle.faults import NetworkDelay, StorageCorrupt
   from chaos_jungle.targets import SSHTarget

   # ── 1. Set up target ──────────────────────────────────────────
   target = SSHTarget(
       host="submit.cluster.example.com",
       user="ubuntu",
       auto_install=True,
   )

   # ── 2. Define faults ──────────────────────────────────────────
   scenario = Scenario("pegasus-chaos", faults=[
       NetworkDelay("100ms", jitter="10ms"),
       StorageCorrupt("*.pdb", "/scratch/pegasus-output", interval="10m"),
   ])

   # ── 3. Create runner ──────────────────────────────────────────
   runner = ChaosRunner(
       scenario,
       target,
       auto_install=True,
       conflict="raise",
   )

   # ── 4. Start chaos, run workflow, stop chaos ──────────────────
   runner.start()
   try:
       result = subprocess.run(
           ["pegasus-run", "--submit", "pegasus.yml"],
           capture_output=True, text=True,
       )
       print(result.stdout)
   finally:
       runner.stop()

   # ── 5. Export session log ─────────────────────────────────────
   with open("chaos-session.json", "w") as fh:
       fh.write(runner.export("json"))
   print("Chaos log saved to chaos-session.json")

Or as a decorator:

.. code-block:: python

   from chaos_jungle.decorators import chaos
   from chaos_jungle.faults import NetworkDelay, StorageCorrupt
   from chaos_jungle.targets import SSHTarget

   target = SSHTarget("submit.cluster.example.com", user="ubuntu")

   @chaos(
       NetworkDelay("100ms", jitter="10ms"),
       StorageCorrupt("*.pdb", "/scratch/pegasus-output"),
       target=target,
       scenario_name="pegasus-chaos",
       duration="1h",
   )
   def run_pegasus():
       import subprocess
       subprocess.run(["pegasus-run", "--submit", "pegasus.yml"], check=True)

   run_pegasus()   # chaos starts → workflow → chaos always reverted


17. Full parallel suite (YAML) — four-node experiment
-------------------------------------------------------

A production-ready YAML config that covers all fault types simultaneously
across four worker nodes.

**full-suite.yml:**

.. code-block:: yaml

   duration: 30m
   conflict: raise
   auto_install: true

   experiments:

     # Control: no faults — measures baseline performance
     - name: control
       target: ssh://ubuntu@node0
       faults: []

     # Network delay experiment
     - name: net-delay-100ms
       target: ssh://ubuntu@node1
       faults:
         - kind: NetworkDelay
           delay: 100ms
           jitter: 10ms

     # Packet loss experiment
     - name: packet-loss-5pct
       target: ssh://ubuntu@node2
       faults:
         - kind: NetworkLoss
           rate: 5%

     # Storage corruption experiment (runs for only 15 m)
     - name: storage-corruption
       target: ssh://ubuntu@node3
       duration: 15m
       faults:
         - kind: StorageCorrupt
           pattern: "*.pdb"
           directory: /scratch/output
           interval: 5m
           recursive: true

.. code-block:: bash

   chaos-jungle suite --config full-suite.yml
