Paper Study: Reproducing the PEARC '19 Experiments
====================================================

This guide shows how to develop a complete chaos engineering study using
chaos-jungle, modelled on the nine experiments from:

  *Rynge et al., "Chaos Engineering for Scientific Workflows", PEARC '19,
  Chicago, IL, USA.*

The original paper tested a **Rosetta protein-design workflow** running under
Pegasus WMS and HTCondor across nine fault conditions — from a clean baseline
to fully compounded network and storage chaos.

.. contents::
   :class: this-will-duplicate-information-and-it-is-still-useful-here
   :local:
   :depth: 2

----

The Workflow Under Test
------------------------

.. code-block:: text

   10 x .pdb files (protein structures)
            │
            ▼
     HTCondor distributes
     10 jobs across workers
            │
            ▼
     minirosetta runs on each file:
     reads .pdb → computes redesign → writes score + output .pdb
            │
            ▼
     Pegasus collects results at submit node

Each job reads a protein structure file, computes a redesign using the
Rosetta scoring function, and writes a score file (``*_score.sc``) and
an output structure (``*_0001.pdb``). Chaos attacks the *inputs* (storage),
the *transfers* (network), and the *outputs* (storage).

----

The Nine Experiments
---------------------

.. list-table::
   :header-rows: 1
   :widths: 8 30 62

   * - ID
     - Fault(s)
     - Question
   * - E1
     - *none*
     - Baseline — reference point for all other experiments
   * - E2
     - ``StorageCorrupt``
     - Can Pegasus detect silent disk-level bit-flips?
   * - E3
     - ``NetworkDelay`` 100ms ±10ms
     - How sensitive is the workflow to slow file staging?
   * - E4
     - ``NetworkLoss`` 5%
     - Does HTCondor handle unreliable connections gracefully?
   * - E5
     - ``NetworkCorrupt`` 1%
     - Does Pegasus verify file integrity after transfer?
   * - E6
     - ``StorageCorrupt`` + ``NetworkDelay``
     - Does slow staging give corruption more time to act?
   * - E7
     - ``StorageCorrupt`` + ``NetworkLoss``
     - Can you trace faults when jobs are rescheduled?
   * - E8
     - ``StorageCorrupt`` + ``NetworkCorrupt``
     - Can you distinguish disk vs network corruption?
   * - E9
     - Everything combined
     - What does partial workflow success look like?

----

How to Develop the Study
-------------------------

Step 1 — Define the target
~~~~~~~~~~~~~~~~~~~~~~~~~~~

The chaos is injected on the **worker node** — the machine where HTCondor
runs the Rosetta jobs. Create an SSH target pointing at it:

.. code-block:: python

   from chaos_jungle.targets import SSHTarget

   worker = SSHTarget(
       host="worker1.example.com",
       user="ubuntu",
       key="~/.ssh/id_rsa",
   )

Step 2 — Define each experiment
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Each experiment is a :class:`~chaos_jungle.scenario.Scenario` — a named
list of faults. The faults match the paper's conditions exactly:

.. code-block:: python

   from chaos_jungle import Scenario
   from chaos_jungle.faults import (
       NetworkDelay, NetworkLoss, NetworkCorrupt, StorageCorrupt,
   )

   PDB_DIR = "/scratch/rosetta/input"

   experiments = {

       "E1-baseline": Scenario("E1-baseline", faults=[]),

       "E2-storage": Scenario("E2-storage", faults=[
           StorageCorrupt("*.pdb", PDB_DIR, interval="10m"),
       ]),

       "E3-delay": Scenario("E3-delay", faults=[
           NetworkDelay("100ms", jitter="10ms"),
       ]),

       "E4-loss": Scenario("E4-loss", faults=[
           NetworkLoss("5%"),
       ]),

       "E5-corrupt": Scenario("E5-corrupt", faults=[
           NetworkCorrupt("1%"),
       ]),

       "E6-storage-delay": Scenario("E6-storage-delay", faults=[
           NetworkDelay("100ms", jitter="10ms"),
           StorageCorrupt("*.pdb", PDB_DIR, interval="10m"),
       ]),

       "E7-storage-loss": Scenario("E7-storage-loss", faults=[
           NetworkLoss("5%"),
           StorageCorrupt("*.pdb", PDB_DIR, interval="10m"),
       ]),

       "E8-storage-net-corrupt": Scenario("E8-storage-net-corrupt", faults=[
           NetworkCorrupt("1%"),
           StorageCorrupt("*.pdb", PDB_DIR, interval="10m"),
       ]),

       "E9-all-chaos": Scenario("E9-all-chaos", faults=[
           NetworkDelay("100ms", jitter="10ms"),
           StorageCorrupt("*.pdb", PDB_DIR, interval="10m"),
       ]),
   }

