.. _guide-metrics:

Metrics
========

chaos-jungle provides two measurement systems:

* **Infrastructure metrics** (``Metric`` classes) — measure system-level
  signals: ping latency, TCP connections, file integrity, custom commands.
  Used with ``@chaos_measure``.
* **AI quality metrics** (``LLMJudge``) — score LLM response quality:
  faithfulness, hallucination, coherence, guardrail compliance.
  Used with ``runner.measure(evaluator=judge)``.

----

Infrastructure metrics
-----------------------

Built-in metric classes
~~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 25 35 40

   * - Class
     - Parameters
     - Keys returned
   * - ``PingLatency``
     - ``host``, ``count=5``
     - ``avg_ms``, ``min_ms``, ``max_ms``, ``samples``
   * - ``CommandMetric``
     - ``cmd``, ``parse``, ``name``
     - whatever your ``parse`` fn returns
   * - ``FileIntegrity``
     - ``pattern``, ``directory``, ``checksum_file=None``
     - ``files_found``, ``files_corrupted``
   * - ``ScriptMetric``
     - ``name``, ``script`` or ``remote_script``
     - output of your script (JSON or key=value)

Quick example:

.. code-block:: python

   from chaos_jungle.decorators import chaos_measure
   from chaos_jungle import NetworkDelay
   from chaos_jungle.metrics import PingLatency, CommandMetric

   @chaos_measure(
       NetworkDelay("100ms", jitter="10ms"),
       metrics=[
           PingLatency("8.8.8.8", count=5),
           CommandMetric(
               "ss -tn state established | wc -l",
               parse=lambda out: {"open_connections": int(out.strip())},
               name="tcp",
           ),
       ],
       scenario_name="network-impact",
   )
   def run_experiment():
       run_pipeline()
       return {"jobs_completed": 42}

   summary  = run_experiment()
   baseline = summary["metrics"]["baseline"]
   chaos    = summary["metrics"]["chaos"]

   print(f"Latency:     {baseline['ping_avg_ms']} ms → {chaos['ping_avg_ms']} ms")
   print(f"Connections: {baseline['tcp_open_connections']} → {chaos['tcp_open_connections']}")

The result stored in the database:

.. code-block:: python

   {
       "baseline_ping_avg_ms":           0.2,
       "baseline_ping_min_ms":           0.1,
       "baseline_ping_max_ms":           0.4,
       "baseline_ping_samples":          5,
       "baseline_tcp_open_connections":  12,
       "chaos_ping_avg_ms":             108.6,
       "chaos_ping_min_ms":             100.1,
       "chaos_ping_max_ms":             115.3,
       "chaos_ping_samples":             5,
       "chaos_tcp_open_connections":     8,
       "fn_jobs_completed":             42,
   }

Custom metrics
~~~~~~~~~~~~~~

**Option 1 — ``@metric`` decorator** (recommended):

.. code-block:: python

   from chaos_jungle.metrics import metric

   @metric("throughput")
   def my_throughput(_):
       import json, urllib.request
       with urllib.request.urlopen("http://localhost:9100/metrics") as r:
           data = json.loads(r.read())
       return {"mbps": data["bits_per_second"] / 1e6}

   @chaos_measure(NetworkDelay("100ms"), metrics=[my_throughput])
   def run():
       run_pipeline()

The function receives the active ``target`` as its only argument and must
return a plain ``dict``.

**Option 2 — ``ScriptMetric``** (run a shell/Python script on the target):

.. code-block:: python

   from chaos_jungle.metrics import ScriptMetric

   m = ScriptMetric("app", script="./scripts/measure_app.sh")

   @chaos_measure(NetworkDelay("100ms"), metrics=[m])
   def run():
       run_pipeline()

The script must print JSON or ``key=value`` to stdout:

.. code-block:: bash

   # measure_app.sh
   echo '{"error_rate": 0.02, "throughput_mbps": 850.3}'

