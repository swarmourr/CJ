# Injection Recipes

Use the narrowest injection mechanism that reaches the real operation under test.

## Best Method By Objective

| Objective | Preferred method | Why |
| --- | --- | --- |
| Test one Python function's HTTP behavior | `inject(...)` context manager | Smallest scope; automatic exit |
| Test a same-process agent using a supported HTTP client | `inject(...)` context manager | No proxy process or scenario boilerplate |
| Apply the same fault around a whole function/test | `@chaos(...)` or `chaos_session(...)` | Scope follows the workload |
| Compare local baseline and fault | `ChaosRunner.measure(...)` | Built-in repeated measurement and evidence |
| Test a cross-process/container model route | CJ proxy fault + `ChaosRunner` | Managed routing and objective proxy evidence |
| Target one agent role or step | Proxy fault with selector and CJ metadata | Precise and measurable when metadata propagation is verified |
| Stress networking/resources in a container | `DockerTarget` + infrastructure fault | Fault stays in the experiment namespace |
| Kill or pause the experiment container | `DockerContainerControllerTarget` + `ContainerKill` | Restricted host-side control of one container |
| Corrupt durable checkpoint state | State fault on a disposable persistent volume | Supports before/after and restart/resume verification |
| Coordinate multiple targets | `InjectionGroup` or distributed scenario | Explicit target membership and rollback policy |

## 1. Same-Process Interception

Use this for a narrow unit or integration experiment when the supported HTTP client and workload execute in the same Python process.

```python
from chaos_jungle import Latency, inject

with inject(Latency(0.5)) as injection:
    response = call_model_or_service()

print(injection)
```

Do not use this pattern for another process, a container, or a remote agent. The interceptor cannot affect traffic it cannot observe.

## 2. Function And Test Scope

Use a decorator when the complete function should run under one fault. Use a
context manager when setup or assertions must remain outside the fault window.

```python
from chaos_jungle import LLMLatency, chaos

@chaos(LLMLatency(delay_s=0.5, port=18021))
def test_agent_under_latency():
    return run_agent()
```

Use `chaos_measure(...)` when the function returns numeric metrics that CJ
should record. These interfaces are preferable to a scenario for ordinary
tests because scope and cleanup are visible in the code. They still use CJ's
managed lifecycle internally, but the caller does not need to construct a
`Scenario` explicitly.

For repeated same-process fault/rest cycles over supported HTTP clients, use
`door(...)` with intercept behaviors instead of building a scenario scheduler.

## 3. Managed LLM Proxy Injection

Use a managed proxy scenario for cross-process/container traffic, semantic or
tool mutation requiring proxy evidence, compound faults, or a reproducible
experiment lifecycle. Do not choose it solely because `Scenario` appears in
the package quickstart. Instantiate the model client after `runner.start()` if
the SDK captures its base URL during construction.

```python
import os
from chaos_jungle import ChaosRunner, LLMLatency, LocalTarget, Scenario

fault = LLMLatency(
    delay_s=0.5,
    port=18021,
    upstream=os.environ["LLM_UPSTREAM_URL"],
    base_url_env="OPENAI_BASE_URL",
)
runner = ChaosRunner(Scenario("slow-model", [fault]), LocalTarget())

runner.start()
try:
    # Construct the SDK client here so it reads the proxy URL exported by CJ.
    output = run_agent()
finally:
    runner.stop()
```

Best practice:

1. Keep the upstream URL and workload URL separate.
2. Allocate a unique proxy port.
3. Verify the proxy's registered fault before starting the workload.
4. Confirm intercepted and affected call counts independently.
5. Measure latency around CJ's injected sleep, not as fault-run minus control-run duration.

For Docker workloads, the host proxy URL seen by the container is commonly `http://host.docker.internal:<port>/v1`; the upstream configured in the host proxy remains the provider URL. Pass the container-reachable URL explicitly rather than relying on the host process environment.

## 4. Role-Scoped LLM Injection

Use a selector only after confirming the framework transmits role metadata to CJ.

```python
from chaos_jungle import ChaosRunner, LLMUnavailable, LocalTarget, Scenario

fault = LLMUnavailable(
    port=18022,
    upstream=upstream_url,
    selector={"agent_role": "reviewer"},
)
runner = ChaosRunner(Scenario("reviewer-outage", [fault]), LocalTarget())
```

The agent client must attach controlled CJ metadata for run ID, role, and step. Validate that non-reviewer calls are intercepted but unaffected and reviewer calls are affected. If metadata is missing, classify the selector as unsupported or the run as untriggered; never apply the fault globally as a fallback.

## 5. Tool Failure Injection

`ToolFault` matches requests containing an actual tool-result message. A prompt that merely talks about a tool will not trigger it.

