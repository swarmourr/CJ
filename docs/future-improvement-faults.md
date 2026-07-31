# Future Improvement Faults

This document tracks identified weaknesses in the current chaos-jungle fault library and the new fault classes / fixes that should be implemented.

---

## 1. Network Layer

### Missing Fault Classes

#### `NetworkBandwidthLimit` ✅ **Implemented**
Throttle interface bandwidth using `tc netem rate`.

```python
fault = NetworkBandwidthLimit("1mbit", iface="eth0")
```

#### `NetworkReorder` ✅ **Implemented**
Simulate out-of-order packet delivery using `tc netem reorder`.

```python
fault = NetworkReorder(rate="25%", iface="eth0")
```

#### `NetworkReset` ✅ **Implemented**
Inject TCP RST events via `iptables REJECT --reject-with tcp-reset`. Tagged with UUID comment for safe per-instance removal.

```python
fault = NetworkReset(dport=443, direction="OUTPUT")
```

#### `NetworkPartition` ✅ **Implemented**
Block all traffic to a specific IP using `iptables DROP`. Tagged with UUID comment; supports optional INPUT block.

```python
fault = NetworkPartition(dest_ip="10.0.0.2", block_input=True)
```

---

### Bugs / Realism Gaps

| ID | Description | Severity | Status |
|----|-------------|----------|--------|
| NET-1 | `tc qdisc add` silently fails if a qdisc already exists. Should use `tc qdisc replace` or check first. Fault appears to start but does nothing. | Critical | ✅ **Fixed** — all tc faults now use `tc qdisc replace` |
| NET-2 | All network faults apply to the entire interface — no IP/port targeting. Real chaos should target specific services. | High | Open |
| NET-3 | Combining two network fault classes (e.g., `NetworkDelay` + `NetworkLoss`) creates two conflicting qdiscs. Should compose into a single `tc netem delay X loss Y` rule. | High | Open |
| NET-4 | All `tc netem` faults are Linux-only. macOS has no `tc`. Developers on macOS laptops cannot run network faults locally. | Medium | Open |
| NET-5 | No IPv6 support in interface detection or tc commands. | Low | Open |

---

## 2. Resource Exhaustion

### Missing Fault Classes

#### `InodeFull` ✅ **Implemented**
Exhaust filesystem inodes by creating millions of tiny files — a filesystem can have free space but zero inodes.

```python
fault = InodeFull(path="/tmp", count=500_000)
```

#### `FDExhaust` ✅ **Implemented**
Exhaust file descriptor limits by opening files until the process or system `ulimit` is hit.

```python
fault = FDExhaust(count=60_000)
```

#### `ProcessExhaust` ✅ **Implemented**
Hit the kernel PID limit by forking many short-lived processes. `stop()` kills the entire process group via `kill -KILL -$pid`.

```python
fault = ProcessExhaust(count=5_000)
```

#### `CgroupCPULimit`
Apply a cgroup CPU quota to a specific process/container instead of stressing the whole machine.

```python
fault = CgroupCPULimit(cgroup="/sys/fs/cgroup/myapp", quota_pct=10)
```

#### `SwapPressure`
Force swap thrashing by allocating memory beyond RAM capacity.

```python
fault = SwapPressure(mb=8192)
```

---

### Bugs / Realism Gaps

| ID | Description | Severity | Status |
|----|-------------|----------|--------|
| RES-1 | `CPUStress`, `MemoryStress`, `IOStress` use `pkill -f 'stress-ng --cpu'` in `stop()`. This kills ALL matching stress-ng processes on the machine. Breaks concurrent test runs. | Critical | ✅ **Fixed** — all three faults now use unique UUID PID files and kill by PID |
| RES-2 | `DiskFull` has no preflight check for available disk space. On a nearly-full disk it writes a partial fill file silently and appears to succeed. | High | Open |
| RES-3 | `stress-ng` affects the whole machine, not a specific container or cgroup. In containerized workloads this is unrealistic. | Medium | Open |

---

## 3. Process / Service

### Missing Fault Classes

#### `PodFault`
Kill, evict, or delete a Kubernetes pod.

```python
fault = PodFault("my-agent-pod", action="delete", namespace="default")
```

#### `DeploymentScaleDown`
Scale a Kubernetes Deployment to zero replicas.

```python
fault = DeploymentScaleDown("agent-deployment", namespace="default")
```

#### `ContainerNetworkIsolate`
Disconnect a Docker container from its network without killing it (`docker network disconnect`).

```python
fault = ContainerNetworkIsolate("my-agent")
```

---

### Bugs / Realism Gaps