``ScriptMetric`` parameters:

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Parameter
     - Default
     - Description
   * - ``name``
     - required
     - Metric name prefix — keys become ``<name>_<key>``
   * - ``script``
     - ``""``
     - Local path to upload and run on the target
   * - ``remote_script``
     - ``""``
     - Path already on the target (mutually exclusive with ``script``)
   * - ``interpreter``
     - ``"auto"``
     - ``"bash"`` / ``"python3"`` / ``""`` — auto-detected from extension
   * - ``parse``
     - ``"auto"``
     - ``"json"`` / ``"keyvalue"`` / ``"auto"`` (tries JSON first)
   * - ``extra_args``
     - ``""``
     - Extra arguments appended to the script invocation

**Option 3 — subclass** (full control):

.. code-block:: python

   from chaos_jungle.metrics import Metric
   from chaos_jungle.targets.base import Target

   class OpenConnections(Metric):
       name = "tcp"

       def collect(self, target: Target) -> dict:
           _, out, _ = target.run("ss -tn state established | wc -l")
           try:
               return {"open_connections": int(out.strip())}
           except ValueError:
               return {"open_connections": 0}

   class RetransmitRate(Metric):
       name = "tcp_retrans"

       def collect(self, target: Target) -> dict:
           _, out, _ = target.run(
               "awk '/^Tcp:/{getline; print $12}' /proc/net/snmp"
           )
           try:
               return {"retransmits": int(out.strip())}
           except ValueError:
               return {"retransmits": 0}

   @chaos_measure(NetworkDelay("100ms"), metrics=[OpenConnections(), RetransmitRate()])
   def run():
       run_pipeline()

Metrics on remote targets
~~~~~~~~~~~~~~~~~~~~~~~~~

All ``collect(target)`` calls receive the same target the runner uses.
For an ``SSHTarget``, ``target.run(cmd)`` executes on the remote machine:

.. code-block:: python

   from chaos_jungle.targets import SSHTarget
   from chaos_jungle.metrics import PingLatency, FileIntegrity

   target = SSHTarget("worker1", user="ubuntu")

   @chaos_measure(
       NetworkDelay("100ms"),
       target=target,
       metrics=[
           PingLatency("storage-node", count=5),
           FileIntegrity("*.pdb", "/scratch/data", checksum_file="/scratch/ref.md5"),
       ],
   )
   def run_remote():
       run_pipeline()

Standalone collection (outside a decorator):

.. code-block:: python

   from chaos_jungle.metrics import PingLatency
   from chaos_jungle.targets import LocalTarget

   m      = PingLatency("8.8.8.8", count=3)
   result = m.collect(LocalTarget())
   print(result)  # {"avg_ms": 12.3, "min_ms": 10.1, "max_ms": 14.7, "samples": 3}

----

AI quality metrics (LLMJudge)
-------------------------------

``LLMJudge`` scores LLM responses on four quality dimensions using a second
"judge" model.  Use it with ``runner.measure(evaluator=judge)`` to get
baseline vs. fault quality deltas automatically.

Quality dimensions
~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Metric
     - Meaning
   * - ``faithfulness``
     - 0–1: how closely the answer follows the provided context
   * - ``hallucination``
     - 0–1: fraction of the answer that is fabricated or contradicts the context
   * - ``coherence``
     - 0–1: grammatical and logical coherence of the response
   * - ``guardrail_violation``
     - True / False: did the model follow an injected instruction?

Basic usage:

.. code-block:: python

   import openai, os
   os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:11434/v1"
   os.environ["OPENAI_API_KEY"]  = "ollama"

   from chaos_jungle import ChaosRunner, Scenario, SemanticCorrupt, LLMJudge, LocalTarget

   CONTEXT  = "France is in Western Europe. Its capital is Paris."
   QUESTION = "What is the capital of France?"

   judge  = LLMJudge(model="qwen2.5:latest")
   fault  = SemanticCorrupt(mode="entity_swap", port=18050,
                            upstream="http://127.0.0.1:11434")
   runner = ChaosRunner(Scenario("quality-test", [fault]), LocalTarget())

   def workload():
       resp = openai.OpenAI().chat.completions.create(
           model="qwen2.5:latest",
           messages=[
               {"role": "system", "content": "Answer ONLY from the context."},
               {"role": "user",   "content": f"Context: {CONTEXT}\nQuestion: {QUESTION}"},
           ],
       )
       return {
           "question": QUESTION,
           "context":  CONTEXT,
           "response": resp.choices[0].message.content or "",
       }

   result = runner.measure(workload, n_baseline=3, n_fault=3, evaluator=judge)
   print(result.summary())

