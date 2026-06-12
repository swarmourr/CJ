# chaos-jungle — Framework & Architecture

chaos-jungle is a Python chaos engineering library designed to be **fully
embeddable in application code**. Faults are Python objects. Experiments are
Python functions. No config files, no sidecars, no infrastructure required to
get started.

The library targets four distinct layers of a modern system — network,
storage, LLM API transport, and semantic content — with a deliberate focus on
LLM and agentic applications where standard infrastructure chaos tools fall
short.

---

## Core idea

Traditional chaos tools inject faults at the infrastructure level (packet
loss, disk failure, CPU stress). That is not enough for LLM-based applications,
where the real failure modes are:

- The API is slow or rate-limited
- The response is structurally valid but factually wrong
- A RAG context has been poisoned
- A prompt injection slipped through
- The model ran out of token budget mid-answer

chaos-jungle covers all of these — from a single `pip install`, on any OS,
without touching the model or the application code.

---

## Five-plane architecture

```
╔══════════════════════════════════════════════════════════════════╗
║                       CONTROL  PLANE                            ║
║                                                                  ║
║   Scenario ──── ChaosRunner ──── ExperimentSuite                ║
║   @chaos · @chaos_measure · inject() · door() · measure()       ║
╚═══════════════════════╤══════════════════════════════════════════╝
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
╔═══════════════╗ ╔════════════╗ ╔═══════════════════╗
║  TRANSPORT    ║ ║   TARGET   ║ ║   EVALUATION      ║
║    PLANE      ║ ║   PLANE    ║ ║     PLANE         ║
╠═══════════════╣ ╠════════════╣ ╠═══════════════════╣
║  HTTP proxy   ║ ║  Local     ║ ║  LLMJudge         ║
║  httpx patch  ║ ║  SSH       ║ ║  Metrics          ║
║  OS / BPF     ║ ║  HTTP      ║ ║  Quality gates    ║
╚═══════╤═══════╝ ╚══════╤═════╝ ╚═════════╤═════════╝
        │                │                 │
        └────────────────┼─────────────────┘
                         ▼
╔══════════════════════════════════════════════════════════════════╗
║                        DATA  PLANE                              ║
║                                                                  ║
║   SQLite DB  ──►  Web Dashboard  ──►  CSV Export  ──►  CLI      ║
╚══════════════════════════════════════════════════════════════════╝
```

---

## Control plane — the Python API

The control plane is the developer-facing interface. It assembles faults into
scenarios, manages the experiment lifecycle, and records results.

| Object | Role |
|--------|------|
| `Scenario` | A named, ordered list of `Fault` objects — pure data, no logic |
| `ChaosRunner` | Orchestrates preflight → start → workload → stop → revert |
| `ExperimentSuite` | Run a batch of scenarios in sequence or parallel |
| `@chaos` / `@chaos_measure` | Decorator wrappers around `ChaosRunner` |
| `inject()` | Lightweight context manager for zero-boilerplate fault injection |
| `door()` | Cycling runner — alternates fault ON / fault OFF for N cycles |
| `measure()` | Statistical mode — baseline + fault + delta + quality scoring |
| `@pytest.mark.chaos` | pytest marker that activates `inject()` per test |

### Experiment lifecycle

```
╔═══════════╗     ╔═══════════╗     ╔═══════════╗
║ PREFLIGHT ║────►║   START   ║────►║ WORKLOAD  ║
║           ║     ║           ║     ║           ║
║ check     ║     ║ inject    ║     ║ your code ║
║ tools     ║     ║ faults    ║     ║ runs here ║
╚═══════════╝     ╚═══════════╝     ╚═════╤═════╝
                                          │
╔═══════════╗     ╔═══════════╗     ╔═════▼═════╗
║  RECORD   ║◄────║  REVERT   ║◄────║   STOP    ║
║           ║     ║           ║     ║           ║
║ write to  ║     ║ undo side ║     ║ remove    ║
║ SQLite    ║     ║ effects   ║     ║ faults    ║
╚═══════════╝     ╚═══════════╝     ╚═══════════╝
```

