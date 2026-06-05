.. _guide-resources:

Resource Faults
===============

Resource exhaustion faults consume CPU, memory, disk space, or I/O
bandwidth to simulate a **degraded node** — the same class of scenario
that Chaos Monkey addresses by killing the instance, but without destroying
the machine.  Your application must survive under resource pressure without
hanging, crashing, or producing incorrect results.

All faults use ``stress-ng`` (CPU / memory / I/O) or standard POSIX
``coreutils`` (disk) on the target machine.  They run in the background and
are cleaned up automatically on ``stop()`` / ``revert()``.

.. code-block:: text

   ┌──────────────────────────────────────────────────────────────────┐
   │                      TARGET  MACHINE                            │
   │                                                                  │
   │  ┌─────────────────────────┐    ┌───────────────────────────┐   │
   │  │   YOUR  APPLICATION     │    │   CJ  STRESS  WORKERS     │   │
   │  │                         │    │                           │   │
   │  │  inference / embedding  │    │  CPUStress  ── tight loop │   │
   │  │  agent calls / pipeline │    │  MemStress  ── vm alloc   │   │
   │  │                         │    │  IOStress   ── hdd write  │   │
   │  └──────────┬──────────────┘    │  DiskFull   ── dd zeros   │   │
   │             │  competes for     └───────────────────────────┘   │
   │             ▼                                                    │
   │  ╔══════════════════════════════════════════════════════════╗    │
   │  ║              SHARED  KERNEL  RESOURCES                   ║    │
   │  ╠══════════════╦═════════════╦══════════════╦═════════════╣    │
   │  ║   CPU cores  ║    RAM      ║   Disk I/O   ║  Disk space ║    │
   │  ╚══════════════╩═════════════╩══════════════╩═════════════╝    │
   └──────────────────────────────────────────────────────────────────┘

Available faults
----------------

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Fault
     - Description
   * - ``DiskFull``
     - Fill a filesystem near capacity using ``dd if=/dev/zero``
   * - ``CPUStress``
     - Saturate N CPU cores using ``stress-ng --cpu``
   * - ``MemoryStress``
     - Allocate N MiB of RAM using ``stress-ng --vm``
   * - ``IOStress``
     - Generate sustained disk I/O load using ``stress-ng --hdd``

Installing dependencies on the target
--------------------------------------

.. code-block:: bash

   # Ubuntu / Debian
   sudo apt-get install -y stress-ng

   # RHEL / CentOS / Fedora
   sudo dnf install -y stress-ng

   # coreutils (dd) is pre-installed on all POSIX systems


DiskFull
--------

Creates a large zero-filled file at ``path`` using ``dd``.  ``stop()`` /
``revert()`` delete the fill file, restoring free space immediately.

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, DiskFull
   from chaos_jungle.targets import SSHTarget

   target = SSHTarget("10.0.0.5", user="ubuntu")

   # Fill /var/lib/myapp with 20 GiB of zeros
   fault = DiskFull("/var/lib/myapp", size_mb=20_000)

   runner = ChaosRunner(Scenario("disk-full", [fault]), target)
   runner.start()
   # run workload — any write to /var/lib/myapp will fail with ENOSPC
   runner.stop()   # fill file deleted, space restored

**What to observe:**

* Does the application catch ``ENOSPC`` and surface a meaningful error?
* Does it fall back to a temporary directory or an alternative storage path?
* Does it recover automatically once space is freed, or does it require a restart?

.. warning::

   Set ``size_mb`` conservatively — leave at least 1–2 GiB free for the OS
   and other processes, or the target machine may become unresponsive.


CPUStress
---------

Runs ``stress-ng --cpu`` workers in the background.  Each worker executes
a tight computation loop on one CPU core.  ``stop()`` kills all stress-ng
CPU workers.

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, CPUStress
   from chaos_jungle.targets import SSHTarget

   target = SSHTarget("10.0.0.5", user="ubuntu")

   # Saturate 4 cores for up to 5 minutes
   fault = CPUStress(cores=4, duration_s=300)

   runner = ChaosRunner(Scenario("cpu-pressure", [fault]), target)
   runner.start()
   # run inference workload — CPU-bound tasks will slow down
   runner.stop()   # stress-ng killed

