Changelog
=========

1.5.0 (2026-07-06)
------------------

**New features**

* **``Hypothesis``** (``chaos_jungle.hypothesis``) — declare the expected
  behaviour of a system under fault *before* running, verify *after*.  Build
  with chained assertion methods and pass via ``hypothesis=`` to
  ``runner.measure()``.  The result is available on
  ``MeasurementResult.hypothesis_result`` and printed automatically.

  Assertion methods: ``.max_delta_pct()``, ``.max_fault_value()``,
  ``.min_fault_value()``, ``.no_regression()``, ``.max_absolute_delta()``.

  .. code-block:: python

     from chaos_jungle import Hypothesis

     h = (
         Hypothesis("handles 200ms delay gracefully")
         .max_delta_pct("duration_s", 50)
         .max_fault_value("error_rate", 0.05)
         .no_regression("completion_rate")
     )
     result = runner.measure(workload, n_baseline=5, n_fault=5, hypothesis=h)
     print(result.hypothesis_result.summary())

* **``ChaosScheduler``** (``chaos_jungle.scheduler``) — run a chaos experiment
  on a recurring schedule in a background thread.  Supports interval-based
  scheduling (``every("1h")``) and daily cron-style scheduling
  (``daily_at("02:00")``).  Results are accessible via ``scheduler.results``
  and callbacks registered with ``on_result()`` / ``on_error()``.

  .. code-block:: python

     from chaos_jungle import ChaosScheduler

     scheduler = (
         ChaosScheduler(scenario, target, workload=workload)
         .every("1h")
         .on_result(lambda r: print(r.summary()))
     )
     scheduler.start()

* **Observability exporters** (``chaos_jungle.exporters``) — push
  :class:`~chaos_jungle.runner.MeasurementResult` metrics to external systems
  after an experiment completes.

  - :class:`~chaos_jungle.exporters.PrometheusExporter` — push baseline,
    fault, and delta metrics to a Prometheus Pushgateway
    (``pip install prometheus-client``).
  - :class:`~chaos_jungle.exporters.DatadogExporter` — send metrics via
    DogStatsD to a local Datadog Agent (``pip install datadog``).
  - :class:`~chaos_jungle.exporters.WebhookExporter` — POST JSON to any HTTP
    endpoint, no extra dependencies.

  .. code-block:: python

     from chaos_jungle.exporters import PrometheusExporter, WebhookExporter

     result = runner.measure(workload, n_baseline=5, n_fault=5)
     PrometheusExporter(gateway="http://pushgateway:9091").export(result)
     WebhookExporter(url="https://hooks.example.com/chaos").export(result)

**Documentation**

* **Hypothesis guide** (:ref:`guide-hypothesis`) — assertion methods, CI gate
  pattern, ``HypothesisResult`` fields.

* **Scheduler guide** (:ref:`guide-scheduler`) — interval and daily schedules,
  callbacks, accessing collected results.

* **Exporters guide** (:ref:`guide-exporters`) — PrometheusExporter,
  DatadogExporter, WebhookExporter, custom exporter pattern.

* **index.rst** — new *Automation & Observability* toctree section.

----

1.4.0 (2026-06-27)
------------------

**New features**

* **``ChaosFuzzer``** (``chaos_jungle.fuzzing``) — replaces ``fuzz_scenarios()``
  as the primary fuzzing API.  Follows the same pattern as ``ChaosRunner``:
  construct with a source and target, call ``.measure()``.  Two modes:

  - **Explicit pool** — user provides fault instances; CJ picks random subsets.
  - **Category-based** — user names categories (``"system"``, ``"llm"``,
    ``"application"``); CJ picks faults *and* randomizes their parameters.

  Key improvement: a **single shared baseline** is measured once before all
  experiments.  Every :class:`~chaos_jungle.runner.MeasurementResult` in the
  returned list carries the same baseline — comparisons are consistent and
  no redundant baseline runs are wasted.

  ``fuzz_scenarios()`` and ``summarise_fuzz()`` remain as thin backward-compat
  wrappers.

* **``inject(measure=)``** — ``inject()`` now accepts an optional ``measure``
  parameter (``True`` or a list of metric names) that collects lightweight
  metrics from the ``with`` block without requiring a workload function.
  Returns an :class:`~chaos_jungle.intercept.InjectResult` via the context
  variable with: ``duration_s``, ``http_calls``, ``http_errors``,
  ``memory_mb`` (psutil), ``cpu_percent`` (psutil).

  .. code-block:: python

     with inject(RateLimit(after_n=2), measure=True) as m:
         agent.run("Book a flight")
     print(m.duration_s, m.http_calls, m.http_errors)