### Usage styles

```python
# 1. Explicit start / stop
runner = ChaosRunner(scenario, target)
runner.start()
run_workload()
runner.stop()

# 2. Context manager
from chaos_jungle.intercept import inject, Latency
with inject(Latency(2.0)):
    run_workload()

# 3. Decorator
@chaos(LLMRateLimit(n=5))
def run_workload():
    ...

# 4. pytest marker
@pytest.mark.chaos(Unavailable(), Latency(1.0))
def test_handles_outage(llm_call, assert_ok):
    reply, _ = llm_call("ping", timeout=10.0)
    assert_ok(reply)

# 5. Statistical measurement
result = runner.measure(workload, n_baseline=5, n_fault=5, evaluator=judge)
print(result.summary())
assert result.passed_quality(min_faithfulness=0.70)
```

---

## Transport plane — three injection depths

Faults are injected at different depths depending on what layer you want to
test.

### 1. OS / kernel level — infrastructure faults

Directly manipulates the Linux kernel via privileged tools. Requires a Linux
target and `sudo`.

```
┌──────────────────────────────────────────────────────┐
│                  YOUR  APPLICATION                   │
└────────────────────────┬─────────────────────────────┘
           syscalls / file I/O / network packets
┌────────────────────────▼─────────────────────────────┐
│               LINUX  KERNEL  LAYER                   │
│                                                      │
│  ╔══════════════╗  ╔═══════════╗  ╔═══════════════╗  │
│  ║   tc/netem   ║  ║    BPF    ║  ║  stress-ng    ║  │
│  ║              ║  ║           ║  ║  systemctl    ║  │
│  ║ NetworkDelay ║  ║ SilentNet ║  ║  docker       ║  │
│  ║ NetworkLoss  ║  ║ Corrupt   ║  ║  pkill        ║  │
│  ╚══════════════╝  ╚═══════════╝  ╚═══════════════╝  │
└──────────────────────────────────────────────────────┘
         Network · Storage · CPU · Memory · Disk
```

Faults: `NetworkDelay`, `NetworkLoss`, `NetworkCorrupt`, `NetworkDuplicate`,
`SilentNetworkCorrupt`, `StorageCorrupt`, `CPUStress`, `MemoryStress`,
`IOStress`, `DiskFull`, `ProcessKill`, `ServiceFault`, `ContainerKill`.

---

### 2. HTTP proxy level — LLM API faults

A local MITM proxy sits between the LLM SDK and the real API endpoint. The SDK
is redirected to `localhost:<port>` and the proxy applies faults before
forwarding. Works on macOS, Windows, and Linux — no `sudo` needed.

```
┌──────────────────────────────────────────────────────┐
│            LLM  SDK  (any provider)                  │
│   OpenAI · Anthropic · Azure · Ollama · LiteLLM …    │
└────────────────────────┬─────────────────────────────┘
           OPENAI_BASE_URL redirected to localhost:<port>
┌────────────────────────▼─────────────────────────────┐
│                    CJ  PROXY                         │
│                                                      │
│  ① match request URL against fault rules             │
│  ② apply fault:                                      │
│       latency · 429 · 503 · corrupt · truncate       │
│       hallucinate · token-starve · timeout · poison  │
│  ③ forward (or short-circuit) to real endpoint       │
└────────────────────────┬─────────────────────────────┘
                    HTTPS tunnel
┌────────────────────────▼─────────────────────────────┐
│              REAL  API  ENDPOINT                     │
└──────────────────────────────────────────────────────┘
```

Faults: `LLMLatency`, `LLMRateLimit`, `LLMTimeout`, `LLMResponseCorrupt`,
`LLMUnavailable`, `LLMHallucination`, `LLMStreamInterrupt`,
`LLMTokenStarvation`, `SemanticCorrupt`, `ToolFault`, `MCPFault`.

---

### 3. HTTP transport patch — intercept layer

