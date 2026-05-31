Changelog
=========

0.2.0 (2026-05-31)
------------------

**New features**

* ``@chaos_measure`` decorator — runs a function under chaos, auto-saves
  its return dict as session results, optionally captures stdout
* ``runner.record_result(metrics)`` — attach arbitrary JSON metrics to a
  session; metrics appear in the dashboard and in CSV exports
* ``results`` table in ``chaos_jungle.db`` — stores workflow outcome
  metrics linked to each session
* ``chaos-jungle export`` rewrite — writes a named file to disk (JSON
  or CSV); CSV has flattened metrics columns; ``--session`` omitted
  exports all sessions; ``--output`` for custom path
* ``chaos-jungle fetch`` — downloads files from a remote SSH host via
  SFTP; auto-generates ``chaos_sessions.csv`` from the fetched DB
* ``chaos-jungle dashboard`` command — opens the experiment tracking
  dashboard in the browser (FastAPI, no extra dependencies)
* ``LoggingTarget`` — transparent proxy that logs every ``tc``/``dd``/
  shell command run on the target to the session event log
* ``chaos-jungle suite`` CLI command — run an ``ExperimentSuite`` from
  a YAML config file

**Bug fixes**

* ``SSHTarget.get()`` / ``LocalTarget.get()`` — file download API now
  fully implemented across all targets
* Dashboard ``/api/sessions`` 500 error — fixed ``sqlite3.Row.get()``
  call (must use ``dict(row)`` first)
* Dashboard JS syntax error — fixed unescaped newlines in template
  literals embedded in Python triple-quoted strings

0.1.0 (2026-05-29)
------------------

* Initial release
* Faults: ``NetworkDelay``, ``NetworkLoss``, ``NetworkCorrupt``, ``NetworkDuplicate``, ``StorageCorrupt``
* Targets: ``LocalTarget``, ``SSHTarget``, ``HTTPTarget``
* Usage modes: decorator, context manager, explicit, separate
* Chaos daemon (FastAPI) for HTTP target mode
* Unified SQLite session database (``chaos_jungle.db``)
* CLI: ``start``, ``stop``, ``status``, ``list``, ``export``, ``daemon``
* Sphinx documentation with furo theme