* **ScenarioRegistry internalized** — CJ now manages the scenario lifecycle
  automatically.  ``ChaosRunner`` registers, starts, and closes every scenario;
  users observe status via CLI or dashboard only.  The ``ScenarioRegistry``
  class is no longer part of the public API, and target methods
  ``push_scenario`` / ``run_scenario`` / ``scenario_status`` are now private
  (prefixed ``_``).

* **Dashboard — Scenarios Registry view** — new *Scenarios* sidebar tab shows
  every experiment's lifecycle status (``pending`` → ``running`` →
  ``done`` / ``failed``) with filtering by target type and status.  Backed by
  a new ``GET /api/scenarios`` endpoint.

**Documentation**

* **Fuzzing guide rewritten** (:ref:`guide-fuzzing`) — ``ChaosFuzzer`` is now
  the primary API; shared baseline, category mode, and backward-compat wrapper
  all documented.

* **Intercept guide updated** (:ref:`guide-intercept`) — new *Optional
  measurement* section documents ``inject(measure=True/[...])`` and
  ``InjectResult``.

* **Concepts guide updated** (:doc:`concepts`) — ChaosRunner section now
  lists all four usage styles (start/stop, context manager, decorator,
  measure) with numbered headings.

* **Registry guide rewritten** (:ref:`guide-registry`) — the guide now
  positions the ScenarioRegistry as an internal lifecycle index managed
  entirely by CJ.  Only CLI commands (``chaos-jungle scenarios list/status/watch``)
  are documented; no Python API is exposed.

* **SSH and HTTP target guides simplified** (:ref:`guide-ssh`, :ref:`guide-http`)
  — scenario tracking sections reduced to a single callout note; the full
  push / run / watch code examples are removed.

* **LLM Scenario Guide removed** — the standalone scenario cookbook
  (S01–S11, R01–R10) has been removed from the documentation.  Individual
  fault patterns are covered by the dedicated fault guides; the runnable
  scenario files remain in the ``scenarios/`` folder.

----

1.3.0 (2026-06-18) — agent-chaos parity
-----------------------------------------

**New features**

* **Per-call fault targeting in** ``inject()``

  Three new parameters give surgical control over which LLM calls are affected:

  - ``after_n_calls=N`` — skip the first N calls before activating the fault.
  - ``only_model="gpt-4"`` — only fire when the request targets a model whose
    name contains this substring (case-insensitive).
  - ``only_tool="search"`` — only fire when the request contains a tool result
    whose name matches.

  .. code-block:: python

     with inject(RateLimit(after_n=0), after_n_calls=2, only_model="gpt-4o"):
         run_pipeline()   # first 2 calls pass, then gpt-4o calls get 429

* **``ToolMutate`` behavior** (``chaos_jungle.intercept``)

  Silently rewrites ``role: "tool"`` messages before the LLM sees them.
  The HTTP status stays 200 and no exception is raised — the hardest fault
  to detect.  Modes: ``"garble"``, ``"empty"``, ``"null"``, ``"wrong_type"``,
  or a custom ``replacement`` value.

* **``PromptInjection`` behavior** (``chaos_jungle.intercept``)

  Appends adversarial text to outgoing LLM messages.  Targets: ``"user"``,
  ``"system"``, or ``"all"``.  Tests injection resistance without modifying
  agent code.

* **``Evaluator`` protocol** (``chaos_jungle.judge``)

  A ``@runtime_checkable`` ``Protocol`` that any object with a compatible
  ``score(question, context, response) -> JudgeScore`` method satisfies.
  ``LLMJudge`` already conforms.  Enables DeepEval, Pydantic Evals, or any
  custom evaluator via the same ``evaluator=`` parameter in
  ``ChaosRunner.measure()``.

* **``fuzz_scenarios()``** (``chaos_jungle.fuzzing``, new module)

  Randomly combines faults from a pool and measures each combination.
  Parameters: ``n_combinations``, ``max_faults_per_run``, ``seed``,
  ``stop_on_first_failure``.  Includes ``summarise_fuzz()`` for tabular output.

* **``ConversationScenario`` + ``Turn``** (``chaos_jungle.conversation``, new module)

  Multi-turn agent conversation runner with per-turn fault injection.
  ``Turn`` supports static or dynamic (lambda) inputs, ``chaos=``,
  ``chaos_after_n=``, and ``expected=`` substring assertions.
  ``ConversationScenario.run(agent)`` executes the full conversation and
  returns a ``list[TurnResult]``.