Patches `httpx` and `requests` **at the class level** so every SDK that uses
them is affected automatically. No proxy port, no env var, no SDK
reconfiguration. Works on any OS.

```
┌──────────────────────────────────────────────────────────────┐
│   LLM SDK  (OpenAI · Anthropic · LiteLLM · LangChain …)     │
│                  uses httpx or requests internally           │
└──────────────────────────┬───────────────────────────────────┘
                           │  patched transparently
┌──────────────────────────▼───────────────────────────────────┐
│               CJ  TRANSPORT  PATCH                           │
│                                                              │
│  ① Behavior.before(url) ── latency · jitter · timeout        │
│  ② real send()          ── actual HTTP/HTTPS request         │
│  ③ Behavior.after(url)  ── corrupt · 429 · 503               │
│  ④ probability roll     ── each behavior fires independently │
└──────────────────────────┬───────────────────────────────────┘
                           │  real TCP connection
┌──────────────────────────▼───────────────────────────────────┐
│                   API  ENDPOINT                              │
└──────────────────────────────────────────────────────────────┘
```

Faults: `Latency`, `Jitter`, `RateLimit`, `Unavailable`, `Timeout`,
`CorruptResponse` — from `chaos_jungle.intercept`.

---

## Target plane — machine abstraction

A **Target** abstracts over a machine. The runner and faults call
`target.run(cmd)`, `target.sudo(cmd)`, and `target.put(file)`.

```
╔═════════════════════════════════════════════════════════╗
║                     ChaosRunner                         ║
╚══════════════╤═══════════════╤══════════════════╤════════╝
               │               │                  │
     ┌─────────▼──────┐ ┌──────▼───────┐ ┌───────▼──────────┐
     │  LocalTarget   │ │  SSHTarget   │ │  HTTPTarget      │
     │                │ │              │ │                  │
     │ subprocess.run │ │ Paramiko SSH │ │ HTTP POST /exec  │
     └────────┬───────┘ └──────┬───────┘ └───────┬──────────┘
              │                │                 │
              ▼                ▼                 ▼
       same  machine     remote  Linux      cj-daemon :8642
```

| Target | Use case |
|--------|----------|
| `LocalTarget` | LLM / AI faults on macOS or any OS — no sudo |
| `SSHTarget` | Infrastructure faults on remote Linux servers |
| `HTTPTarget` | Machines behind a firewall or inside CI runners |

`cj-daemon` is a lightweight REST agent deployed on the target machine that
accepts `POST /exec` commands from `HTTPTarget`.

---

## Evaluation plane — quality measurement

chaos-jungle can measure whether faults actually degrade quality, not just
whether they execute. The `measure()` API runs paired baseline and fault
samples, then computes statistical deltas.

```
runner.measure(workload, n_baseline=5, n_fault=5, evaluator=judge)
│
├─► PHASE 1 ─ BASELINE  ── run workload × n_baseline ──► baseline metrics
│
├─► PHASE 2 ─ FAULT ON  ── inject faults
│
├─► PHASE 3 ─ FAULT     ── run workload × n_fault    ──► fault metrics
│
├─► PHASE 4 ─ FAULT OFF ── stop faults
│
└─► PHASE 5 ─ EVALUATE  ── compute delta + LLMJudge scores
                                    │
             ╔══════════════════════▼════════════════════════╗
             ║            MeasurementResult                  ║
             ╠═══════════════════════════════════════════════╣
             ║  baseline metrics  │  fault metrics           ║
             ║  delta (mean · std · %)                       ║
             ║  judge scores: faithfulness · hallucination   ║
             ║                coherence · guardrail          ║
             ║  passed_quality(min_faithfulness=0.7)         ║
             ╚═══════════════════════════════════════════════╝
```

**LLMJudge** calls a second model to score responses on:

