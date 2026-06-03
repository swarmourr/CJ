.. _guide-measurement:

Measurement, Scheduling & Fault Composition
============================================

Three capabilities that turn chaos-jungle from a fault *injector* into a
fault *measurement* tool:

* :ref:`measure` — run a workload under baseline and fault conditions,
  compute delta automatically.
* :ref:`scheduling` — inject a fault mid-workload without modifying the
  workload code.
* :ref:`composition` — run multiple faults simultaneously and measure the
  compounding effect.

.. _measure:

Automatic measurement
---------------------

``ChaosRunner.measure(workload)`` runs your workload under two conditions:

1. **Baseline** — no fault active, *n_baseline* trials.
2. **Fault** — fault injected, *n_fault* trials.

It averages the metrics across trials, computes ``delta = fault - baseline``
for every numeric key, persists everything to the session database, and
returns a :class:`~chaos_jungle.runner.MeasurementResult`.

Your workload must be a zero-argument callable that returns a ``dict`` of
metrics each time it is called:

.. code-block:: python

   import hashlib, os, shutil, time
   from chaos_jungle import Scenario, ChaosRunner, MeasurementResult
   from chaos_jungle.faults import StorageCorrupt
   from chaos_jungle.targets import LocalTarget

   SRC  = "/data/protein_files"
   DEST = "/tmp/transfer_out"

   def transfer_and_verify() -> dict:
       """Copy .pdb files and verify MD5 integrity."""
       os.makedirs(DEST, exist_ok=True)
       for f in os.listdir(DEST):
           os.remove(os.path.join(DEST, f))

       files  = [f for f in os.listdir(SRC) if f.endswith(".pdb")]
       errors = 0
       t0     = time.time()

       for fname in files:
           src_path = os.path.join(SRC, fname)
           dst_path = os.path.join(DEST, fname)
           shutil.copy2(src_path, dst_path)
           if _md5(src_path) != _md5(dst_path):
               errors += 1

       total = len(files)
       return {
           "duration_s":     round(time.time() - t0, 4),
           "errors":         errors,
           "integrity_rate": round((total - errors) / total, 4),
       }

   runner = ChaosRunner(
       Scenario("storage-measure", faults=[
           StorageCorrupt(directory=SRC, pattern="*.pdb", interval="5s"),
       ]),
       target=LocalTarget(),
       auto_install=True,
   )

   result: MeasurementResult = runner.measure(
       transfer_and_verify,
       n_baseline=3,
       n_fault=3,
   )

   print(result.summary())

Output::

   Scenario : storage-measure
   Trials   : 3 baseline / 3 fault

     duration_s                     baseline=0.012   fault=0.013   Δ +0.001
     errors                         baseline=0.0     fault=2.667   Δ +2.667
     integrity_rate                 baseline=1.0     fault=0.7333  Δ -0.2667

MeasurementResult
~~~~~~~~~~~~~~~~~

.. code-block:: python

   result.baseline         # {"duration_s": 0.012, "errors": 0.0, ...}
   result.fault            # {"duration_s": 0.013, "errors": 2.667, ...}
   result.delta            # {"duration_s": 0.001, "errors": 2.667, ...}
   result.raw_baseline     # list of per-trial dicts (unaveraged)
   result.raw_fault        # list of per-trial dicts (unaveraged)
   result.session_id       # DB session id — use with dashboard / export

Pass/fail assertions
~~~~~~~~~~~~~~~~~~~~

Use :meth:`~chaos_jungle.runner.MeasurementResult.passed` to assert a
threshold on any delta metric:

.. code-block:: python

   # PASS if integrity rate degrades by no more than 5 %
   assert result.passed("integrity_rate", threshold=0.05), (
       f"integrity degraded by {result.delta['integrity_rate']:.2%}"
   )

   # PASS if fewer than 1 error per run on average
   assert result.passed("errors", threshold=1.0)

.. _scheduling:

Fault scheduling
----------------

``start(start_after=N)`` defers fault injection by *N* seconds.  The call
returns immediately and injection happens in a background thread.

Use this to hit a workload mid-run — simulating a disk failure or network
outage that starts *during* a computation, not before it:

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner
   from chaos_jungle.faults import NetworkDelay
   from chaos_jungle.targets import LocalTarget

   runner = ChaosRunner(
       Scenario("mid-job-delay", faults=[NetworkDelay("2000ms")]),
       target=LocalTarget(),
   )

   # Inject after 30 s, auto-clear after 15 s more (at T=45 s)
   runner.start(start_after=30, duration=15)

   run_long_computation()   # fault hits at T=30s without any code change

   # runner auto-stopped at T=45s; join if you need to wait
   runner.stop()            # safe to call even after auto-stop

Combining ``start_after`` and ``duration`` gives a precise fault window::

   T=0s                workload starts
   T=30s               fault injected  (start_after=30)
   T=45s               fault cleared   (duration=15)
   T=60s               workload ends

.. tip::

   If ``duration`` expires before you call ``stop()``, the stop is a
   no-op — safe to call regardless.

.. _composition:

Fault composition
-----------------

Pass multiple faults to :class:`~chaos_jungle.scenario.Scenario` to inject
them simultaneously.  All faults start together and stop together:

.. code-block:: python

   from chaos_jungle import Scenario, ChaosRunner, MeasurementResult
   from chaos_jungle.faults import StorageCorrupt, NetworkDelay
   from chaos_jungle.targets import LocalTarget

   def run_scenario(label, faults):
       runner = ChaosRunner(
           Scenario(label, faults=faults),
           target=LocalTarget(),
           auto_install=True,
           conflict="force",
       )
       return runner.measure(transfer_and_verify, n_baseline=3, n_fault=3)

   result_a = run_scenario("storage-only",  [StorageCorrupt(...)])
   result_b = run_scenario("network-only",  [NetworkDelay("800ms")])
   result_c = run_scenario("combined",      [StorageCorrupt(...),
                                             NetworkDelay("800ms")])

   print(f"storage Δerrors : {result_a.delta['errors']:+.2f}")
   print(f"network Δerrors : {result_b.delta['errors']:+.2f}")
   print(f"combined Δerrors: {result_c.delta['errors']:+.2f}  ← compounding")

Faults are started in list order and stopped in reverse order, so
dependencies are handled correctly (e.g. a network fault always cleans up
before a storage fault that writes to a network share).

Summary table
~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 30 35 35

   * - Feature
     - API
     - Use case
   * - Automatic measurement
     - ``runner.measure(workload, n_baseline, n_fault)``
     - Quantify fault impact with statistics
   * - Pass/fail assertion
     - ``result.passed(key, threshold)``
     - CI/CD fault tolerance gates
   * - Fault scheduling
     - ``runner.start(start_after=N, duration=M)``
     - Inject fault mid-computation
   * - Fault composition
     - ``Scenario("name", [fault1, fault2])``
     - Measure compounding effects
   * - Raw trial data
     - ``result.raw_baseline`` / ``result.raw_fault``
     - Statistical analysis, plotting
   * - DB persistence
     - ``result.session_id``
     - Dashboard, ``chaos-jungle export``