**What to observe:**

* Does the LLM inference / embedding service meet its latency SLA under
  CPU contention?
* Does the application's timeout fire before the CPU is freed?
* Does a multi-tenant system correctly throttle other users' requests?

Combining with measurement::

   def workload():
       import time
       t0 = time.time()
       reply = call_llm("Summarise this document in one sentence.", model)
       return {"duration_s": round(time.time() - t0, 2), "chars": len(reply)}

   result = runner.measure(workload, n_baseline=5, n_fault=5)
   # fault_mean("duration_s") should be noticeably higher than baseline


MemoryStress
------------

Runs ``stress-ng --vm`` to allocate ``mb`` MiB of anonymous memory and
continuously write to it.  This forces the OS to swap and evicts page-cache
pages, causing memory-mapped files and caches to miss.

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, MemoryStress
   from chaos_jungle.targets import SSHTarget

   target = SSHTarget("10.0.0.5", user="ubuntu")

   # Allocate 8 GiB to simulate a memory-hungry co-tenant
   fault = MemoryStress(mb=8192, duration_s=120)

   runner = ChaosRunner(Scenario("memory-pressure", [fault]), target)
   runner.start()
   # run workload — OS will swap, LLM model weights may be evicted from page cache
   runner.stop()   # stress-ng killed, memory freed

**What to observe:**

* Does the model server (Ollama, vLLM, etc.) slow down as weights are
  swapped out of page cache?
* Does the application's OOM-kill trigger if ``mb`` exceeds available RAM?
* Does the application recover when memory pressure subsides?

.. tip::

   Set ``mb`` to approximately 70–80 % of total RAM to create realistic
   pressure without triggering the OOM killer.


IOStress
--------

Runs ``stress-ng --hdd`` workers that continuously write and read temporary
files under ``path``.  This saturates the I/O scheduler and increases
latency for all other I/O on the same disk.

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, IOStress
   from chaos_jungle.targets import SSHTarget

   target = SSHTarget("10.0.0.5", user="ubuntu")

   # 2 workers hammering /var/lib/data
   fault = IOStress(workers=2, duration_s=120, path="/var/lib/data")

   runner = ChaosRunner(Scenario("io-stress", [fault]), target)
   runner.start()
   # run storage-heavy pipeline — reads/writes will be slower
   runner.stop()   # stress-ng killed, temp files deleted

**What to observe:**

* Does the pipeline's throughput drop under I/O contention?
* Do write operations to the same disk timeout or fail?
* Does a read-heavy workload (model loading, log parsing) slow down
  proportionally?


Combined degraded-node scenario
---------------------------------

Combine multiple resource faults to simulate a node under severe pressure:

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, CPUStress, MemoryStress
   from chaos_jungle.targets import SSHTarget

   target = SSHTarget("10.0.0.5", user="ubuntu")

   # Saturate CPU AND pressure memory simultaneously
   scenario = Scenario("degraded-node", [
       CPUStress(cores=4, duration_s=180),
       MemoryStress(mb=4096, duration_s=180),
   ])
   runner = ChaosRunner(scenario, target)
   runner.start()
   # run workload under combined CPU + memory pressure
   runner.stop()   # both stress processes killed

Add network faults for a fully realistic degraded node::

   from chaos_jungle import NetworkLoss

   scenario = Scenario("full-degraded", [
       CPUStress(cores=2),
       MemoryStress(mb=2048),
       NetworkLoss("3%"),
   ])


Revert behaviour
----------------

All resource faults clean up on ``stop()`` and ``revert()``:

* ``DiskFull`` — deletes ``{path}/.cj_diskfill``
* ``CPUStress`` — kills ``stress-ng --cpu`` processes, removes log file
* ``MemoryStress`` — kills ``stress-ng --vm`` processes, removes log file
* ``IOStress`` — kills ``stress-ng --hdd`` processes, removes log file

If the target machine is lost mid-experiment, run this on the target to
clean up manually::

   pkill -f stress-ng || true
   rm -f /tmp/.cj_diskfill /tmp/cj_*.log

See also
--------

* :ref:`guide-process` — process/service/container faults
* :ref:`guide-ssh` — SSHTarget setup and passwordless sudo