**New documentation**

* :ref:`guide-conversation` — multi-turn conversation fault injection guide
* :ref:`guide-fuzzing` — fault fuzzer guide
* :ref:`guide-intercept` — updated with ``ToolMutate``, ``PromptInjection``,
  per-call targeting, and the ``modify_request`` hook
* :ref:`guide-judge` — updated with Evaluator protocol and DeepEval adapter example

----

1.2.0 (2026-06-16)
------------------

**New features**

* **LangSmith-style dashboard** — ``chaos_jungle.dashboard`` rebuilt with a
  full six-tab UI covering every fault type (network, resource, process,
  storage, state, LLM/MCP, skill, semantic, GPU) and LLM call tracing:

  - **Overview** — KPI row for fault injection + LLM telemetry when data
    present; fault-type distribution bar chart; session status donut; recent
    experiments table.
  - **Experiments** — filterable table with per-fault category badges
    (color-coded by fault kind) and status chips.
  - **LLM Calls** — 9-metric summary bar; 12-column table with expandable rows
    showing full prompt/response text inline (LangSmith-style), metadata grid,
    blocked/modified badges.
  - **Monitoring** — fault timeline bar, category donut; LLM section with
    latency per call, token usage stacked bar, cumulative cost line, call
    outcome donut (all Chart.js 4).
  - **System** — 15 tool checks (stress-ng, redis-cli, psql, docker,
    nvidia-smi, iostat, ping, etc.).
  - **Session drawer** — new *LLM Calls* sub-tab per session with 6-metric
    summary and inline prompt/response.

* **New API endpoints** (``/api/llm_calls``, ``/api/session/{id}/llm_calls``,
  ``/api/monitoring``) and ``"category"`` field on every fault in
  ``/api/sessions`` and ``/api/session/{id}``.

* **``controller.py`` preflight fix** — ``_add_revert`` bridge now absorbs
  ``auto_install`` kwarg passed by ``ChaosRunner``, preventing ``TypeError``
  on preflight.

----

1.1.0 (2026-06-16)
------------------

**New features**

* **Full LLM call telemetry** — the ``llm_calls`` table expanded to 32
  columns.  New fields captured per call:

  - ``fault_name``, ``was_blocked``, ``was_modified`` — injection context
  - ``total_tokens``, ``tokens_per_second`` — throughput
  - ``ttft_s`` — time-to-first-token for streaming calls
  - ``request_size_bytes``, ``response_size_bytes``, ``response_length_chars``
  - ``message_count``, ``tool_count``, ``response_tool_calls`` — request shape
  - ``is_streaming``, ``temperature``, ``max_tokens_requested``
  - ``system_fingerprint``, ``rate_limit_remaining_requests``,
    ``rate_limit_remaining_tokens``

* **Auto-cost for all faults** — ``_MODEL_PRICING`` table in the proxy
  (OpenAI, Anthropic, Google, Ollama models) auto-computes ``cost_usd`` for
  every forwarded call, not just ``LLMBudgetExceeded`` experiments.

* **Blocked call recording** — all 11 early-return paths in the proxy (
  unavailable, rate-limit, budget-exceeded, timeout, tool-fault, skill-*)
  now call ``_record_llm_call()`` with ``was_blocked=1``.

* **Streaming TTFT** — ``_stream_interrupt()`` captures time-to-first-token
  on the first SSE ``data:`` event and stores it as ``ttft_s``.

* The proxy accepts ``--db-path``, ``--session-id``, ``--phase`` flags;
  ``_LLMProxyFault.start()`` passes them automatically.

----

1.0.0 (2026-06-16)
------------------

**New features**

* **Native LLM call capture** — the LLM proxy now records every forwarded
  API call directly into the session database with zero extra dependencies
  and no agent-code changes:

  - New ``llm_calls`` table in ``SessionDB`` stores per-call:
    ``model``, ``prompt_tokens``, ``completion_tokens``, ``cost_usd``,
    ``finish_reason``, ``prompt_text``, ``response_text``, ``latency_s``,
    ``http_status``.
  - ``SessionDB.record_llm_call()`` / ``SessionDB.get_llm_calls()`` — new
    public methods; ``export_session()`` includes the ``llm_calls`` list.
  - ``MeasurementResult.llm_calls`` — ``list[dict]`` of all captured calls
    for a ``measure()`` run; empty when no LLM proxy is active.
  - ``MeasurementResult.summary()`` extended with an **LLM calls** table
    showing per-call token counts, cost, latency, and finish reason plus
    aggregate totals.
  - The proxy accepts three new CLI flags: ``--db-path``, ``--session-id``,
    ``--phase``; ``_LLMProxyFault.start()`` passes these automatically when
    a ``LoggingTarget`` (from ``ChaosRunner``) is present.

