.. _guide-oracles:

Oracle Assertions
=================

Oracles are **post-run assertions** that inspect workload results to
enforce guarantees that numeric metrics alone cannot capture.

A metric tells you *how much* a fault degraded performance (latency up
300 ms, throughput down 40%).  An oracle tells you *whether something
that must never happen did happen* — a secret leaked, a cost budget was
exceeded, a prompt injection was followed.

.. code-block:: python

   from chaos_jungle import ChaosRunner, Scenario
   from chaos_jungle.faults import LLMLatency
   from chaos_jungle.targets import LocalTarget
   from chaos_jungle.oracles import (
       NoSecretLeakage, NoPIILeakage, ValidJSONSchema,
       MaxCost, MaxRetries,
   )

   runner = ChaosRunner(
       Scenario("latency-test", [LLMLatency(delay_s=3.0)]),
       LocalTarget(),
   )

   result = runner.measure(
       workload,
       n_baseline=3,
       n_fault=3,
       oracles=[
           NoSecretLeakage(),
           NoPIILeakage(),
           ValidJSONSchema(),
           MaxCost(max_usd=0.05),
           MaxRetries(max_retries=5),
       ],
   )

   print(result.summary())   # oracle section printed automatically

   # Gate the test:
   if not result.passed_oracles(phase="fault"):
       raise AssertionError("Fault run failed oracle assertions")

How oracles run
---------------

Oracles are called **twice** by :meth:`~chaos_jungle.runner.ChaosRunner.measure`:

1. Once on the baseline run results (``phase="baseline"``) — to detect
   pre-existing issues.
2. Once on the fault run results (``phase="fault"``) — to detect
   fault-induced violations.

Both sets of results are stored in
:attr:`~chaos_jungle.runner.MeasurementResult.oracle_results` and printed
by :meth:`~chaos_jungle.runner.MeasurementResult.summary`.

----

Built-in oracles
----------------

NoSecretLeakage
~~~~~~~~~~~~~~~

Matches API keys, bearer tokens, password assignments, and PEM private
key headers in every workload response.

.. code-block:: python

   from chaos_jungle.oracles import NoSecretLeakage

   oracle = NoSecretLeakage()

   # Custom patterns (appended to defaults):
   oracle = NoSecretLeakage(patterns=[r"MY_SECRET_TOKEN=\S+"])

   # Replace defaults entirely:
   oracle = NoSecretLeakage(patterns=[r"CUSTOM=\S+"], strict=True)

Default patterns:

* ``sk-[A-Za-z0-9]{20,}`` — OpenAI API key
* ``Bearer <token>`` — HTTP bearer token
* ``password: <value>`` / ``api_key: <value>`` / ``secret: <value>``
* ``-----BEGIN (RSA|EC|OPENSSH) PRIVATE KEY``

NoPIILeakage
~~~~~~~~~~~~

Checks for email addresses, US phone numbers, SSNs, and credit card
numbers in responses.

.. code-block:: python

   from chaos_jungle.oracles import NoPIILeakage

   oracle = NoPIILeakage()                                # all PII types
   oracle = NoPIILeakage(categories=["email", "ssn"])    # specific types

Supported categories: ``"email"``, ``"phone"``, ``"ssn"``, ``"credit_card"``.

ValidJSONSchema
~~~~~~~~~~~~~~~

Asserts that every response is valid JSON.  Optionally validates against
a JSON Schema (requires ``pip install jsonschema``).

.. code-block:: python

   from chaos_jungle.oracles import ValidJSONSchema

   oracle = ValidJSONSchema()    # just check it parses as JSON

   oracle = ValidJSONSchema(
       schema={
           "type": "object",
           "required": ["answer", "confidence"],
           "properties": {
               "answer": {"type": "string"},
               "confidence": {"type": "number", "minimum": 0, "maximum": 1},
           },
       }
   )

MaxCost
~~~~~~~

Asserts that total LLM cost does not exceed a USD budget.  Reads the
``"cost_usd"`` key from each run dict, or estimates from ``"tokens_used"``.