Output::

   Scenario : quality-test
   Trials   : 3 baseline / 3 fault

     faithfulness        baseline=0.95   fault=0.18   Δ -0.77
     hallucination       baseline=0.02   fault=0.91   Δ +0.89
     coherence           baseline=0.94   fault=0.88   Δ -0.06
     guardrail_violation baseline=False  fault=False

Score a single response directly:

.. code-block:: python

   from chaos_jungle import LLMJudge

   judge = LLMJudge(model="qwen2.5:latest")
   score = judge.score(
       question="What is the capital of France?",
       context="France is in Western Europe. Its capital is Paris.",
       response="The capital of France is Berlin.",
   )
   print(score.faithfulness)        # 0.05  (answer contradicts context)
   print(score.hallucination)       # 0.95  (Berlin is fabricated)
   print(score.guardrail_violation) # False

Average scores across multiple responses:

.. code-block:: python

   from chaos_jungle import average_scores

   scores = [judge.score(...) for ... in responses]
   avg    = average_scores(scores)
   print(avg.faithfulness)   # mean across all responses

Quality gate in CI:

.. code-block:: python

   result = runner.measure(workload, n_baseline=5, n_fault=5, evaluator=judge)

   assert result.passed_quality(
       min_faithfulness=0.70,
       max_hallucination=0.30,
   ), f"Quality gate failed: {result.summary()}"

----

Auto-collected fault metrics
-----------------------------

Every fault class declares a ``default_metrics`` list — the set of metrics that
are most relevant for analysing its impact.  ``MetricSet`` and
``CollectStrategy`` let you control *which* of those metrics are collected and
*when* they are sampled, without writing any collection boilerplate.

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario
   from chaos_jungle.faults import NetworkDelay
   from chaos_jungle.metrics import MetricSet, CollectStrategy

   runner = ChaosRunner(
       Scenario("latency-test", [NetworkDelay("200ms")]),
   )
   result = runner.measure(
       workload,
       strategy=CollectStrategy.SNAPSHOT,    # when to sample
       metric_set=MetricSet.DEFAULT,          # which metrics to collect
   )
   cm = result.collected_metrics
   print(cm.delta)   # {"rtt_ms": +198.4, "cpu_percent": +2.1, ...}

How it works
~~~~~~~~~~~~

**Metric sources** — two kinds of metrics are collected automatically:

.. list-table::
   :header-rows: 1
   :widths: 30 35 35

   * - Kind
     - Examples
     - How collected
   * - **System metrics**
     - ``cpu_percent``, ``memory_mb``, ``rtt_ms``, ``gpu_util_percent``
     - Shell command run on the target (auto-installed via ``preflight``)
   * - **Workload metrics**
     - ``duration_s``, ``error_rate``, ``parse_errors``
     - Read from the dict returned by your ``workload()`` callable

Collection is entirely transparent — no code changes to your workload are
needed for system metrics; workload metrics are extracted from whatever your
function already returns.

MetricSet — select which metrics to collect
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``MetricSet`` filters the fault's ``default_metrics`` list.  All methods are
immutable and can be chained:

.. code-block:: python

   from chaos_jungle.metrics import MetricSet

   # Use every metric declared in fault.default_metrics (the default)
   MetricSet.DEFAULT

   # Drop metrics you don't need
   MetricSet.DEFAULT.exclude("swap_used_mb", "context_switches")

   # Add extras on top of the fault defaults
   MetricSet.DEFAULT.add("inode_used", "open_fds")

   # Ignore fault defaults entirely — collect only these
   MetricSet.only("error_rate", "duration_s")

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Method
     - Effect
   * - ``MetricSet.DEFAULT``
     - Collect all metrics declared in ``fault.default_metrics``
   * - ``.exclude(*names)``
     - Remove specific metrics from the default set
   * - ``.add(*names)``
     - Add extra metric names on top of the defaults
   * - ``MetricSet.only(*names)``
     - Ignore fault defaults — collect only the named metrics