| ID | Description | Severity |
|----|-------------|----------|
| PROC-1 | `ServiceFault` assumes systemd. Containers and older servers running supervisor, SysV init, or runit will silently fail. | High |
| PROC-2 | `pkill -f` pattern matching against the full command line can accidentally match unintended processes on shared machines. No dry-run/confirmation mode. | High |
| PROC-3 | `ProcessKill` is irreversible by design but no helper is provided to restart the killed process. For supervised workers (gunicorn, celery) this is a common need. | Medium |

---

## 4. Storage

### Missing Fault Classes

#### `StorageCorruptImmediate` ✅ **Implemented**
Corrupt a specific file immediately (not via crontab scheduling) — a simpler alternative to `StorageCorrupt`. Backs up the original so `revert()` restores it exactly.

```python
fault = StorageCorruptImmediate("/data/model.bin", offset=1024, byte_count=16)
```

#### `SQLiteCorrupt` ✅ **Implemented**
Corrupt a specific page in a SQLite database file using `dd`. Page 1 (first data page) is the default — avoids the header, triggers `database disk image is malformed`.

```python
fault = SQLiteCorrupt("/var/agent/state.db", page=1, page_size=4096)
```

#### `ReadOnlyMount`
Remount a filesystem as read-only.

```python
fault = ReadOnlyMount("/var/lib/data")
```

#### `NFSTimeout`
Simulate an NFS mount becoming unresponsive (block writes via `iptables` to the NFS server).

```python
fault = NFSTimeout(nfs_server_ip="10.0.1.5")
```

---

### Bugs / Realism Gaps

| ID | Description | Severity | Status |
|----|-------------|----------|--------|
| STR-1 | `StorageCorrupt` uses crontab scheduling — minimum effective interval is seconds, not milliseconds. Cannot inject corruption at a precise moment during a test. | High | ✅ **Addressed** — `StorageCorruptImmediate` fires instantly at `start()` |
| STR-2 | Crontab residue: if `stop()` crashes before removing the crontab entry, corruption continues running indefinitely after the test ends. | High | Open |
| STR-3 | Heavy setup: deploys 3 Python scripts and requires the `python-crontab` pip package. Fragile on air-gapped machines. | Medium | Open |

---

## 5. LLM / AI API Faults

### Missing Fault Classes

#### `LLMUnauthorized` ✅ **Implemented**
Return HTTP 401 to simulate an expired or invalid API key. Supports `after_n` to let N requests succeed first. Adds realistic `response_delay_s` + `jitter_s` before the error response.

```python
fault = LLMUnauthorized()
fault = LLMUnauthorized(after_n=3)   # first 3 succeed, then 401
```

#### `LLMForbidden` ✅ **Implemented**
Return HTTP 403 to simulate a permission boundary or policy violation. Includes realistic delay.

```python
fault = LLMForbidden()
```

#### `LLMAuthExpiry` ✅ **Implemented**
Return successful responses for the first N calls, then switch to 401 — simulates a token that expires mid-session.

```python
fault = LLMAuthExpiry(valid_calls=5)
```

#### `LLMContextLengthExceeded` ✅ **Implemented**
Return HTTP 400 with `context_length_exceeded` to test chunking / summarization fallbacks.

```python
fault = LLMContextLengthExceeded()
```

#### `LLMPartialStreamCorrupt`
Corrupt only a specific SSE chunk in a streaming response, leaving the rest intact.

```python
fault = LLMPartialStreamCorrupt(chunk_index=3)
```

---

### Bugs / Realism Gaps

| ID | Description | Severity | Status |
|----|-------------|----------|--------|
| LLM-1 | Blocking faults return error responses in microseconds. Real API errors arrive after TCP+TLS+processing time (50–300 ms). | Critical | ✅ **Fixed** — `response_delay_s` + `jitter_s` added to all blocking fault types in the proxy |
| LLM-2 | No auth faults (401/403). Auth failures are the most common real-world API failure mode. | Critical | ✅ **Fixed** — `LLMUnauthorized`, `LLMForbidden`, `LLMAuthExpiry` implemented |
| LLM-3 | Single upstream only — each `_LLMProxyFault` targets one upstream URL. Cannot simultaneously simulate "OpenAI slow, Anthropic rate-limited". | High | Open |
| LLM-4 | `MODEL_PRICING` table is static and out of date. No Claude 4.x or Gemini 2.x pricing. | High | Open |
| LLM-5 | `LLMHallucination` injects a fixed static string. Does not test subtle, contextually plausible hallucination detection. | Medium | Open |
| LLM-6 | `SemanticCorrupt` uses regex entity substitution on the raw response body. Can break structured JSON outputs. | Medium | Open |
| LLM-7 | Proxy startup uses fixed `time.sleep(0.4)` — not a readiness health-check. On slow machines requests pass through without the fault. | Medium | ✅ **Fixed** — proxy exposes `/_cj/health`; `start()` now polls until ready (5 s timeout) |
| LLM-8 | `MCPFault` only targets exact tool names — no wildcard or prefix matching. | Low | Open |