----

0.9.0 (2026-06-16)
------------------

**New features**

* **Auto-collected fault metrics** — every fault class now declares
  ``default_metrics``, a list of the metrics most relevant to its failure mode.
  ``ChaosRunner.measure()`` accepts two new parameters to enable automatic
  collection:

  - ``strategy=CollectStrategy.SNAPSHOT`` — collect at three fixed points:
    baseline (before fault), fault (while active), and recovery (immediately
    after stop).
  - ``strategy=CollectStrategy.RECOVERY(recovery_window_s=60)`` — same as
    SNAPSHOT plus a configurable post-fault time-series window sampled every
    10 s.
  - ``metric_set=MetricSet.DEFAULT`` — use all metrics declared in
    ``fault.default_metrics``; customise with ``.exclude()``, ``.add()``, or
    ``MetricSet.only()``.

* **``MetricSet``** (``chaos_jungle.metrics.MetricSet``) — immutable, fluent
  API for controlling which fault metrics are active:

  .. code-block:: python

     MetricSet.DEFAULT                              # all fault defaults
     MetricSet.DEFAULT.exclude("swap_used_mb")     # drop specific metrics
     MetricSet.DEFAULT.add("inode_used")            # add extras
     MetricSet.only("error_rate", "duration_s")    # ignore defaults entirely

* **``CollectStrategy``** (``chaos_jungle.metrics.CollectStrategy``) — controls
  collection *frequency*:

  .. code-block:: python

     CollectStrategy.SNAPSHOT                         # 3-point, instant
     CollectStrategy.RECOVERY(recovery_window_s=120)  # + post-fault window

* **``CollectedMetrics``** (``chaos_jungle.metrics.CollectedMetrics``) — new
  result dataclass stored in ``MeasurementResult.collected_metrics``:

  - ``.baseline``, ``.fault``, ``.recovery`` — ``dict[str, MetricSummary]``
    with ``.avg``, ``.min``, ``.max``, ``.p50``, ``.p99``, ``.series``
  - ``.delta`` — ``fault.avg - baseline.avg`` per metric
  - ``.active_metrics`` — the resolved list of metric names collected

* **System metric auto-collectors** (``metrics/strategy.py``) — shell commands
  mapped to metric names for transparent collection on any target:
  ``cpu_percent``, ``memory_mb``, ``swap_used_mb``, ``rtt_ms``,
  ``disk_used_bytes``, ``inode_used``, ``iops``,
  ``gpu_util_percent``, ``gpu_memory_mb``, ``gpu_clock_mhz``.

* **``LLMBudgetExceeded``** fault — tracks per-request cost in USD via
  the LLM proxy.  Rejects with HTTP 402 once ``max_cost_usd`` is reached.
  Supports per-model pricing via ``MODEL_PRICING`` table (OpenAI, Anthropic,
  Google, Ollama) or explicit ``input_price_per_1k`` / ``output_price_per_1k``
  overrides.

* **``default_metrics`` on all faults** — 30 fault classes across all
  categories (network, resources, process, storage, state, GPU, LLM, MCP,
  skill-file) now declare fault-specific ``default_metrics``.

* **``preflight.py`` extended** — ``PKG_MAP`` and ``PKG_TO_BIN`` updated with
  entries for metric collection tools:
  ``sysstat`` (iostat), ``iputils`` (ping), ``procps`` (pgrep),
  ``nvidia-utils`` (nvidia-smi), ``docker-cli``, ``redis-tools``,
  ``postgresql-client``, ``stress-ng``.

**Documentation**

* :ref:`guide-metrics` updated — new *Auto-collected fault metrics* section
  covering ``MetricSet``, ``CollectStrategy``, ``CollectedMetrics``, system vs.
  workload metrics, and per-fault-category default metric tables.
* :ref:`guide-measurement` updated — new *Auto-collected fault metrics* section
  under ``measure()``; ``MeasurementResult`` API extended with
  ``collected_metrics``; summary table updated.
* API reference (``docs/api/metrics.rst``) extended with ``automodule`` entries
  for ``metric_set``, ``strategy``, and ``schema``.

----

0.8.0 (2026-06-05)
------------------

**Documentation**


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
