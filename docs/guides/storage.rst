.. _guide-storage:

Storage Faults
==============

Storage faults simulate **block-level data corruption** — bit flips that
corrupt input files on disk without breaking file system metadata.  The file
still exists, still has the right size, and still opens normally.  Only the
content is silently wrong.

This is the most realistic model of real-world storage failures: cosmic ray
bit flips, failing NAND cells, write errors on spinning disks, and silent
RAID corruption.

Available faults
----------------

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Fault
     - Description
   * - ``StorageCorrupt``
     - Periodically flip random bytes in files matching a glob pattern (crontab-scheduled)
   * - ``StorageCorruptImmediate``
     - Corrupt a specific file instantly at ``start()`` time — no crontab needed
   * - ``SQLiteCorrupt``
     - Overwrite one page of a SQLite database file to trigger ``database disk image is malformed``


How StorageCorrupt works
-------------------------

1. At ``start()`` the bundled ``cj_storage.py`` script is uploaded to the
   target machine under ``~/.chaos-jungle/storage/``.
2. A crontab job is registered to run the corruptor on the specified
   interval (e.g. every 10 minutes).
3. Before corrupting each file, the original bytes are saved to
   ``~/.chaos-jungle/cj.db`` on the target.
4. At ``stop()`` the crontab is removed.
5. At ``revert()`` every corrupted byte is restored exactly from the backup.

.. mermaid::

   flowchart TD
       START_ST["runner.start()"]
       CRON_ST["crontab\nfires every interval, e.g. 10 min"]
       CORRUPT_ST["cj_corrupt.py\n① find files matching glob pattern\n② read original byte → save to ~/.chaos-jungle/cj.db\n③ flip bit via dd → overwrite byte in file"]
       FILE_ST["Your pipeline reads the file\nfile silently has wrong bytes"]
       WITH_ST["with integrity check\ndetects bad checksum\nlogs error, skips file"]
       WITHOUT_ST["without integrity check\nprocesses wrong data silently\nproduces incorrect result"]
       STOP_ST["runner.stop()\nremoves crontab — no new corruptions"]
       REVERT_ST["runner.revert()\nrestores every flipped byte from cj.db"]

       START_ST --> CRON_ST --> CORRUPT_ST --> FILE_ST
       FILE_ST --> WITH_ST
       FILE_ST --> WITHOUT_ST
       WITH_ST --> STOP_ST
       WITHOUT_ST --> STOP_ST
       STOP_ST --> REVERT_ST


Basic usage
-----------

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, StorageCorrupt, SSHTarget

   target = SSHTarget("10.0.0.5", user="ubuntu")

   # Corrupt *.pdb files in /data every 10 minutes
   fault = StorageCorrupt("*.pdb", "/data", interval="10m")

   runner = ChaosRunner(Scenario("storage-corrupt", [fault]), target)
   runner.start()
   # run pipeline — some .pdb files will have flipped bits
   runner.stop()    # crontab removed, no new corruptions
   runner.revert()  # all corrupted bytes restored from backup

Parameters:

.. list-table::
   :header-rows: 1
   :widths: 20 20 60

   * - Parameter
     - Default
     - Description
   * - ``pattern``
     - required
     - Glob pattern for files to corrupt: ``"*.pdb"``, ``"*.dat"``
   * - ``directory``
     - required
     - Absolute path to search: ``"/data"``, ``"/scratch/input"``
   * - ``interval``
     - ``"10m"``
     - Corruption frequency: ``"1m"``, ``"10m"``, ``"2h"``
   * - ``recursive``
     - ``True``
     - Search subdirectories recursively


Interval syntax
---------------

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Value
     - Meaning
   * - ``"30s"``
     - Every 30 seconds (aggressive — use for short experiments)
   * - ``"1m"``
     - Every minute
   * - ``"10m"``
     - Every 10 minutes (default — realistic for batch pipelines)
   * - ``"2h"``
     - Every 2 hours (slow drift — long-running stability tests)


What to observe
---------------

* **Integrity validation present** — your pipeline catches the corruption and
  logs an error before processing the file.  ``files_corrupted`` count rises,
  but no silent wrong result is produced.
* **No integrity validation** — the pipeline processes the corrupted file
  silently and produces a wrong result downstream.  This is the dangerous case.
* **Recovery speed** — how long does it take to detect, re-fetch, and
  re-process the corrupted file?

**Default metrics:** ``read_errors``, ``parse_errors``, ``write_errors``, ``checksum_errors``, ``corrupted_files``