Step 3 — Run each experiment
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Wrap the workflow call with ``ChaosRunner``. Chaos starts, the workflow
runs, chaos always reverts — even if the workflow crashes:

.. code-block:: python

   import subprocess
   from chaos_jungle import ChaosRunner

   for name, scenario in experiments.items():
       runner = ChaosRunner(
           scenario,
           target=worker,
           auto_install=True,   # install tc/filefrag if missing
           conflict="raise",    # guardrails on
       )

       runner.start()
       print(f"[{name}] chaos ON")
       try:
           subprocess.run(
               ["pegasus-run", "--output-dir", f"results/{name}"],
               cwd="/home/ubuntu/rosetta-workflow",
               check=True,
           )
       finally:
           runner.stop()
           print(f"[{name}] chaos OFF — reverted")

       # Save the chaos session log
       with open(f"results/{name}/chaos-session.json", "w") as fh:
           fh.write(runner.export("json"))

Step 4 — Compare scores against baseline
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

After all experiments complete, diff the score files against E1:

.. code-block:: python

   import os
   from pathlib import Path

   baseline_scores = list(Path("results/E1-baseline/scores").glob("*.sc"))

   for name in experiments:
       if name == "E1-baseline":
           continue
       ok = changed = missing = 0
       for bsf in baseline_scores:
           exp_file = Path(f"results/{name}/scores/{bsf.name}")
           if not exp_file.exists():
               missing += 1
           elif bsf.read_text() != exp_file.read_text():
               changed += 1
           else:
               ok += 1
       print(f"{name:<30}  ok={ok}  changed={changed}  missing={missing}")

Step 5 — Query the chaos session database
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Every event is stored in ``~/.chaos-jungle/chaos_jungle.db``:

.. code-block:: python

   from chaos_jungle.db.session_db import SessionDB

   db = SessionDB()
   for session in db.list_sessions():
       data = db.export_session(session["id"])
       name  = data["session"]["name"]
       start = data["session"]["started_at"]
       stop  = data["session"]["stopped_at"]
       faults_injected = len(data["faults"])
       events          = len(data["events"])
       print(f"{name}  start={start}  faults={faults_injected}  events={events}")

Or query directly with SQLite:

.. code-block:: bash

   sqlite3 ~/.chaos-jungle/chaos_jungle.db \
     "SELECT s.name, f.kind, f.parameters, e.message
      FROM sessions s
      JOIN faults f ON f.session_id = s.id
      JOIN events e ON e.session_id = s.id
      ORDER BY e.id"

----

Running All 9 Experiments at Once
-----------------------------------

Option A — Sequential on one node (simple)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Use the complete script provided in ``examples/paper_study.py``:

.. code-block:: bash

   # Set your cluster details
   export CJ_WORKER_HOST=worker1.example.com
   export CJ_WORKER_USER=ubuntu
   export CJ_WORKFLOW_DIR=/home/ubuntu/rosetta-workflow
   export CJ_PDB_DIR=/scratch/rosetta/input
   export CJ_DURATION=30m

   # Run all 9 experiments sequentially
   python examples/paper_study.py

   # Run one experiment only
   python examples/paper_study.py --experiment E3-net-delay

   # Dry run — print plan without connecting
   python examples/paper_study.py --dry-run

