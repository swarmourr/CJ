chaos-jungle
============

A chaos engineering framework for injecting faults across every layer of
modern systems — network links, storage, OS processes, system resources,
LLM APIs, semantic context, and agent state — on any Linux machine or
macOS laptop, controlled via Python, SSH, or HTTP daemon.

.. list-table::
   :widths: 33 33 33
   :header-rows: 0

   * - **Infrastructure**
     - **LLM / AI**
     - **Observability & Safety**
   * - Network (tc / BPF)
     - API faults (latency, rate-limit, corrupt)
     - Session database + trace events (SQLite)
   * - Storage (bit-flip)
     - Semantic (entity swap, RAG poison)
     - Web dashboard
   * - Processes & services
     - Agent state (Redis, JSON, Postgres)
     - CSV export
   * - CPU / memory / disk / GPU
     - Quality scoring (LLMJudge)
     - CI/CD quality gates
   * - Safety policy + danger levels
     - Oracle assertions (PII, cost, injection)
     - Trace replay

----

.. toctree::
   :maxdepth: 2
   :caption: Getting Started

   quickstart
   architecture
   concepts
   examples

.. toctree::
   :maxdepth: 2
   :caption: Chaos Strategies

   guides/strategies

.. toctree::
   :maxdepth: 2
   :caption: Setup & Targets

   guides/local
   guides/ssh
   guides/http
   guides/separate-mode

.. toctree::
   :maxdepth: 2
   :caption: Infrastructure Faults

   guides/network
   guides/storage
   guides/process
   guides/resources
   guides/gpu

.. toctree::
   :maxdepth: 2
   :caption: LLM / AI Faults

   guides/llm
   guides/intercept
   guides/semantic
   guides/state
   guides/skill
   guides/judge
   guides/ollama
   guides/scenarios

.. toctree::
   :maxdepth: 2
   :caption: Measurement & Results

   guides/measurement
   guides/metrics
   guides/dashboard
   guides/data

.. toctree::
   :maxdepth: 2
   :caption: Safety & Assertions

   guides/safety
   guides/oracles
   guides/traces

.. toctree::
   :maxdepth: 2
   :caption: API Reference

   api/faults
   api/judge
   api/targets
   api/scenario
   api/runner
   api/decorators
   api/metrics
   api/guardrails
   api/suite
   api/cli
   api/daemon
   api/dashboard

.. toctree::
   :maxdepth: 1
   :caption: Project

   changelog