| Metric | What it measures |
|--------|-----------------|
| `faithfulness` | How closely the answer follows the provided context (0–1) |
| `hallucination` | Fraction of the answer that is fabricated (0–1) |
| `coherence` | Grammatical and logical coherence (0–1) |
| `guardrail_violation` | Whether an injected instruction was followed (bool) |

---

## Data plane — observability

Every experiment writes structured data to a local SQLite database. All
observation tools read from the same database.

```
╔══════════════════════════════════════════════════════════╗
║          ~/.chaos-jungle/chaos_jungle.db                ║
╠══════════════════════════════════════════════════════════╣
║  sessions  ── one row per ChaosRunner.start() call      ║
║  faults    ── one row per active fault + parameters      ║
║  events    ── timestamped log (started · stopped · err)  ║
║  results   ── JSON blobs from runner.record_result()     ║
║  commands  ── every shell command on every target        ║
╚══════════════════════════╤═══════════════════════════════╝
                           │
           ┌───────────────┼───────────────┐
           ▼               ▼               ▼
    ╔═════════════╗  ╔═══════════╗  ╔═══════════╗
    ║  Dashboard  ║  ║    CSV    ║  ║    CLI    ║
    ║   :8080     ║  ║  export   ║  ║  summary  ║
    ╚═════════════╝  ╚═══════════╝  ╚═══════════╝
```

---

## Fault catalogue

### Infrastructure layer (Linux only)

| Class | Effect |
|-------|--------|
| `NetworkDelay` | Add latency + jitter via `tc netem` |
| `NetworkLoss` | Drop N% of packets |
| `NetworkCorrupt` | Corrupt N% of packets (TC-level) |
| `NetworkDuplicate` | Duplicate N% of packets |
| `SilentNetworkCorrupt` | Flip bits silently — TCP checksum still valid (BPF/XDP) |
| `StorageCorrupt` | Flip random bytes in files on a schedule — fully revertible |
| `CPUStress` | Saturate N cores via `stress-ng` |
| `MemoryStress` | Allocate N MiB of RAM |
| `IOStress` | Generate sustained disk I/O load |
| `DiskFull` | Fill filesystem near capacity |
| `ProcessKill` | Kill OS processes matching a pattern |
| `ServiceFault` | Stop / restart / kill a systemd service |
| `ContainerKill` | Kill / stop / pause a Docker container |

### LLM API layer (any OS)

| Class | Effect | Mechanism |
|-------|--------|-----------|
| `LLMLatency` | Delay every response | HTTP proxy |
| `LLMRateLimit` | Return 429 after N calls | HTTP proxy |
| `LLMTimeout` | Hang connection for N seconds | HTTP proxy |
| `LLMResponseCorrupt` | Truncate / empty / invalidate JSON | HTTP proxy |
| `LLMUnavailable` | Return 503 for every call | HTTP proxy |
| `LLMHallucination` | Replace answer with injected wrong text | HTTP proxy |
| `LLMStreamInterrupt` | Cut streaming response after N chunks | HTTP proxy |
| `LLMTokenStarvation` | Force `max_tokens` to a tiny value | HTTP proxy |
| `ToolFault` | Fail all tool calls (or a named tool) | HTTP proxy |
| `MCPFault` | Fail / timeout MCP server calls | HTTP proxy |

### Semantic layer (any OS)

| Class | Mode | Effect |
|-------|------|--------|
| `SemanticCorrupt` | `entity_swap` | Swap named entities — Paris → Berlin |
| `SemanticCorrupt` | `context_truncate` | Cut context to ~50% |
| `SemanticCorrupt` | `inject_distractor` | Insert contradictory instruction |
| `SemanticCorrupt` | `rag_poison` | Append false authoritative paragraph |

### Intercept layer (any OS, zero config)

| Class | Effect |
|-------|--------|
| `Latency` | Fixed delay per request |
| `Jitter` | Random delay in a range |
| `RateLimit` | Return 429 after N calls |
| `Unavailable` | Return 503 (optional probability) |
| `Timeout` | Drop connection immediately |
| `CorruptResponse` | Return malformed response body |

