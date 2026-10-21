# LLM Proxy Routing

CJ proxy faults require three distinct endpoints:

1. **Upstream URL:** the real provider or local model server as seen by the host-side CJ proxy.
2. **Direct workload URL:** the provider as seen by the workload for the direct condition.
3. **CJ proxy URL:** the host-side proxy as seen by the workload for control/fault conditions.

Inside Docker, `127.0.0.1` means the container. Use an explicit Docker gateway or `host.docker.internal` where supported to reach a host-side proxy. Do not rewrite the proxy URL back to the upstream in the entrypoint.

## Correct Sequence

1. Configure the proxy fault with the host-reachable upstream.
2. Start `ChaosRunner` and verify activation/readiness.
3. Resolve the proxy address reachable from the workload namespace.
4. Start the workload with that condition-specific base URL.
5. Execute requests and collect the complete CJ proxy evidence.
6. Stop in `finally` and verify recovery.

For pass-through control, intercepted calls may be positive but affected calls must be zero. For a fault condition, intercepted and affected calls are separate counters.

## Scoped Injection

Selectors such as `{"agent_role": "reviewer"}` only work if request metadata reaches CJ. Propagate controlled internal metadata for run ID, role, and step. Confirm target coverage and selectivity:

- target coverage = affected matching calls / all matching calls;
- selectivity precision = affected matching calls / all affected calls.

Do not forward internal CJ metadata to the external provider unless required. If the framework cannot attach the metadata safely, report role scoping as unsupported.

## Common Routing Failures

- Workload still points directly to the provider: fault never triggers.
- Container points to `127.0.0.1`: it calls itself, not the host proxy.
- Pass-through process still owns the port: requested fault mode cannot activate.
- Proxy starts after the workload: early requests bypass injection.
- Base URL loses `/v1`: provider returns path/model errors.
- Multiple experiments reuse a port: readiness checks see another session.

Use unique ports, wait for the prior proxy to terminate, and verify the registered mode before starting the workload.