---

## 6. Skill File Faults

### Missing Fault Classes

#### `SkillURLUnavailable`
Simulate a skill file loaded from an HTTP endpoint becoming unavailable (404 / timeout).

```python
fault = SkillURLUnavailable("https://skills.internal/search_web")
```

#### `SkillJSONCorrupt` ✅ **Implemented**
Corrupt a JSON-format tool definition (OpenAI function calling format) using dot-notation field addressing.

```python
fault = SkillJSONCorrupt("skills/search_web.json", field="parameters.properties")
fault = SkillJSONCorrupt("skills/search_web.json", field="description", corrupt_value="")
```

---

### Bugs / Realism Gaps

| ID | Description | Severity | Status |
|----|-------------|----------|--------|
| SKILL-1 | All skill file faults assume local filesystem. Skills loaded from S3, HTTP endpoints, or databases are not covered. | High | Open |
| SKILL-2 | `SkillFilePermissionDenied` uses `chmod 000`. If the process crashes before `stop()` runs, the file is left permanently unreadable. | High | ✅ **Fixed** — original mode persisted to a `.cj_mode` sidecar file before `chmod 000`; `stop()` reads sidecar for crash recovery |
| SKILL-3 | `SkillFileVersionSkew` only manipulates YAML frontmatter. Plain JSON tool definitions are not supported. | Medium | ✅ **Addressed** — `SkillJSONCorrupt` handles JSON definitions directly |
| SKILL-4 | `LLMSkillFaultGenerator` makes a live API call during `start()`. This adds network latency and a failure mode to fault injection itself. | Medium | Open |

---

## 7. State Faults

### Missing Fault Classes

#### `VectorDBCorrupt`
Corrupt embeddings or metadata in a vector store (Weaviate, Pinecone, Chroma, Qdrant) to test RAG retrieval quality under bad memory.

```python
fault = VectorDBCorrupt(
    collection="agent_memory",
    mutation="nullify_metadata",
    condition="source='user_profile'",
)
```

#### `MemcachedStateCorrupt`
Corrupt or delete Memcached keys matching a pattern.

```python
fault = MemcachedStateCorrupt("session:*", mutation="delete")
```

#### `SQLiteStateCorrupt`
Corrupt a field in a SQLite table — common for LangChain SQLite checkpointer and local agent state.

```python
fault = SQLiteStateCorrupt(
    db_path="/var/agent/state.db",
    table="checkpoints",
    column="state_json",
    mutation="nullify",
)
```

---

### Bugs / Realism Gaps

| ID | Description | Severity |
|----|-------------|----------|
| STATE-1 | `RedisStateCorrupt` silently skips non-string key types (hashes, lists, sorted sets). Most agent frameworks (LangGraph, AutoGen) store state in Redis Hashes — so this fault does nothing in the most common case. | Critical |
| STATE-2 | `RedisStateCorrupt` uses `KEYS` (O(N), blocks Redis) instead of `SCAN` (non-blocking cursor). Dangerous on production Redis instances with large keyspaces. | High |
| STATE-3 | No vector DB fault coverage. RAG-based agents (the dominant architecture pattern) have no memory corruption testing. | High |
| STATE-4 | `JsonStateCorrupt` writes via shell heredoc. Breaks silently if the JSON contains the sentinel string `CJ_EOF`. | Medium |
| STATE-5 | `PostgresStateCorrupt._psql()` quote escaping is fragile for complex inject values containing single quotes. Should use a temp SQL file with `psql -f` instead. | Medium |

---

## 8. GPU Faults

### Missing Fault Classes

#### `GPUErrorInjection`
Simulate CUDA/HIP runtime errors or ECC memory errors to test ML framework error recovery.

```python
fault = GPUErrorInjection(error_type="ecc_dbe", gpu_id=0)
```

#### `GPUTemperatureAlarm`
Trigger the GPU thermal warning threshold via sysfs to test throttling behavior.

```python
fault = GPUTemperatureAlarm(target_temp_c=85, gpu_id=0)
```

---

### Bugs / Realism Gaps

