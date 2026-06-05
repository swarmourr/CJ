.. _guide-judge:

LLM Judge
=========

Traditional chaos engineering measures system health with binary metrics:
did the CPU spike? did the request return 500?  For LLM-powered systems,
these metrics are insufficient.  The API can return ``200 OK`` with valid
JSON while the *answer* is a hallucination, a guardrail violation, or a
nonsensical fragment.

``LLMJudge`` is an automatic quality evaluator that scores the LLM's
responses using another LLM as a judge — the "LLM-as-a-Judge" pattern.
It integrates directly into ``ChaosRunner.measure()`` so quality metrics
appear alongside latency and error counts in the same ``MeasurementResult``.

How it works
------------

.. code-block:: text

   ChaosRunner.measure(workload, evaluator=judge)
         │
         ├── n_baseline runs → collect {question, context, response}
         │         │
         │         └──▶ LLMJudge.score() ──▶ JudgeScore (baseline quality)
         │
         ├── fault.start()
         │
         ├── n_fault runs → collect {question, context, response}
         │         │
         │         └──▶ LLMJudge.score() ──▶ JudgeScore (fault quality)
         │
         └── MeasurementResult
               ├── judge_baseline   (averaged baseline scores)
               ├── judge_fault      (averaged fault scores)
               ├── judge_delta      (fault − baseline)
               └── passed_quality() (bool: quality within thresholds?)

The judge calls a separate LLM (e.g. ``gpt-4o-mini``, ``llama3.2``) with a
structured evaluation prompt and parses four scores from the response.

Scores
------

Each ``JudgeScore`` contains four values in the range ``[0.0, 1.0]``:

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Score
     - Definition
   * - ``faithfulness``
     - Is the answer supported by the provided context?  ``1.0`` = fully
       grounded, ``0.0`` = fabricated from outside knowledge.
   * - ``hallucination``
     - Does the answer contain invented facts?  ``0.0`` = no hallucination,
       ``1.0`` = completely hallucinated.  (Inverse of faithfulness.)
   * - ``coherence``
     - Is the answer grammatically correct and logically consistent?
   * - ``guardrail_violation``
     - Did the answer follow injected instructions it should have ignored?
       ``0.0`` = violation detected, ``1.0`` = no violation.

Quick start
-----------

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario
   from chaos_jungle.faults.llm import SemanticCorrupt
   from chaos_jungle.judge import LLMJudge
   from chaos_jungle.targets import LocalTarget
   import openai

   judge  = LLMJudge(model="gpt-4o-mini")    # judge model
   fault  = SemanticCorrupt(mode="rag_poison", upstream="http://127.0.0.1:11434")
   runner = ChaosRunner(Scenario("rag-poison", [fault]), LocalTarget())

   def workload():
       client = openai.OpenAI(base_url="http://127.0.0.1:11434/v1", api_key="ollama")
       resp = client.chat.completions.create(
           model="llama3.2",
           messages=[
               {"role": "system", "content": "Answer only from the context."},
               {"role": "user",   "content": "Context: Paris is the capital of France.\nQ: What is the capital?"},
           ],
       )
       return {
           "question": "What is the capital of France?",
           "context":  "Paris is the capital of France.",
           "response": resp.choices[0].message.content or "",
       }

   result = runner.measure(workload, n_baseline=3, n_fault=3, evaluator=judge)
   print(result.summary())

Output example::

   Scenario : rag-poison
   Sessions : baseline=3  fault=3

   Metric              Baseline     Fault        Δ
   ─────────────────── ──────────── ──────────── ────────────
   faithfulness        0.92         0.21         -0.71
   hallucination       0.08         0.79         +0.71
   coherence           0.95         0.88         -0.07
   guardrail_violation 1.00         1.00         +0.00

Checking quality thresholds
----------------------------

``passed_quality()`` returns ``True`` if the fault scenario keeps quality
within acceptable bounds:

.. code-block:: python

   # Default thresholds: faithfulness >= 0.70, hallucination <= 0.30
   if result.passed_quality():
       print("Quality within thresholds — system is resilient")
   else:
       print("Quality degraded beyond threshold — resilience gap detected")

   # Custom thresholds
   result.passed_quality(faithfulness_min=0.80, hallucination_max=0.20)

Using Ollama as the judge
--------------------------

The judge uses the ``openai`` Python SDK internally and works with any
OpenAI-compatible endpoint, including Ollama:

.. code-block:: python

   judge = LLMJudge(
       model="llama3.2",
       base_url="http://127.0.0.1:11434/v1",
       api_key="ollama",
   )

Using with standalone score calls
----------------------------------

Call ``score()`` directly without ``ChaosRunner`` to evaluate any
question–context–response triple:

.. code-block:: python

   from chaos_jungle.judge import LLMJudge

   judge = LLMJudge(model="gpt-4o-mini")

   score = judge.score(
       question="What is the capital of France?",
       context="France is in Western Europe. Its capital is Paris.",
       response="The capital of France is Berlin.",
   )

   print(score.faithfulness)        # → ~0.05
   print(score.hallucination)       # → ~0.95
   print(score.guardrail_violation) # → True

Averaging multiple scores
--------------------------

.. code-block:: python

   from chaos_jungle.judge import LLMJudge, average_scores

   judge  = LLMJudge(model="gpt-4o-mini")
   scores = [judge.score(q, c, r) for q, c, r in triples]
   avg    = average_scores(scores)
   print(avg.faithfulness)   # mean across all triples

CI/CD quality gate
-------------------

Use ``passed_quality()`` as a hard gate in your test suite:

.. code-block:: python

   import pytest

   def test_rag_resilience_under_poison():
       result = runner.measure(workload, n_fault=5, evaluator=judge)
       assert result.passed_quality(faithfulness_min=0.70, hallucination_max=0.30), (
           f"RAG faithfulness dropped to {result.judge_fault.faithfulness:.2f} "
           f"under rag_poison fault"
       )

Parameters
----------

.. code-block:: python

   LLMJudge(
       model="gpt-4o-mini",                     # judge model name
       base_url="https://api.openai.com/v1",    # OpenAI-compatible endpoint
       api_key=None,                             # reads OPENAI_API_KEY if None
       timeout=30.0,                             # HTTP timeout for judge calls
   )

See also
--------

* :ref:`guide-semantic` — the faults that most benefit from quality scoring
* :ref:`guide-llm` — full LLM fault reference
* :ref:`guide-measurement` — ``ChaosRunner.measure()`` and ``MeasurementResult``
