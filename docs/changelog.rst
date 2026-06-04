Changelog
=========

0.4.0 (2026-06-04)
------------------

**New features**

* **SemanticCorrupt** — injects semantic-layer faults into LLM request
  payloads without breaking HTTP or JSON structure.  Four modes:

  - ``entity_swap`` — replaces named entities in context messages
  - ``context_truncate`` — cuts context to ~50 %
  - ``inject_distractor`` — inserts a contradictory instruction mid-context
  - ``rag_poison`` — appends false statements to retrieved context chunks

* **State-layer faults** (``chaos_jungle.faults.state``):

  - ``RedisStateCorrupt`` — mutates Redis keys matching a glob pattern via
    ``redis-cli``; supports nullify / delete / negate / type_mismatch /
    inject mutations; auto-reverts on ``stop()``
  - ``JsonStateCorrupt`` — mutates a dot-path field in a JSON checkpoint
    file; supports the same five mutation modes
  - ``PostgresStateCorrupt`` — runs a parameterised ``UPDATE`` via ``psql``;
    supports an optional ``condition`` (WHERE clause)

* **LLMJudge evaluator** (``chaos_jungle.judge``):

  - ``LLMJudge.score()`` returns a ``JudgeScore`` with four quality metrics:
    ``faithfulness``, ``hallucination``, ``coherence``, ``guardrail_violation``
  - Works with any OpenAI-compatible endpoint (OpenAI, Ollama, Azure)
  - Integrated into ``ChaosRunner.measure()`` via the ``evaluator=`` parameter
  - ``MeasurementResult`` gains ``judge_baseline``, ``judge_fault``,
    ``judge_delta`` fields and ``passed_quality()`` helper
  - ``average_scores()`` helper to aggregate multiple ``JudgeScore`` objects

* **Full command output capture**:

  - New ``commands`` table in ``SessionDB`` stores untruncated ``stdout``
    and ``stderr`` for every command run through ``LoggingTarget``
  - ``SessionDB.record_command()`` / ``get_commands()`` API
  - ``ChaosRunner.commands(failed_only=False)`` convenience accessor
  - ``export_session()`` now includes the ``commands`` list

* New guides: :ref:`guide-semantic`, :ref:`guide-state`, :ref:`guide-judge`
* Ollama guide updated with macOS IPv6 note and semantic/judge examples

**Bug fixes**

* **Proxy base URL** — the fault proxy now sets
  ``OPENAI_BASE_URL=http://127.0.0.1:<port>/v1`` (previously omitted
  ``/v1``).  Without ``/v1``, the openai Python SDK routed requests to
  ``/chat/completions`` instead of ``/v1/chat/completions``, causing 404
  on Ollama and all non-OpenAI-hosted endpoints.

0.3.0 (2026-05-31)
------------------

**New features**

* **LLM agent fault injection** — five new fault types that intercept HTTP
  traffic between an LLM agent and its API endpoint via a stdlib-only
  local proxy.  No agent code changes required:

  - ``LLMLatency`` — artificial per-request delay
  - ``LLMRateLimit`` — HTTP 429 after *n* requests
  - ``LLMTimeout`` — hang every connection for *timeout_s* seconds
  - ``LLMResponseCorrupt`` — truncate / empty / invalid-JSON responses
  - ``LLMUnavailable`` — always return HTTP 503

* Works with OpenAI, Anthropic, Azure OpenAI, Ollama, and any
  OpenAI-compatible endpoint; configurable via ``upstream`` and
  ``base_url_env`` parameters.
* ``examples/llm_agent.py`` — one runnable example per fault type plus
  a ``@chaos_measure`` integration example.
* New guide: :ref:`guide-llm` — fault reference, failure taxonomy, and
  multi-fault suite patterns.

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