| ID | Description | Severity |
|----|-------------|----------|
| GPU-1 | `GPUThrottle` and `GPUClockLock` have no-op `stop()` — only `revert()` restores state. Calling `start()`/`stop()` (without `revert()`) permanently leaves the GPU throttled. | Critical |
| GPU-2 | `GPUMemoryPressure` uses ctypes scripts that are CUDA/HIP version-specific. Breaks silently on driver version mismatches with no error message. | High |
| GPU-3 | No MIG (Multi-Instance GPU) or vGPU partition support for shared-GPU cloud environments. | Medium |

---

## 9. Intercept Layer (`inject()`)

### New Behaviors ✅ **Implemented**

#### `Unauthorized`
Return HTTP 401 with realistic delay at the intercept layer. Supports `after_n` to let N requests succeed first.

```python
with inject(Unauthorized(after_n=3, response_delay_s=0.1)):
    agent.run(...)
```

#### `Forbidden`
Return HTTP 403 with realistic delay.

```python
with inject(Forbidden()):
    agent.run(...)
```

#### `AuthExpiry`
Simulate a token that expires mid-session. First `valid_calls` requests succeed; subsequent ones return 401.

```python
with inject(AuthExpiry(valid_calls=5)):
    for _ in range(10):
        client.chat.completions.create(...)
```

---

### Bugs / Realism Gaps

| ID | Description | Severity | Status |
|----|-------------|----------|--------|
| INT-1 | `inject()` patches `httpx` and `requests` at the module level. If tests run in parallel threads, faults from one test leak into all other threads simultaneously. | Critical | Open |
| INT-2 | The "asyncio single-thread" assumption breaks if the agent uses `anyio` with thread workers or `concurrent.futures` thread pool executors. | High | Open |
| INT-3 | Streaming SSE responses pass through without per-chunk injection. Only full response-level manipulation is possible. | Medium | Open |

---

## 10. Framework / Architecture

| ID | Description | Severity |
|----|-------------|----------|
| ARCH-1 | No fault timeout / auto-revert. If `stop()` is never called (process crash, unhandled exception), faults remain active indefinitely. A `max_duration_s` parameter and background watchdog thread would prevent this. | Critical |
| ARCH-2 | SQLite session DB has no concurrent-write protection. Parallel test runs corrupt each other's session records. | High |
| ARCH-3 | No distributed fault synchronization. `SSHTarget` applies faults to multiple nodes sequentially, not simultaneously. Multi-node experiments have timing skew between fault starts. | High |
| ARCH-4 | `HTTPTarget` does not support `put()` (file upload). Storage, GPU, and BPF faults that require deploying scripts cannot run against HTTP-managed targets. | High |
| ARCH-5 | No Windows support. `tc`, `chmod`, `pkill`, systemd, `/tmp` paths — the entire framework assumes POSIX. | Medium |
| ARCH-6 | `_LLMProxyFault.start()` uses a fixed `time.sleep(0.4)` readiness wait. Should poll `GET /health` until the proxy responds or timeout. | Medium |

---

## Priority Summary

| Priority | ID | Title | Status |
|----------|----|-------|--------|
| P0 | NET-1 | `tc qdisc add` silent no-op when qdisc already exists | ✅ Fixed |
| P0 | LLM-1 | Blocking faults return errors in microseconds — unrealistically fast | ✅ Fixed |
| P0 | LLM-2 | No auth faults (401 / 403 / expiry) | ✅ Fixed |
| P0 | STATE-1 | `RedisStateCorrupt` silently skips hash-type keys | Open |
| P0 | GPU-1 | `GPUThrottle`/`GPUClockLock` `stop()` is a no-op | Open |
| P1 | RES-1 | `stress-ng` `stop()` kills all matching processes globally | ✅ Fixed |
| P1 | STATE-2 | `KEYS` blocks Redis — should use `SCAN` | Open |
| P1 | STATE-3 | No vector DB fault coverage for RAG agents | Open |
| P1 | INT-1 | `inject()` is thread-unsafe — parallel test fault leakage | Open |
| P1 | ARCH-1 | No fault timeout / auto-revert on crash | Open |
| P2 | LLM-4 | `MODEL_PRICING` table out of date | Open |
| P2 | STR-1 | `StorageCorrupt` cannot inject at a precise moment | ✅ Addressed (`StorageCorruptImmediate`) |
| P2 | LLM-7 | Proxy startup race (`time.sleep(0.4)`) | ✅ Fixed |
| P2 | SKILL-2 | `SkillFilePermissionDenied` leaves file unreadable on crash | ✅ Fixed |
| P2 | PROC-1 | `ServiceFault` assumes systemd | Open |
| P2 | ARCH-3 | No distributed fault synchronization | Open |