Output::

   ======================================================================
     Chaos-Jungle Paper Study  —  9 experiment(s)
     Worker  : ubuntu@worker1.example.com
     Duration: 30m per experiment
     Results : ./results
   ======================================================================

   [1/9]  E1-baseline
     Baseline — no chaos
     Question: Normal behavior. Reference point for all other experiments.
     Fault: none (baseline)
     [workflow] Submitting E1-baseline ...
     [workflow] done  status=ok  wall=1803s
     [chaos] OFF — reverted
     [log] saved → results/E1-baseline/chaos-session.json

   ...

   ==========================================================================================
     EXPERIMENT                    WORKFLOW       OK    CHANGED  MISSING  NOTES
     ------------------------------------------------------------------------------------------
     E1-baseline                      ok           10          0        0
     E2-storage-only                  ok            7          3        0
     E3-net-delay                     ok           10          0        0
     E4-net-loss                      ok            9          0        1
     E5-net-corrupt                   ok           10          0        0
     E6-storage-delay                 ok            6          4        0
     E7-storage-loss                  ok            5          3        2
     E8-storage-net-corrupt           ok            6          3        1
     E9-all-chaos                     ok            4          4        2
   ==========================================================================================

Option B — Parallel on 9 nodes (fast)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Use the provided YAML config in ``examples/paper_study.yml``.
Replace the hostnames with your actual nodes:

.. code-block:: yaml

   # examples/paper_study.yml  (excerpt)
   duration: 30m
   conflict: raise
   auto_install: true

   experiments:
     - name: E1-baseline
       target: ssh://ubuntu@node0.example.com
       faults: []

     - name: E2-storage-only
       target: ssh://ubuntu@node1.example.com
       faults:
         - kind: StorageCorrupt
           pattern: "*.pdb"
           directory: /scratch/rosetta/input
           interval: 10m

     - name: E3-net-delay
       target: ssh://ubuntu@node2.example.com
       faults:
         - kind: NetworkDelay
           delay: 100ms
           jitter: 10ms
     # ... see examples/paper_study.yml for the full config

Then run:

.. code-block:: bash

   chaos-jungle suite --config examples/paper_study.yml

All 9 experiments start simultaneously. The total wall-clock time equals
the duration of the longest single experiment rather than 9 × duration.

----

What the Data Reveals
----------------------

After running, compare each experiment against E1 (baseline):

.. list-table::
   :header-rows: 1
   :widths: 22 25 53

   * - Experiment
     - Dangerous failure mode
     - What to look for
   * - E2 — storage
     - Silent wrong result
     - ``files_changed > 0`` with ``workflow.status = ok``. The job succeeded but the science is wrong.
   * - E3 — delay
     - Cascading timeouts
     - Increased ``wall_s``. HTCondor transfer errors if staging timeouts are short.
   * - E4 — loss
     - Worker appears offline, jobs rescheduled
     - ``files_missing > 0``. Pegasus retried jobs. Check ``pegasus-analyzer``.
   * - E5 — net corrupt
     - Corrupted data passes TCP checksum
     - ``files_changed > 0`` without ``files_missing``. Rare but possible.
   * - E6 — storage + delay
     - More corruptions than E2 alone
     - Higher ``files_changed`` vs E2: slow staging gives corruption more time.
   * - E7 — storage + loss
     - Cannot trace which node produced wrong result
     - ``files_changed`` + ``files_missing``. Jobs ran twice (on different nodes).
   * - E8 — storage + net corrupt
     - Cannot distinguish disk vs network corruption
     - ``files_changed > 0``. No way to know the source without per-layer checksums.
   * - E9 — all chaos
     - Partial workflow success
     - Mixed results: some ok, some wrong, some missing. Pegasus reports "complete".

----

Reading the Chaos Session Log
-------------------------------