Measuring with checksums
-------------------------

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, StorageCorrupt, SSHTarget

   target = SSHTarget("10.0.0.5", user="ubuntu")

   # Pre-compute a manifest of expected checksums
   _, manifest_out, _ = target.run("sha256sum /data/*.pdb")
   MANIFEST = {}
   for line in manifest_out.strip().splitlines():
       checksum, fname = line.split(None, 1)
       MANIFEST[fname.strip()] = checksum

   def workload():
       _, out, _ = target.run("sha256sum /data/*.pdb")
       corrupted = 0
       for line in out.strip().splitlines():
           checksum, fname = line.split(None, 1)
           if MANIFEST.get(fname.strip()) != checksum:
               corrupted += 1
       return {
           "files_corrupted": corrupted,
           "integrity_ok":    int(corrupted == 0),
       }

   runner = ChaosRunner(
       Scenario("storage-corrupt", [StorageCorrupt("*.pdb", "/data", interval="1m")]),
       target,
   )
   result = runner.measure(workload, n_baseline=3, n_fault=3)
   print(result.summary())
   # fault_mean("files_corrupted") should be > 0
   # fault_mean("integrity_ok") should be < 1.0


Revert behaviour
----------------

``revert()`` is separate from ``stop()`` and restores the original bytes:

.. code-block:: python

   runner.start()
   # ... experiment ...
   runner.stop()     # removes crontab, no new corruptions
   runner.revert()   # restores all corrupted bytes from cj.db

If you call ``runner.stop()`` without ``runner.revert()``, corrupted files
remain on disk.  Always call ``revert()`` after a storage experiment unless
you intentionally want to leave the corruption in place.


StorageCorruptImmediate
-----------------------

Corrupts a specific file at a specific byte offset instantly at ``start()``
time, without deploying scripts or setting up a crontab.  Useful for
injecting corruption at a precise moment during a test — for example, just
before an agent reads a model checkpoint.

The original file is backed up before modification; ``revert()`` restores
it exactly.  Requires ``sudo`` on the target (raw block write via ``dd``).

.. code-block:: python

   from chaos_jungle.faults.storage import StorageCorruptImmediate
   from chaos_jungle.targets import SSHTarget

   target = SSHTarget("10.0.0.5", user="ubuntu")

   fault = StorageCorruptImmediate("/data/model.bin", offset=1024, byte_count=32)
   runner = ChaosRunner(Scenario("corrupt-model", [fault]), target)

   runner.start()
   # model checkpoint is now corrupt at byte 1024–1056
   agent.load_model("/data/model.bin")
   runner.revert()   # file restored

Parameters:

.. list-table::
   :header-rows: 1
   :widths: 20 20 60

   * - Parameter
     - Default
     - Description
   * - ``file_path``
     - required
     - Absolute path to the file to corrupt
   * - ``offset``
     - ``0``
     - Byte offset at which to start writing random bytes
   * - ``byte_count``
     - ``16``
     - Number of bytes to overwrite with random data

**Default metrics:** ``read_errors``, ``parse_errors``, ``checksum_errors``, ``corrupted_files``


SQLiteCorrupt
-------------

Overwrites one full page of a SQLite database with random bytes using
``dd``.  SQLite detects the checksum mismatch on the next read and raises::

   sqlite3.DatabaseError: database disk image is malformed

Common targets: LangChain SQLite checkpointer, agent state databases,
local caches.

The file is backed up before modification; ``revert()`` restores it.

.. code-block:: python

   from chaos_jungle.faults.storage import SQLiteCorrupt
   from chaos_jungle.targets import SSHTarget

   target = SSHTarget("10.0.0.5", user="ubuntu")

   fault = SQLiteCorrupt("/var/agent/state.db")
   fault = SQLiteCorrupt("/var/agent/state.db", page=2, page_size=4096)

Parameters:

.. list-table::
   :header-rows: 1
   :widths: 20 20 60

   * - Parameter
     - Default
     - Description
   * - ``db_path``
     - required
     - Absolute path to the SQLite ``.db`` file
   * - ``page``
     - ``1``
     - Zero-based page index to corrupt (page 0 = header; page 1 = first data page)
   * - ``page_size``
     - ``4096``
     - SQLite page size in bytes (must match the database's ``page_size`` pragma)

**What to observe:**

* Does the agent catch ``DatabaseError`` and fall back to rebuilding state?
* Does a LangGraph / LangChain checkpointer silently return stale data?
* Is there an automatic recovery path (delete + re-create the DB)?

**Default metrics:** ``read_errors``, ``parse_errors``, ``query_errors``, ``corrupted_files``


Combined scenario — degraded worker node
-----------------------------------------

Combine storage corruption with network loss to simulate a node with both
a failing disk and a degraded network link:

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario, StorageCorrupt, NetworkLoss, SSHTarget

   target = SSHTarget("10.0.0.5", user="ubuntu")

   scenario = Scenario("degraded-worker", [
       StorageCorrupt("*.pdb", "/data", interval="5m"),
       NetworkLoss("2%"),
   ])
   runner = ChaosRunner(scenario, target)
   runner.start()
   # run pipeline
   runner.stop()
   runner.revert()   # always revert storage after the experiment

Dependencies
------------

Required on the target machine:

.. code-block:: bash

   sudo apt-get install -y python3 e2fsprogs coreutils inotify-tools
   pip3 install python-crontab

These are checked automatically by ``preflight()``::

   runner.preflight(target, auto_install=True)

See also
--------

* :ref:`guide-network` — network layer fault injection
* :ref:`guide-process` — process / service / container faults
* :ref:`guide-ssh` — SSHTarget setup and passwordless sudo