CollectStrategy — when to sample
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``CollectStrategy`` controls the *frequency* of collection, not the content:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Strategy
     - Behaviour
   * - ``CollectStrategy.SNAPSHOT``
     - Sample at three fixed points: **before** the fault (baseline),
       **during** the fault, and **immediately after** the fault stops.
       Fast, low overhead, gives a clear before/after comparison.
   * - ``CollectStrategy.RECOVERY``
     - Same as SNAPSHOT plus a post-fault time-series window.  System
       metrics are sampled every 10 s for ``recovery_window_s`` seconds
       (default 60 s) after the fault is reverted, showing how quickly the
       system returns to baseline.

.. code-block:: python

   from chaos_jungle.metrics import CollectStrategy

   # Snapshot — 3 points, instant
   result = runner.measure(workload, strategy=CollectStrategy.SNAPSHOT)

   # Recovery — 3 points + 2-minute post-fault window, sampled every 10 s
   result = runner.measure(
       workload,
       strategy=CollectStrategy.RECOVERY(recovery_window_s=120),
   )

CollectedMetrics — reading the results
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``result.collected_metrics`` is a ``CollectedMetrics`` dataclass:

.. code-block:: python

   cm = result.collected_metrics

   cm.strategy          # "snapshot" or "recovery"
   cm.active_metrics    # ["cpu_percent", "error_rate", "rtt_ms", ...]

   # Per-phase summaries (MetricSummary with .avg .min .max .p50 .p99)
   cm.baseline["rtt_ms"].avg    # 1.2 ms
   cm.fault["rtt_ms"].avg       # 199.6 ms
   cm.recovery["rtt_ms"].avg    # 2.4 ms  (after fault stopped)

   # Delta: fault.avg - baseline.avg per metric
   cm.delta   # {"rtt_ms": +198.4, "cpu_percent": +1.3, ...}

   # Time-series (RECOVERY mode — full list of MetricSample objects)
   for sample in cm.recovery["cpu_percent"].series:
       print(sample.timestamp_s, sample.values["cpu_percent"])

System metrics auto-collected per fault type
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Fault category
     - Auto-collected system metrics (from ``default_metrics``)
   * - Network
     - ``rtt_ms``, ``error_rate``, ``packet_loss_rate``, ``p50_latency_ms``, ``p99_latency_ms``
   * - CPU / Memory
     - ``cpu_percent``, ``memory_mb``, ``swap_used_mb``
   * - Disk / I/O
     - ``disk_used_bytes``, ``inode_used``, ``iops``
   * - GPU
     - ``gpu_util_percent``, ``gpu_memory_mb``, ``gpu_clock_mhz``
   * - LLM API
     - ``error_rate``, ``duration_s``, ``http_402_count``, ``http_429_count``, ``cost_usd``
   * - Storage / State
     - ``read_errors``, ``write_errors``, ``parse_errors``, ``corrupted_files``

Full example
~~~~~~~~~~~~

.. code-block:: python

   import time, requests
   from chaos_jungle import ChaosRunner, Scenario
   from chaos_jungle.faults import NetworkDelay
   from chaos_jungle.metrics import MetricSet, CollectStrategy

   runner = ChaosRunner(
       Scenario("net-delay", [NetworkDelay("200ms", jitter="20ms")]),
   )

   def workload():
       t0 = time.time()
       r  = requests.get("http://localhost:8080/ping", timeout=5.0)
       return {
           "duration_s": round(time.time() - t0, 3),
           "error_rate": 0.0 if r.ok else 1.0,
       }

   result = runner.measure(
       workload,
       n_baseline=3,
       n_fault=3,
       strategy=CollectStrategy.SNAPSHOT,
       metric_set=MetricSet.DEFAULT.exclude("swap_used_mb"),
   )

   cm = result.collected_metrics
   print(f"RTT delta:      {cm.delta.get('rtt_ms', 'n/a'):+.1f} ms")
   print(f"CPU delta:      {cm.delta.get('cpu_percent', 'n/a'):+.1f} %")
   print(f"Error rate (fault): {cm.fault.get('error_rate').avg:.2%}")

   print(result.summary())

----

See also
--------

* :ref:`guide-measurement` — ``runner.measure()`` API and CI gates
* :ref:`guide-judge` — ``LLMJudge`` full API reference
* :ref:`guide-llm` — LLM API fault parameters