Each experiment saves a JSON log to ``results/<name>/chaos-session.json``.
The log records exactly what was injected, when, and the full event timeline:

.. code-block:: python

   import json

   with open("results/E2-storage-only/chaos-session.json") as fh:
       log = json.load(fh)

   print(log["session"])
   # {'id': 2, 'name': 'E2-storage-only',
   #  'started_at': '2025-05-29T14:00:00+00:00',
   #  'stopped_at': '2025-05-29T14:30:01+00:00',
   #  'status': 'reverted'}

   for fault in log["faults"]:
       print(fault["kind"], fault["parameters"])
   # StorageCorrupt {"pattern": "*.pdb", "directory": "/scratch/rosetta/input",
   #                 "interval": "10m", "recursive": false}

   for event in log["events"]:
       print(event["timestamp"], event["message"])
   # 2025-05-29T14:00:00+00:00  Session started: E2-storage-only
   # 2025-05-29T14:00:00+00:00  Starting fault: StorageCorrupt
   # 2025-05-29T14:00:01+00:00  Fault started: StorageCorrupt
   # 2025-05-29T14:30:00+00:00  Stopping fault: StorageCorrupt
   # 2025-05-29T14:30:01+00:00  Fault stopped and reverted: StorageCorrupt
   # 2025-05-29T14:30:01+00:00  Session closed

----

Extending the Study
--------------------

Add a silent BPF corruption experiment
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The original paper's core technique — swapping payload bytes while
preserving TCP checksums — is available as
:class:`~chaos_jungle.faults.bpf.SilentNetworkCorrupt`:

.. code-block:: python

   from chaos_jungle.faults import SilentNetworkCorrupt

   # Mangle 1 in 5000 packets silently (preserves checksum)
   silent = Scenario("E10-silent-corrupt", faults=[
       SilentNetworkCorrupt(rate=5000, hook="tc"),
   ])

.. code-block:: yaml

   # In paper_study.yml
   - name: E10-silent-corrupt
     target: ssh://ubuntu@node9.example.com
     faults:
       - kind: SilentNetworkCorrupt
         rate: 5000
         hook: tc

This is the hardest fault to detect: the packet is delivered, the checksum
passes, but 2 bytes in the payload have been silently swapped.

Vary the storage corruption rate
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

To understand the sensitivity to corruption frequency, run multiple
storage experiments with different intervals:

.. code-block:: yaml

   - name: E2a-storage-5m
     target: ssh://ubuntu@node-a.example.com
     faults:
       - kind: StorageCorrupt
         pattern: "*.pdb"
         directory: /scratch/rosetta/input
         interval: 5m     # more frequent

   - name: E2b-storage-30m
     target: ssh://ubuntu@node-b.example.com
     faults:
       - kind: StorageCorrupt
         pattern: "*.pdb"
         directory: /scratch/rosetta/input
         interval: 30m    # less frequent

Add a custom verification step
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

After ``runner.stop()``, use the target to run your own integrity check:

.. code-block:: python

   from chaos_jungle import ChaosRunner
   from chaos_jungle.targets import SSHTarget

   worker = SSHTarget("worker1", user="ubuntu")
   runner = ChaosRunner(scenario, worker)

   runner.start()
   try:
       run_workflow()
   finally:
       runner.stop()

   # Custom post-chaos check: hash all output files
   _, stdout, _ = worker.run(
       "find /scratch/rosetta/output -name '*.pdb' -exec md5sum {} \\;"
   )
   with open(f"results/{name}/md5sums.txt", "w") as fh:
       fh.write(stdout)

----

Full Example Files
-------------------

The complete, runnable study is in the ``examples/`` directory:

* ``examples/paper_study.py`` — Python script implementing E1–E9 sequentially
* ``examples/paper_study.yml`` — YAML config for the parallel 9-node version

.. code-block:: bash

   # Sequential (one node)
   python examples/paper_study.py

   # Parallel (nine nodes, edit hostnames in paper_study.yml first)
   chaos-jungle suite --config examples/paper_study.yml