```python
from chaos_jungle import ChaosRunner, LocalTarget, Scenario, ToolFault

fault = ToolFault(
    tool_name="search",
    port=18023,
    upstream=upstream_url,
    selector={"agent_role": "coder"},
)
runner = ChaosRunner(Scenario("search-tool-failure", [fault]), LocalTarget())
```

Before running a campaign, execute a healthy trace and prove that the selected tool is called. Record tool calls attempted, matched, affected, and completed separately. If the workflow never invokes the tool, the fault is untriggered.

## 6. Semantic Corruption

Use semantic faults when the request should remain valid HTTP and JSON while its meaning changes.

```python
from chaos_jungle import ChaosRunner, LocalTarget, Scenario, SemanticCorrupt

fault = SemanticCorrupt(
    mode="rag_poison",
    rag_poison_text="The deployment region is eu-west-9.",
    port=18024,
    upstream=upstream_url,
)
runner = ChaosRunner(Scenario("poisoned-context", [fault]), LocalTarget())
```

Use deterministic corruption for reproducible primary experiments. If an LLM generates false responses, store the generation model, prompt, seed where available, and generated artifact; analyze it as a separate stochastic condition.

## 7. Network Or Resource Fault Inside Docker

Create and start the constrained container before constructing the target. Add only the required capability, such as `NET_ADMIN` for `tc`.

```python
from chaos_jungle import ChaosRunner, NetworkDelay, Scenario
from chaos_jungle.targets import DockerTarget

target = DockerTarget(container_id, timeout_s=30)
fault = NetworkDelay("200ms", iface="eth0")
runner = ChaosRunner(Scenario("container-network-delay", [fault]), target)

runner.start()
try:
    result = execute_workload_in_same_container(container_id)
finally:
    runner.stop()
```

For CPU and memory, use `CPUStress` or `MemoryStress` with values below the container's hard limits and an automatic duration. For disk, use `DiskFull` only on a size-limited disposable volume. Verify both injector state and observed resource pressure.

## 8. Process Failure Inside Docker

Use a uniquely identifiable process pattern and a deterministic workload event.

```python
from chaos_jungle import ChaosRunner, ProcessKill, Scenario
from chaos_jungle.targets import DockerTarget

target = DockerTarget(container_id)
fault = ProcessKill("agent-worker --run-id run-123", signal="TERM")
runner = ChaosRunner(Scenario("worker-termination", [fault]), target)
```

Start the workload asynchronously, wait for a confirmed event such as the first LLM or tool call, then activate the fault. `ProcessKill` is irreversible; application recovery requires a supervisor or explicit restart and must not be inferred from CJ cleanup.

## 9. Container Failure

Use the restricted controller target because the target object is the container itself.

```python
from chaos_jungle import ChaosRunner, ContainerKill, Scenario
from chaos_jungle.targets import DockerContainerControllerTarget

target = DockerContainerControllerTarget(container_id)
fault = ContainerKill(container_id, action="pause")
runner = ChaosRunner(Scenario("pause-agent-container", [fault]), target)
```

Never use a general host target for this recipe. Confirm the exact container ID before activation and collect the Docker state transition. Prefer `pause`, `stop`, or `kill` over `rm` when recovery must be verified.

## 10. Durable State Corruption

Use a real checkpoint read/resume path, not merely a JSON file that the application ignores.

```python
from chaos_jungle import ChaosRunner, JsonStateCorrupt, Scenario
from chaos_jungle.targets import DockerTarget

fault = JsonStateCorrupt(
    "/checkpoints/agent.json",
    "messages[0].content",
    mutation="inject",
    inject_value="stale checkpoint content",
)
runner = ChaosRunner(Scenario("checkpoint-corruption", [fault]), DockerTarget(container_id))
```

Record the pre-fault checksum, mutation evidence, application resume/read event, detection or repair, post-revert checksum, and final task outcome. If the application never reads the checkpoint, the run is untriggered.

## 11. Measurement Pattern

When the same process can run both phases safely, prefer CJ's measurement interface:

```python
runner = ChaosRunner(scenario, target)
result = runner.measure(workload, n_baseline=5, n_fault=5)
print(result.summary())
```

For containerized or publication experiments, use separate fresh executions for direct baseline, CJ pass-through control, and CJ fault. Do not reuse mutated agent state across conditions.

## Pre-Activation Checklist

- The selected fault matches a real operation in a healthy trace.
- The target namespace is the intended blast radius.
- Required tools and narrow capabilities are present.
- Endpoint routing and proxy mode are verified.
- Fault severity, timeout, abort threshold, and cleanup are bounded.
- Activation and manifestation have different evidence sources where possible.
- `finally` cleanup exists, including container removal by the outer orchestrator.
- Secrets and full sensitive payloads will not enter artifacts.
