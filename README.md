# chaos-jungle

A generic chaos engineering framework for injecting faults into distributed systems — network delays, packet loss, storage corruption, and LLM/agent failures — on any Linux machine, controlled locally, via SSH, or via HTTP daemon.

> **Local only — do not push this file.**

---

## Table of Contents

1. [Installation](#installation)
2. [Quick Start](#quick-start)
3. [Concepts](#concepts)
4. [Fault Types](#fault-types)
   - [Network Faults](#network-faults)
   - [Storage Faults](#storage-faults)
   - [LLM & Agent Faults](#llm--agent-faults)
   - [MCP Faults](#mcp-faults)
5. [Targets](#targets)
6. [Usage Modes](#usage-modes)
7. [Metrics](#metrics)
   - [Built-in Metrics](#built-in-metrics)
   - [@metric Decorator](#metric-decorator)
   - [ScriptMetric](#scriptmetric)
   - [Subclassing Metric](#subclassing-metric)
8. [Dashboard](#dashboard)
9. [ExperimentSuite](#experimentsuite)
10. [CLI Reference](#cli-reference)
11. [Data Storage & Export](#data-storage--export)
12. [Guardrails & Validation](#guardrails--validation)
13. [HTTP Daemon](#http-daemon)
14. [SSH Authentication](#ssh-authentication)

---

## Installation

```bash
pip install git+https://github.com/swarmourr/CJ.git

# Force reinstall (upgrade to latest)
pip install --force-reinstall git+https://github.com/swarmourr/CJ.git

# Ubuntu 23+ / externally managed Python
pip install --break-system-packages git+https://github.com/swarmourr/CJ.git
```

System dependencies on the target machine (network faults):

```bash
sudo apt install iproute2 e2fsprogs inotify-tools
```

---

## Quick Start

```python
from chaos_jungle.decorators import chaos_measure
from chaos_jungle.faults import NetworkDelay
from chaos_jungle.metrics import PingLatency

@chaos_measure(
    NetworkDelay("100ms", jitter="10ms"),
    metrics=[PingLatency("8.8.8.8", count=5)],
    scenario_name="E1",
)
def run_experiment():
    run_my_pipeline()
    return {"files_transferred": 120, "retries": 3}

summary = run_experiment()
print(summary["duration_s"])   # wall-clock seconds chaos was active
print(summary["metrics"])      # baseline vs chaos comparison
```

```bash
# View results in the browser
chaos-jungle dashboard        # → http://localhost:8050
```

---

## Concepts

| Term | Meaning |
|------|---------|
| **Fault** | A specific failure to inject (delay, loss, LLM timeout…) |
| **Scenario** | Named collection of faults applied together |
| **Target** | Where to inject faults (local machine, SSH host, HTTP daemon) |
| **Session** | One complete experiment run: preflight → inject → revert → record |
| **Metric** | A measurement collected at baseline and under chaos |
| **ChaosRunner** | Orchestrator: manages lifecycle, DB logging, guardrails |

### Experiment lifecycle

```
preflight → baseline metrics → inject faults → run workload → chaos metrics → revert → record
```

All faults are reverted in a `finally` block — even on exception or crash.

---

## Fault Types

### Network Faults

All network faults use Linux `tc netem`. They auto-detect the default network interface if `iface` is not specified.

#### NetworkDelay

Add artificial latency to outgoing packets.

```python
from chaos_jungle.faults import NetworkDelay

NetworkDelay("100ms")                        # 100 ms delay
NetworkDelay("100ms", jitter="10ms")         # 100 ± 10 ms
NetworkDelay("100ms", jitter="10ms", iface="eth0")
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `delay` | str | required | Delay like `"100ms"`, `"1s"` |
| `jitter` | str | `None` | Jitter like `"10ms"` |
| `iface` | str | auto | Network interface |

#### NetworkLoss

Drop a percentage of outgoing packets.

```python
from chaos_jungle.faults import NetworkLoss

NetworkLoss("5%")         # drop 5% of packets
NetworkLoss("0.1%")       # drop 0.1% of packets
```

#### NetworkCorrupt

Corrupt a percentage of packets (breaks the checksum — kernel will drop them).

```python
from chaos_jungle.faults import NetworkCorrupt

NetworkCorrupt("1%")
```

#### NetworkDuplicate

Duplicate a percentage of packets.

```python
from chaos_jungle.faults import NetworkDuplicate

NetworkDuplicate("0.5%")
```

#### SilentNetworkCorrupt

Corrupt packets silently — the checksum is recomputed after corruption, so the kernel accepts them. Requires BPF/XDP support.

```python
from chaos_jungle.faults import SilentNetworkCorrupt

SilentNetworkCorrupt(rate=100)   # corrupt 1-in-100 packets
```

---

### Storage Faults

#### StorageCorrupt

Flip bits in files matching a glob pattern. Uses crontab + bundled `cj_storage` scripts.

```python
from chaos_jungle.faults import StorageCorrupt

StorageCorrupt("*.pdb", "/scratch/data")
StorageCorrupt("*.h5",  "/mnt/storage", interval_s=30)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `pattern` | str | Glob pattern for target files |
| `directory` | str | Absolute path to search in |
| `interval_s` | int | How often to corrupt (default: 60s) |

---

### LLM & Agent Faults

LLM faults work by launching a lightweight local proxy that sits between your agent and the LLM API. The proxy intercepts HTTP calls and injects the chosen failure. The environment variable `OPENAI_BASE_URL` (or a custom one) is set automatically to redirect your client through the proxy.

**All 10 LLM fault classes:**

#### LLMLatency

Add artificial delay to every API call.

```python
from chaos_jungle.faults.llm import LLMLatency

LLMLatency(delay_s=3.0)
LLMLatency(delay_s=2.0, upstream="http://localhost:11434")  # Ollama
```

#### LLMRateLimit

Return HTTP 429 after N successful requests.

```python
from chaos_jungle.faults.llm import LLMRateLimit

LLMRateLimit(n=5)   # allow 5 calls, then rate-limit
LLMRateLimit(n=0)   # rate-limit immediately
```

#### LLMTimeout

Hang every connection for `timeout_s` seconds then return HTTP 504. No request is ever forwarded.

```python
from chaos_jungle.faults.llm import LLMTimeout

LLMTimeout(timeout_s=30.0)
LLMTimeout(timeout_s=5.0)
```

#### LLMResponseCorrupt

Forward the call but mangle the response before returning it.

```python
from chaos_jungle.faults.llm import LLMResponseCorrupt

LLMResponseCorrupt(mode="truncate")       # cut body to half length
LLMResponseCorrupt(mode="empty")          # replace with {}
LLMResponseCorrupt(mode="invalid_json")   # replace with garbage string
```

#### LLMUnavailable

Always return HTTP 503 — simulate a completely down endpoint.

```python
from chaos_jungle.faults.llm import LLMUnavailable

LLMUnavailable()
```

#### ToolFault

Inject errors into tool-call messages (requests containing `role: tool`).

```python
from chaos_jungle.faults.llm import ToolFault

ToolFault()                         # fail all tool calls
ToolFault(tool_name="search")       # fail only the "search" tool
ToolFault(tool_name="calculator")
```

#### LLMHallucination

Replace the assistant's response content with injected wrong text.

```python
from chaos_jungle.faults.llm import LLMHallucination

LLMHallucination("The capital of France is Berlin.")
LLMHallucination("I don't know anything about that topic.")
```

#### LLMStreamInterrupt

Cut a streaming SSE response after N data events.

```python
from chaos_jungle.faults.llm import LLMStreamInterrupt

LLMStreamInterrupt(interrupt_after=3)
LLMStreamInterrupt(interrupt_after=1)   # cut after first chunk
```

Only affects requests with `"stream": true`.

#### LLMTokenStarvation

Rewrite `max_tokens` to a tiny value, forcing truncated responses.

```python
from chaos_jungle.faults.llm import LLMTokenStarvation

LLMTokenStarvation(max_tokens=5)
LLMTokenStarvation(max_tokens=1)   # force single-token responses
```

---

### MCP Faults

#### MCPFault

Inject failures into MCP (Model Context Protocol) server calls. Unlike LLM faults, `upstream` points to your MCP server.

```python
from chaos_jungle.faults.llm import MCPFault

MCPFault(mode="tool_error")             # return JSON-RPC error -32000
MCPFault(mode="unavailable")            # return HTTP 503
MCPFault(mode="timeout", timeout_s=10)  # hang for 10s then 504

# Custom MCP server
MCPFault(
    mode="tool_error",
    upstream="http://localhost:3000",
    base_url_env="MCP_SERVER_URL",
    port=18100,
)
```

---

## Targets

### LocalTarget

Run on this machine. No network connection needed.

```python
from chaos_jungle.targets import LocalTarget

target = LocalTarget()
```

### SSHTarget

Run on a remote machine over SSH. Paramiko-based; no agent required on the target.

```python
from chaos_jungle.targets import SSHTarget

# Uses ssh-agent or ~/.ssh/id_* by default
SSHTarget("worker1", user="ubuntu")

# Explicit key file
SSHTarget("worker1", user="ubuntu", key="~/.ssh/id_ed25519")

# Password auth
SSHTarget("worker1", user="ubuntu", password="secret",
          allow_agent=False, look_for_keys=False)

# Non-standard port
SSHTarget("worker1", user="ubuntu", port=2222)
```

SSH auth is tried in order: explicit key → ssh-agent → default keys → password.

Required sudoers entry on the target:

```bash
echo "ubuntu ALL=(ALL) NOPASSWD: /sbin/tc, /bin/dd, /usr/sbin/filefrag" \
  | sudo tee /etc/sudoers.d/chaos-jungle
```

### HTTPTarget (Daemon)

Control a remote machine via REST API. Use when SSH is blocked or you need multiple controllers.

```python
from chaos_jungle.targets import HTTPTarget

target = HTTPTarget("http://worker1:7777", token="mysecret")
```

Start the daemon on the target first:

```bash
cj-daemon --port 7777 --token mysecret
```

---

## Usage Modes

### Decorator

```python
from chaos_jungle.decorators import chaos_measure
from chaos_jungle.faults import NetworkDelay

@chaos_measure(NetworkDelay("100ms"), scenario_name="E1")
def run_experiment():
    run_pipeline()
    return {"retries": 3}

summary = run_experiment()
print(summary["duration_s"])
```

### Context Manager

```python
from chaos_jungle import chaos_session
from chaos_jungle.faults import NetworkLoss

with chaos_session(NetworkLoss("5%"), name="loss-test") as session:
    run_pipeline()
    print(session.export("json"))
```

### Explicit API

```python
from chaos_jungle import ChaosRunner, Scenario
from chaos_jungle.faults import NetworkDelay
from chaos_jungle.targets import SSHTarget

scenario = Scenario("E1", [NetworkDelay("100ms")])
target   = SSHTarget("worker1", user="ubuntu")
runner   = ChaosRunner(scenario, target)

runner.start()
run_pipeline()
runner.stop()
```

### Separate Mode (CLI)

Inject faults from one terminal while your workflow runs in another:

```bash
# Terminal 1 — start fault injection
chaos-jungle start --scenario E1 --delay 100ms

# Terminal 2 — run your workflow normally
bash my_workflow.sh

# Terminal 1 — stop and revert
chaos-jungle stop
```

With a duration (auto-stops):

```bash
chaos-jungle start --scenario E1 --delay 100ms --duration 10m
```

---

## Metrics

Metrics are collected automatically at baseline (before chaos) and under chaos. Results are stored in the DB with keys like `baseline_ping_avg_ms` and `chaos_ping_avg_ms`.

### Built-in Metrics

| Class | Measures | Key params |
|-------|----------|-----------|
| `PingLatency` | avg / min / max RTT | `host`, `count` |
| `CommandMetric` | any shell command output | `cmd`, `parse`, `name` |
| `FileIntegrity` | file count + md5 diff | `path`, `pattern` |
| `ThroughputMetric` | curl download speed | `url` |

```python
from chaos_jungle.metrics import PingLatency, CommandMetric

@chaos_measure(
    NetworkDelay("100ms"),
    metrics=[
        PingLatency("8.8.8.8", count=5),
        CommandMetric(
            "ss -tn state established | wc -l",
            parse=lambda out: {"connections": int(out.strip())},
            name="tcp",
        ),
    ],
)
def run(): ...
```

### @metric Decorator

Three forms — all produce a `Metric` instance registered globally:

```python
from chaos_jungle.metrics import metric

# Form 1: explicit name
@metric("throughput")
def measure_throughput(target):
    _, out, _ = target.run("iperf3 -c 10.0.0.1 -t 5 -J")
    import json
    data = json.loads(out)
    return {"mbps": data["end"]["sum"]["bits_per_second"] / 1e6}

# Form 2: use function name as metric name
@metric
def error_rate(target):
    _, out, _ = target.run("curl -sf http://localhost/metrics | grep errors")
    return {"per_s": float(out.split()[-1])}

# Form 3: keyword form
@metric(name="connections")
def open_connections(target):
    _, out, _ = target.run("ss -tn | grep ESTAB | wc -l")
    return {"count": int(out.strip() or 0)}
```

Use in `@chaos_measure`:

```python
@chaos_measure(NetworkDelay("100ms"), metrics=[measure_throughput, error_rate])
def run():
    run_pipeline()
```

Retrieve from the global registry:

```python
from chaos_jungle.metrics.custom import get_metric, list_metrics

m = get_metric("throughput")    # → Metric instance or None
all_metrics = list_metrics()    # → {"throughput": ..., "error_rate": ...}
```

> **Pylance note**: Pylance may show a type error when using `@metric("name")` decorator syntax.
> Use the explicit form to avoid it:
> ```python
> from chaos_jungle.metrics import Metric, metric
> def _fn_impl(target) -> dict: ...
> my_metric: Metric = metric("name")(_fn_impl)  # type: ignore[assignment]
> ```

### ScriptMetric

Run a local or remote script; parse its stdout as metrics automatically.

```python
from chaos_jungle.metrics import ScriptMetric

# Local shell script (uploaded to target at collect time)
m = ScriptMetric("app", script="./scripts/measure.sh")

# Local Python script
m = ScriptMetric("app", script="./scripts/measure.py")

# Script already on the target
m = ScriptMetric("app", remote_script="/opt/app/metrics.sh")

# With extra arguments
m = ScriptMetric("db", script="./measure_db.sh",
                 extra_args="--host localhost --port 5432")
```

Script output format — JSON (recommended):

```bash
#!/bin/bash
echo '{"error_rate": 0.02, "throughput_mbps": 850.3, "latency_ms": 12.4}'
```

Or key=value:

```bash
error_rate=0.02
throughput_mbps=850.3
latency_ms=12.4
```

The metric name is used as a prefix: `ScriptMetric("app", ...)` produces keys `app_error_rate`, `app_throughput_mbps`, etc.

### Subclassing Metric

For full control, subclass `Metric` directly:

```python
from chaos_jungle.metrics import Metric

class RetransmitRate(Metric):
    name = "tcp_retrans"

    def collect(self, target) -> dict:
        _, out, _ = target.run(
            "awk '/^Tcp:/{getline; print $12}' /proc/net/snmp"
        )
        return {"retransmits": int(out.strip())}
```

---

## Dashboard

Built-in web dashboard powered by FastAPI + uvicorn. No extra dependencies beyond what chaos-jungle already requires.

```bash
chaos-jungle dashboard                          # → http://localhost:8050
chaos-jungle dashboard --host 0.0.0.0 --port 9090
```

Or from Python:

```python
from chaos_jungle.dashboard import run
run(host="127.0.0.1", port=8050)

# In a background thread
import threading
t = threading.Thread(target=run, kwargs={"port": 8050}, daemon=True)
t.start()
```

### Tabs

| Tab | Contents |
|-----|----------|
| **Overview** | KPI cards (total sessions, running, clean reverts, failures, avg duration), fault distribution bar chart, session status donut chart, recent 8 sessions |
| **Sessions** | Full paginated table with search (name / fault type / session ID) and status filter |
| **System Tools** | Shows which system binaries (`tc`, `ip`, `dd`, `filefrag`, etc.) are installed |
| **Logs** | Live tail of `~/.chaos-jungle/` log files, color-coded by severity |

Auto-refreshes every 6 seconds. Toggle light/dark theme with the button in the header.

### Session Detail Drawer

Click any session row to open a slide-in drawer with 4 inner tabs:

| Inner Tab | Contents |
|-----------|----------|
| **Summary** | Started/stopped timestamps, duration, command OK/error count, active `tc qdisc` rules, storage bit-flip table |
| **Metrics** | Baseline vs chaos comparison table with inline delta bars (color-aware: red = worse, green = better) |
| **Events** | Full event log as icon-tagged timeline (✓ start/stop, ✕ errors, ! warnings, · info) |
| **Faults** | All faults injected during the session with full parameter JSON |

---

## ExperimentSuite

Run multiple experiments in parallel on different targets, all from one YAML or Python config.

### YAML

```yaml
# suite.yml
duration: 10m
conflict: raise
auto_install: true

experiments:
  - name: control
    target: local
    faults: []

  - name: net-delay
    target: ssh://ubuntu@node1
    faults:
      - kind: NetworkDelay
        delay: 100ms
        jitter: 10ms

  - name: packet-loss
    target: ssh://ubuntu@node2
    faults:
      - kind: NetworkLoss
        rate: 5%

  - name: storage-corrupt
    target: ssh://ubuntu@node3
    duration: 5m
    faults:
      - kind: StorageCorrupt
        pattern: "*.pdb"
        directory: /scratch/data
```

```bash
chaos-jungle suite --config suite.yml
```

### Python

```python
from chaos_jungle.suite import ExperimentSuite
from chaos_jungle import Scenario
from chaos_jungle.faults import NetworkDelay, NetworkLoss
from chaos_jungle.targets import LocalTarget, SSHTarget

suite = ExperimentSuite(duration="10m")
suite.add(Scenario("control", []),                    LocalTarget())
suite.add(Scenario("delay",   [NetworkDelay("100ms")]), SSHTarget("node1", user="ubuntu"))
suite.add(Scenario("loss",    [NetworkLoss("5%")]),     SSHTarget("node2", user="ubuntu"))

results = suite.run(parallel=True)
ExperimentSuite.print_summary(results)
```

---

## CLI Reference

```
chaos-jungle start          Start fault injection
  --scenario NAME           Session name
  --delay DELAY             NetworkDelay e.g. 100ms
  --loss RATE               NetworkLoss e.g. 5%
  --corrupt RATE            NetworkCorrupt e.g. 1%
  --duration DURATION       Auto-stop after e.g. 10m
  --target URL              Target: ssh://user@host or http://host:port
  --conflict MODE           raise | warn | force (default: raise)

chaos-jungle stop           Stop and revert the active session
chaos-jungle status         Show current running session
chaos-jungle list           List all sessions with status / duration

chaos-jungle export         Write session data to a file
  --session ID              Export a specific session (default: latest)
  --format json|csv         Output format (default: json)
  --sessions                Export all sessions into one file
  --dir PATH                Write one file per session into PATH/
  --split                   One file per session (auto-named)

chaos-jungle fetch          Download DB + logs from a remote machine
  --target ssh://user@host  Remote host
  --output-dir PATH         Where to save (default: ./results/)

chaos-jungle suite          Run an ExperimentSuite from YAML
  --config FILE             Path to suite YAML file

chaos-jungle dashboard      Open live monitoring dashboard
  --host HOST               Bind host (default: 127.0.0.1)
  --port PORT               Bind port (default: 8050)

cj-daemon                   Start HTTP control daemon (run on target)
  --port PORT               Port to listen on (default: 7777)
  --token TOKEN             Bearer token for auth
  --host HOST               Bind host (default: 0.0.0.0)
```

---

## Data Storage & Export

All data is stored in `~/.chaos-jungle/chaos_jungle.db` (SQLite).

### Database Schema

| Table | Contents |
|-------|----------|
| `sessions` | Session ID, name, start/stop timestamps, status |
| `faults` | Fault kind and parameters JSON, linked to session |
| `events` | Every shell command, exit code, stdout/stderr |
| `results` | Workflow metrics JSON, linked to session |

### Export

```bash
# Latest session as JSON
chaos-jungle export

# Specific session as CSV
chaos-jungle export --session 5 --format csv

# All sessions into one CSV
chaos-jungle export --format csv --sessions

# One file per session in ./results/
chaos-jungle export --dir ./results/ --split

# Fetch from a remote SSH host (downloads DB + generates CSV)
chaos-jungle fetch --target ssh://ubuntu@worker1 --output-dir ./results/
```

From Python:

```python
from chaos_jungle.db import SessionDB

db = SessionDB()
df = db.to_dataframe()          # pandas DataFrame of all sessions
df.to_csv("experiments.csv")

# Single session
row = db.get_session(session_id=3)
```

---

## Guardrails & Validation

**Input validation** happens at `__init__` time — not at runtime:

```python
NetworkDelay("hundred")        # ValueError: must be like '100ms', '1s'
NetworkLoss("5")               # ValueError: must be like '5%', '0.5%'
StorageCorrupt("*.pdb", "data/")  # ValueError: 'directory' must start with '/'
```

**Guardrails** are checked before any fault starts:

| Condition | Default behavior |
|-----------|-----------------|
| Two tc-netem faults on same interface | `ConflictError` |
| tc rule already active from previous run | `ConflictError` |
| StorageCorrupt + NetworkCorrupt together | warning |

Control behavior with the `conflict` parameter:

```python
runner = ChaosRunner(scenario, target, conflict="warn")   # warn + continue
runner = ChaosRunner(scenario, target, conflict="force")  # overwrite existing rules
runner = ChaosRunner(scenario, target, conflict="raise")  # default: raise error
```

---

## HTTP Daemon

The daemon turns any Linux machine into a remotely controllable chaos target without requiring SSH.

### On the target machine

```bash
# Install chaos-jungle on the target first
pip install git+https://github.com/swarmourr/CJ.git

# Start the daemon
cj-daemon --port 7777 --token mysecret

# Or as a systemd service
```

### From the controller

```python
from chaos_jungle.targets import HTTPTarget
from chaos_jungle.faults import NetworkDelay
from chaos_jungle import ChaosRunner, Scenario

target = HTTPTarget("http://worker1:7777", token="mysecret")
runner = ChaosRunner(
    Scenario("net-delay", [NetworkDelay("100ms")]),
    target,
)
runner.run("10m")
```

### REST API (direct)

```bash
# Start a session
POST http://worker1:7777/start
Authorization: Bearer mysecret
{"scenario": "test", "faults": [{"kind": "NetworkDelay", "delay": "100ms"}]}

# Stop
POST http://worker1:7777/stop

# Status
GET  http://worker1:7777/status
```

---

## SSH Authentication

Auth is attempted in this order (same as OpenSSH client):

1. **Explicit key file** — `SSHTarget("h", user="u", key="~/.ssh/id_ed25519")`
2. **SSH agent** — if `ssh-add` was used, keys are picked up automatically
3. **Default key search** — Paramiko tries `~/.ssh/id_rsa`, `id_ecdsa`, `id_ed25519`
4. **Password** — `SSHTarget("h", user="u", password="...")`

Bypass agent and default keys for password-only:

```python
SSHTarget("host", user="ubuntu",
          password="secret",
          allow_agent=False,
          look_for_keys=False)
```

Required sudoers entry on the target:

```bash
echo "ubuntu ALL=(ALL) NOPASSWD: /sbin/tc, /bin/dd, /usr/sbin/filefrag, /usr/bin/python3" \
  | sudo tee /etc/sudoers.d/chaos-jungle
```

---

## Complete Example — Ollama LLM Testing

Test how an Ollama-based LLM agent behaves under network and LLM faults:

```python
from chaos_jungle.decorators import chaos_measure
from chaos_jungle.faults.llm import LLMLatency, LLMRateLimit, LLMUnavailable
from chaos_jungle.faults import NetworkDelay
from chaos_jungle.metrics import metric, Metric
from chaos_jungle.targets import LocalTarget
import requests

OLLAMA = "http://localhost:11434"

def _ollama_perf_fn(_) -> dict:
    resp = requests.post(f"{OLLAMA}/api/chat", json={
        "model": "llama3",
        "messages": [{"role": "user", "content": "Say hello in one word."}],
        "stream": False,
    }, timeout=120)
    data = resp.json()
    return {
        "tokens_per_s": data.get("eval_count", 0) / max(data.get("eval_duration", 1), 1) * 1e9,
        "total_duration_s": data.get("total_duration", 0) / 1e9,
    }

ollama_perf: Metric = metric("ollama")(_ollama_perf_fn)  # type: ignore[assignment]


@chaos_measure(
    LLMLatency(delay_s=3.0, upstream=OLLAMA, base_url_env="OLLAMA_HOST"),
    metrics=[ollama_perf],
    scenario_name="ollama-latency",
)
def test_llm_latency():
    """Run agent under 3 s artificial latency."""
    run_agent_workflow()
    return {"queries_completed": 10}


@chaos_measure(
    LLMUnavailable(upstream=OLLAMA, base_url_env="OLLAMA_HOST"),
    metrics=[ollama_perf],
    scenario_name="ollama-unavailable",
)
def test_llm_down():
    """Run agent when Ollama is completely unavailable."""
    run_agent_workflow()
    return {"queries_completed": 0}


if __name__ == "__main__":
    test_llm_latency()
    test_llm_down()
    # View in dashboard
    import subprocess
    subprocess.run(["chaos-jungle", "dashboard"])
```

---

*chaos-jungle — local README, do not push.*
