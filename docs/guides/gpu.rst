.. _guide-gpu:

GPU Faults
==========

chaos-jungle can inject faults directly into GPU hardware state — throttling
power, locking clocks to minimum frequency, and filling VRAM — without any
physical access or special firmware.

All three fault classes (:class:`~chaos_jungle.faults.gpu.GPUThrottle`,
:class:`~chaos_jungle.faults.gpu.GPUMemoryPressure`,
:class:`~chaos_jungle.faults.gpu.GPUClockLock`) **auto-detect the installed
GPU vendor** at ``start()`` time and dispatch to the correct backend.  No
``vendor=`` parameter is needed.

Vendor detection order
----------------------

.. list-table::
   :header-rows: 1
   :widths: 15 30 55

   * - Vendor
     - Detection method
     - Backend used
   * - **NVIDIA**
     - ``nvidia-smi`` present and reports a GPU
     - ``nvidia-smi`` CLI + CUDA ctypes (``libcuda.so``)
   * - **AMD**
     - ``rocm-smi`` present **or** ``amdgpu`` sysfs nodes found
     - sysfs power/clock nodes + HIP ctypes (``libamdhip64.so``)
   * - **Intel**
     - ``i915``/``xe`` GT sysfs nodes found
     - sysfs ``rps_max_freq_mhz`` / ``rps_min_freq_mhz``

Requirements
------------

.. list-table::
   :header-rows: 1
   :widths: 15 85

   * - Vendor
     - Requirements
   * - NVIDIA
     - ``nvidia-smi`` in PATH, CUDA toolkit (``libcuda.so``), ``sudo`` access
   * - AMD
     - ``amdgpu`` kernel module loaded, ROCm (``libamdhip64.so``) for memory
       pressure, ``sudo`` access
   * - Intel
     - ``i915`` or ``xe`` kernel module loaded, ``sudo`` access;
       memory pressure **not supported** (use
       :class:`~chaos_jungle.faults.resources.MemoryStress` instead)

----

GPUThrottle — thermal throttling simulation
-------------------------------------------

Reduces the GPU's power limit (NVIDIA / AMD) or maximum clock frequency
(Intel) to simulate the effect of a GPU running hot and throttling.

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario
   from chaos_jungle.faults import GPUThrottle
   from chaos_jungle.targets import LocalTarget

   fault  = GPUThrottle(power_pct=40)          # cap at 40% of TDP / max freq
   runner = ChaosRunner(Scenario("gpu-throttle", [fault]), LocalTarget())

   runner.start()
   run_training_job()
   runner.stop()

Parameters:

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Parameter
     - Default
     - Description
   * - ``power_pct``
     - ``50``
     - Target limit as % of the GPU's maximum TDP or frequency (1–100)
   * - ``gpu_id``
     - ``0``
     - GPU index (``nvidia-smi -L`` / ``rocm-smi -i`` / drm card order)

Backend details:

.. list-table::
   :header-rows: 1
   :widths: 15 40 45

   * - Vendor
     - Start
     - Revert
   * - NVIDIA
     - ``nvidia-smi -pl <watts>``
     - Restore original watt limit via ``nvidia-smi -pl``
   * - AMD
     - Write ``<microwatts>`` to ``hwmon*/power1_cap``
     - Restore original µW value
   * - Intel
     - Write ``<MHz>`` to ``gt0/rps_max_freq_mhz``
     - Restore original MHz value

----

GPUMemoryPressure — VRAM pressure / OOM injection
--------------------------------------------------

Allocates a fixed percentage of GPU VRAM in a background process.
Workloads that try to allocate more than the remaining VRAM will receive
``CUDA out of memory`` (NVIDIA) or ``hipErrorOutOfMemory`` (AMD) errors.

The required scripts (``cj_gpu_memory.py`` for NVIDIA,
``cj_gpu_memory_amd.py`` for AMD) are **bundled inside the package** and
deployed automatically to ``~/.chaos-jungle/gpu/`` on the target — no
manual setup.

.. code-block:: python

   from chaos_jungle.faults import GPUMemoryPressure

   fault = GPUMemoryPressure(memory_pct=85)    # hold 85% of VRAM

Parameters:

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Parameter
     - Default
     - Description
   * - ``memory_pct``
     - ``80``
     - Percentage of total VRAM to allocate (1–99)
   * - ``gpu_id``
     - ``0``
     - GPU index

.. note::

   Intel iGPU shares system RAM with the CPU.  ``GPUMemoryPressure`` is not
   supported for Intel GPUs — use
   :class:`~chaos_jungle.faults.resources.MemoryStress` instead.

Backend details:

.. list-table::
   :header-rows: 1
   :widths: 15 50 35

   * - Vendor
     - Mechanism
     - Revert
   * - NVIDIA
     - Background ``cj_gpu_memory.py`` via ``libcuda.so`` (``cuMemAlloc``)
     - ``SIGTERM`` to background process
   * - AMD
     - Background ``cj_gpu_memory_amd.py`` via ``libamdhip64.so`` (``hipMalloc``)
     - ``SIGTERM`` to background process

----

GPUClockLock — sustained clock degradation
-------------------------------------------

Locks the GPU to its minimum supported clock frequency (or a specific MHz
value), simulating a GPU running at degraded performance due to driver
issues, power budget constraints, or persistent thermal throttling.

.. code-block:: python

   from chaos_jungle.faults import GPUClockLock

   fault = GPUClockLock()                # auto-detect minimum clock
   fault = GPUClockLock(freq_mhz=300)    # lock to a specific frequency

Parameters:

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Parameter
     - Default
     - Description
   * - ``freq_mhz``
     - ``None``
     - Clock frequency in MHz.  ``None`` = query and use the GPU's minimum
       supported clock automatically.
   * - ``gpu_id``
     - ``0``
     - GPU index

Backend details:

.. list-table::
   :header-rows: 1
   :widths: 15 45 40

   * - Vendor
     - Start
     - Revert
   * - NVIDIA
     - ``nvidia-smi --lock-gpu-clocks=<freq>,<freq>``
     - ``nvidia-smi --reset-gpu-clocks``
   * - AMD
     - ``power_dpm_force_performance_level=manual``, ``pp_dpm_sclk=0``
     - ``power_dpm_force_performance_level=auto``
   * - Intel
     - Set ``rps_max_freq_mhz`` = ``rps_min_freq_mhz`` = ``<freq>``
     - Restore original ``rps_max`` / ``rps_min`` values

----

Full example — measure GPU throttle impact on training throughput
-----------------------------------------------------------------

.. code-block:: python

   import time
   from chaos_jungle import ChaosRunner, Scenario
   from chaos_jungle.faults import GPUThrottle
   from chaos_jungle.targets import SSHTarget

   target = SSHTarget("gpu-node", user="ubuntu")
   fault  = GPUThrottle(power_pct=50)
   runner = ChaosRunner(Scenario("throttle-impact", [fault]), target,
                        auto_install=False, conflict="warn")

   def training_step():
       t0 = time.time()
       # run one epoch / benchmark step on the remote node
       rc, out, _ = target.run("python3 ~/train.py --steps 100 --benchmark")
       elapsed = time.time() - t0
       # parse samples/sec from output
       samples_per_sec = float(out.split("samples/sec:")[-1].split()[0])
       return {"duration_s": elapsed, "samples_per_sec": samples_per_sec}

   result = runner.measure(training_step, n_baseline=3, n_fault=3)
   print(result.summary())

Expected output::

   Scenario : throttle-impact
   Trials   : 3 baseline / 3 fault

     duration_s                     baseline=12.4   fault=22.1   Δ +9.7
     samples_per_sec                baseline=840.3  fault=462.1  Δ -378.2

----

Combining GPU faults with other faults
---------------------------------------

GPU faults compose with any other fault in a single
:class:`~chaos_jungle.scenario.Scenario`:

.. code-block:: python

   from chaos_jungle.faults import GPUThrottle, NetworkDelay, StorageCorrupt

   scenario = Scenario("full-stress", faults=[
       GPUThrottle(power_pct=40),
       NetworkDelay("200ms"),
       StorageCorrupt("*.pt", "/checkpoints", interval="30s"),
   ])

   runner = ChaosRunner(scenario, SSHTarget("gpu-node", user="ubuntu"))
   result = runner.measure(run_training_job, n_baseline=2, n_fault=2)
   print(result.summary())

----

Fault comparison table
-----------------------

.. list-table::
   :header-rows: 1
   :widths: 25 20 20 15 20

   * - Fault
     - What is injected
     - What to measure
     - NVIDIA
     - AMD / Intel
   * - ``GPUThrottle(50)``
     - Power cap at 50% TDP
     - throughput delta, duration delta
     - ✓
     - ✓ / ✓
   * - ``GPUMemoryPressure(80)``
     - 80% VRAM held, workload OOMs
     - OOM errors, job failures
     - ✓
     - ✓ / ✗
   * - ``GPUClockLock()``
     - Clocks pinned to minimum
     - throughput delta, latency delta
     - ✓
     - ✓ / ✓

See also
--------

* :ref:`guide-resources` — CPU / memory / disk stress faults
* :ref:`guide-measurement` — ``runner.measure()`` API
* :ref:`guide-ssh` — running faults on remote GPU nodes
