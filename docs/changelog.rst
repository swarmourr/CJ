Changelog
=========

0.8.0 (2026-06-05)
------------------

**Documentation**

* **LLM Scenario Guide** — new full guide at :ref:`guide-scenarios` covering
  all 21 runnable scenarios (S01–S11 and R01–R10).  Each entry documents
  *what* the scenario does, *why* it maps to a real production failure, *how*
  to run it with a copy-paste code snippet, and *what results* to expect with
  an annotated signal / problem table.

* Guide wired into the Sphinx sidebar under **LLM / AI Faults** — visible on
  every page after a clean full rebuild.

* Quick-reference table at the end of the guide maps every production failure
  pattern to the correct scenario.

**Bug fixes**

* **R03 provider failover** — secondary ``call_llm`` now passes
  ``base_url=OLLAMA_API_BASE`` explicitly so it bypasses the
  ``OPENAI_BASE_URL`` env var that the fault proxy sets.  Previously the
  secondary call routed through the same broken proxy as the primary and also
  returned 503.

* **R03 failover verdict** — ``fallback_worked`` now requires the secondary
  reply to be clean (not ``[ERROR]``), not just that ``used == "secondary"``.
  Three-state verdict: succeeded / switched but failed / did not fall back.

* **Sphinx incremental build gap** — existing guide pages did not include the
  new sidebar entry after an incremental rebuild.  Fixed by always doing a
  clean rebuild (``rm -rf docs/_build/html``) when adding new toctree entries.

----

0.7.0 (2026-06-04)
------------------

**New features**

* **Baseline measurements in all R-scenarios** — every realistic scenario now
  runs a warm-up call (discarded) followed by 2–3 timed baseline calls before
  any fault is activated.  The clean call duration is printed as a reference
  row so fault-induced deltas are immediately visible:

  - R01, R02, R08, R09 — warm-up + 2 timed calls; ``baseline_avg_s`` added to
    return dict
  - R10 — extra ``0.0 (base)`` row printed at the top of the latency ramp table
  - R03, R05, R06 — already had baselines; R04 / R07 use answer-quality
    baselines (timing not meaningful for semantic scenarios)

* **Realistic scenarios subfolder** (``scenarios/realistic/``) — all ten
  R-scenarios consolidated under one path; ``run_realistic.py`` updated.

**Bug fixes**

* **Rate-limit detection** — ``_is_rate_limited()`` now checks for all three
  surface forms of an HTTP 429 from the OpenAI SDK:
  ``"429" in reply or "raise_for_status" in reply or "RateLimitError" in reply``.
  Previously checks that only looked for ``"429"`` reported 0 rate-limited
  calls because the SDK raises ``RuntimeError: Cannot call 'raise_for_status'``
  when a mock 429 response is returned by the intercept layer.  Fixed in
  R01, R09, and all three pytest test files.

----

0.6.0 (2026-05-27)
------------------

**New features**

* **Realistic multi-fault scenarios (R01–R10)** — ten production failure
  patterns, each combining multiple simultaneous faults against a local
  Ollama model:

  - ``R01`` API Overload: ``Latency(2s)`` + ``RateLimit(after_n=3)``
  - ``R02`` Flaky Provider: ``Jitter(0.1, 2.5)`` + ``Unavailable(p=0.25)``
  - ``R03`` Provider Failover: ``LLMUnavailable`` on primary + fallback to secondary
  - ``R04`` Poisoned RAG: ``SemanticCorrupt(rag_poison)`` via proxy
  - ``R05`` Prompt Injection Under Load: ``SemanticCorrupt(inject_distractor)`` + ``Latency(1.5s)``
  - ``R06`` Token Starvation Cascade: ``LLMTokenStarvation(12)`` + ``Latency(1.5s)``
  - ``R07`` Double Semantic Attack: chained entity-swap + rag-poison proxies
  - ``R08`` Cascading Outage: ``LLMLatency`` door-cycling + full ``Unavailable``
  - ``R09`` Rate Limit Exhaustion: gradual and immediate quota exhaustion variants
  - ``R10`` Progressive Overload: latency ramp (0.1 s → 8.0 s) with fixed SLA

* **``run_realistic.py``** — runner for all R-scenarios with numbered subset
  support (``python scenarios/run_realistic.py 01,03,09``).

* **pytest integration** (``scenarios/pytest/``):

  - ``@pytest.mark.chaos(fault, ...)`` decorator activates ``inject()`` for
    the duration of a test with zero boilerplate
  - ``urls=["host"]`` parameter scopes the fault to specific hostnames
  - Built-in fixtures: ``llm_call``, ``assert_ok``, ``ollama_model``
  - Four test files: ``test_api_faults.py``, ``test_realistic_scenarios.py``,
    ``test_provider_resilience.py``, ``test_quality_gates.py``
  - ``conftest.py`` registers the plugin and configures the Ollama model fixture
  - ``pytest.ini`` suppresses ``PytestUnknownMarkWarning`` for the chaos marker

* **Scenarios folder reorganised** into category subfolders:

  .. code-block:: text

     scenarios/
       api/           S01–S05  (API transport faults)
       content/       S06–S09  (hallucination, stream, tokens, semantic)
       measurement/   S10–S11  (statistical delta, multi-model)
       realistic/     R01–R10  (multi-fault production patterns)
       pytest/                 (pytest marker integration)

  All files updated with ``sys.path.insert(0, parent)`` so ``helpers``
  imports resolve correctly from sub-directories.

0.5.0 (2026-06-02)
------------------

**New features**

* **Process, service & container faults** (``chaos_jungle.faults.process``):

  - ``ProcessKill(pattern, signal)`` — kills OS processes matching a command
    pattern via ``pkill -f``; captures killed PIDs for reporting
  - ``ServiceFault(service, action)`` — stops, restarts, kills, or masks a
    systemd service; auto-restores on ``stop()``
  - ``ContainerKill(container, action)`` — kills, stops, pauses, or removes
    a Docker container; auto-restores on ``stop()``

* **Resource exhaustion faults** (``chaos_jungle.faults.resources``):

  - ``DiskFull(path, size_mb)`` — fills a filesystem via ``dd if=/dev/zero``;
    fill file removed on ``stop()``
  - ``CPUStress(cores, duration_s)`` — saturates N CPU cores via
    ``stress-ng --cpu``; killed on ``stop()``
  - ``MemoryStress(mb, duration_s)`` — allocates N MiB of RAM via
    ``stress-ng --vm``; killed on ``stop()``
  - ``IOStress(workers, duration_s, path)`` — generates disk I/O load via
    ``stress-ng --hdd``; killed on ``stop()``

**Documentation**

* New guide: :ref:`guide-process` — process / service / container faults
* New guide: :ref:`guide-resources` — CPU / memory / disk / I/O exhaustion
* API reference updated: ``faults.process``, ``faults.resources``

0.4.0 (2026-05-31)
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

0.3.0 (2026-05-28)
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

0.2.0 (2026-05-25)
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

0.1.0 (2026-05-22)
------------------

* Initial release
* Faults: ``NetworkDelay``, ``NetworkLoss``, ``NetworkCorrupt``, ``NetworkDuplicate``, ``StorageCorrupt``
* Targets: ``LocalTarget``, ``SSHTarget``, ``HTTPTarget``
* Usage modes: decorator, context manager, explicit, separate
* Chaos daemon (FastAPI) for HTTP target mode
* Unified SQLite session database (``chaos_jungle.db``)
* CLI: ``start``, ``stop``, ``status``, ``list``, ``export``, ``daemon``
* Sphinx documentation with furo theme
