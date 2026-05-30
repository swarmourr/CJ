Concepts
========

chaos-jungle is built around four simple abstractions.

Fault
-----

A **fault** is one type of failure to inject. It knows three things:

* ``start(target)`` — inject the fault
* ``stop(target)`` — remove the fault
* ``revert(target)`` — undo any persistent side effects

Available faults:

+---------------------+--------------------------------------+------------------+
| Class               | What it does                         | Underlying tool  |
+=====================+======================================+==================+
| ``NetworkDelay``    | Add artificial latency               | tc netem         |
+---------------------+--------------------------------------+------------------+
| ``NetworkLoss``     | Drop a percentage of packets         | tc netem         |
+---------------------+--------------------------------------+------------------+
| ``NetworkCorrupt``  | Corrupt a percentage of packets      | tc netem         |
+---------------------+--------------------------------------+------------------+
| ``NetworkDuplicate``| Duplicate a percentage of packets    | tc netem         |
+---------------------+--------------------------------------+------------------+
| ``StorageCorrupt``  | Flip bits in files at block level    | dd + cj_storage  |
+---------------------+--------------------------------------+------------------+

Target
------

A **target** is a machine. It knows how to run commands, transfer files,
and execute privileged operations.

+------------------+----------------------------------+
| Class            | How it connects                  |
+==================+==================================+
| ``LocalTarget``  | subprocess on local machine      |
+------------------+----------------------------------+
| ``SSHTarget``    | Paramiko SSH to remote machine   |
+------------------+----------------------------------+
| ``HTTPTarget``   | HTTP requests to chaos daemon    |
+------------------+----------------------------------+

Scenario
--------

A **scenario** is just a named list of faults. No logic — pure data.

.. code-block:: python

   scenario = Scenario("net-chaos", faults=[
       NetworkDelay("100ms"),
       NetworkLoss("5%"),
   ])

ChaosRunner
-----------

The **runner** orchestrates the lifecycle: preflight → start → stop → revert.
It also writes everything to the SQLite session database.

Usage modes
~~~~~~~~~~~

+------------------+---------------------------------------------------+
| Mode             | How                                               |
+==================+===================================================+
| Decorator        | ``@chaos(...)``                                   |
+------------------+---------------------------------------------------+
| Context manager  | ``with chaos_session(...) as s:``                 |
+------------------+---------------------------------------------------+
| Explicit         | ``runner.start()`` / ``runner.stop()``            |
+------------------+---------------------------------------------------+
| Separate         | ``runner.start()`` returns; attach from elsewhere |
+------------------+---------------------------------------------------+

Database
--------

chaos-jungle writes two SQLite databases:

* ``~/.chaos-jungle/chaos_jungle.db`` — **new**: sessions, faults, events
* ``~/.chaos-jungle/cj.db`` — **existing** (untouched): storage bit-flip records

The ``chaos_jungle.db`` schema:

.. code-block:: sql

   sessions (id, name, started_at, stopped_at, status)
   faults   (id, session_id, kind, parameters, started_at, stopped_at)
   events   (id, session_id, fault_id, timestamp, message)