### Agent state layer (Redis / JSON / Postgres)

| Class | Effect |
|-------|--------|
| `RedisStateCorrupt` | Mutate Redis keys matching a glob |
| `JsonStateCorrupt` | Mutate a dot-path field in a JSON file |
| `PostgresStateCorrupt` | Run a parameterised UPDATE on a Postgres column |

Mutation modes: `nullify`, `delete`, `negate`, `type_mismatch`, `inject`.

---

## Module map

```
chaos_jungle/
│
├── scenario.py        ── Scenario dataclass
├── runner.py          ── ChaosRunner · MeasurementResult · door()
├── suite.py           ── ExperimentSuite
├── decorators.py      ── @chaos · @chaos_session · @chaos_measure
├── intercept.py       ── inject() · Behavior subclasses
├── pytest_plugin.py   ── @pytest.mark.chaos auto-fixture
│
├── faults/
│   ├── llm.py         ── LLMLatency · LLMRateLimit · LLMHallucination …
│   ├── semantic.py    ── SemanticCorrupt (4 modes)
│   ├── state.py       ── RedisStateCorrupt · JsonStateCorrupt · PostgresStateCorrupt
│   ├── network.py     ── NetworkDelay · NetworkLoss · NetworkCorrupt …
│   ├── storage.py     ── StorageCorrupt
│   ├── process.py     ── ProcessKill · ServiceFault · ContainerKill
│   ├── resources.py   ── CPUStress · MemoryStress · IOStress · DiskFull
│   └── bpf.py         ── SilentNetworkCorrupt
│
├── targets/
│   ├── local.py       ── LocalTarget
│   ├── ssh.py         ── SSHTarget
│   └── http.py        ── HTTPTarget
│
├── judge.py           ── LLMJudge · JudgeScore · average_scores
├── metrics.py         ── PingLatency · CommandMetric · FileIntegrity …
├── session_db.py      ── SQLite schema + helpers
├── dashboard.py       ── FastAPI web dashboard
├── daemon.py          ── cj-daemon REST agent
├── guardrails.py      ── ConflictError / ConflictWarning
└── preflight.py       ── tool detection + auto-install
```

---

## Design principles

**Fault injection as code.**
Faults are Python objects. Scenarios are Python lists. There are no YAML
manifests, no Kubernetes operators, no agent deployments. If you can `pip
install` it you can inject faults.

**No vendor lock-in.**
The intercept layer patches `httpx` and `requests` at the class level so
OpenAI, Anthropic, LiteLLM, LangChain, and any other SDK that relies on
those libraries is covered automatically — no per-SDK configuration.

**Layered and composable.**
Multiple faults stack. The runner injects them in order and removes them in
reverse. The intercept layer supports nested `inject()` contexts.

**Revertible by default.**
Every fault implements `revert()`. `StorageCorrupt` keeps a backup of every
file it touches. `DiskFull` removes the padding file on stop. No manual
cleanup required.

**Zero infrastructure for LLM tests.**
`inject()` works on macOS, Windows, and Linux — no `sudo`, no proxy process,
no port setup. Just wrap your code.

**Observability first.**
Every action is written to SQLite. The dashboard, CSV export, and CLI all read
the same database — a complete audit trail of every fault, every command, and
every result.

---

## Current status

| Layer | Status |
|-------|--------|
| LLM API faults (proxy) | Implemented and tested locally against Ollama |
| Intercept layer | Implemented and tested — works on any OS |
| Semantic faults | Implemented and tested locally |
| Realistic scenarios (R01–R10) | Implemented with baseline measurements |
| pytest integration | Implemented — `@pytest.mark.chaos` working |
| Network / storage faults | Implemented — require Linux target for full validation |
| State faults (Redis / JSON / Postgres) | Implemented — require running services |
| Resource exhaustion faults | Implemented — require Linux + `stress-ng` |
| Web dashboard | Implemented — FastAPI, local use |
| LLMJudge | Implemented — requires OpenAI API key or local judge model |