.. code-block:: python

   from chaos_jungle.oracles import MaxCost

   oracle = MaxCost(max_usd=0.10)
   oracle = MaxCost(max_usd=1.0, cost_per_1k_tokens=0.01)  # GPT-4 rate

Your workload should return one of:

.. code-block:: python

   return {"response": "...", "cost_usd": 0.002}      # exact cost
   return {"response": "...", "tokens_used": 1500}    # estimated from tokens

MaxRetries
~~~~~~~~~~

Asserts that the agent did not exceed a retry budget.  Reads the
``"retries"`` key (int) from each run dict.

.. code-block:: python

   from chaos_jungle.oracles import MaxRetries

   oracle = MaxRetries(max_retries=3)

Your workload should return:

.. code-block:: python

   return {"response": "...", "retries": 2}

NoPromptInjectionFollowed
~~~~~~~~~~~~~~~~~~~~~~~~~

Checks whether the model's *response* echoes prompt-injection compliance
patterns (e.g. ``"Ignore previous instructions..."``).

.. code-block:: python

   from chaos_jungle.oracles import NoPromptInjectionFollowed

   oracle = NoPromptInjectionFollowed()
   oracle = NoPromptInjectionFollowed(
       indicators=[r"my new role is"],
   )

MaxAgentSteps
~~~~~~~~~~~~~

Asserts that the agent completed within a maximum number of tool calls.
Reads the ``"tool_calls"`` key (int or list) from each run dict.

.. code-block:: python

   from chaos_jungle.oracles import MaxAgentSteps

   oracle = MaxAgentSteps(max_steps=10)

Your workload should return:

.. code-block:: python

   # int form:
   return {"response": "...", "tool_calls": 3}

   # list form (tool names recorded for debugging):
   return {"response": "...", "tool_calls": ["search", "calculate", "respond"]}

----

Writing a custom oracle
-----------------------

Subclass :class:`~chaos_jungle.oracles.Oracle` and implement
:meth:`~chaos_jungle.oracles.Oracle.check`::

    from chaos_jungle.oracles import Oracle, OracleResult

    class RequiredCitation(Oracle):
        name = "RequiredCitation"

        def check(self, runs: list[dict], phase: str = "both") -> OracleResult:
            for i, run in enumerate(runs):
                if "sources:" not in run.get("response", "").lower():
                    return OracleResult(
                        oracle=self.name,
                        passed=False,
                        score=0.0,
                        phase=phase,
                        reason=f"Run #{i + 1} missing required citation section",
                    )
            return OracleResult(
                oracle=self.name,
                passed=True,
                score=1.0,
                phase=phase,
                reason=f"All {len(runs)} run(s) include citations",
            )

Then pass it alongside built-in oracles::

    result = runner.measure(
        workload,
        oracles=[NoPIILeakage(), RequiredCitation()],
    )

----

OracleResult fields
-------------------

.. list-table::
   :header-rows: 1
   :widths: 20 15 65

   * - Field
     - Type
     - Description
   * - ``oracle``
     - ``str``
     - Oracle class name
   * - ``passed``
     - ``bool``
     - ``True`` if the assertion holds
   * - ``reason``
     - ``str``
     - Human-readable explanation
   * - ``score``
     - ``float``
     - 0.0–1.0 continuous pass score; ``1.0`` = fully passed
   * - ``phase``
     - ``str``
     - ``"baseline"``, ``"fault"``, or ``"both"``

----

Oracles vs LLMJudge
--------------------

.. list-table::
   :header-rows: 1
   :widths: 20 40 40

   * - Feature
     - Oracles
     - LLMJudge
   * - Cost
     - Free (regex / arithmetic)
     - Requires an LLM call per evaluation
   * - Output
     - Binary pass/fail + score
     - Continuous 0–1 quality scores
   * - What it checks
     - Security, cost, structure, behaviour
     - Faithfulness, hallucination, coherence
   * - Latency
     - Microseconds
     - Seconds (model inference)
   * - Can combine?
     - Yes — pass both to ``measure()``
     - Yes

See also
--------

* :ref:`guide-measurement` — ``runner.measure()`` API
* :ref:`guide-safety` — ``SafetyPolicy`` and danger levels
* :ref:`guide-traces` — trace event capture and replay
