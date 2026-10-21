---
name: chaos-jungle-injection
description: Design, implement, or review Chaos Jungle fault-injection experiments using the correct CJ fault, target, routing, lifecycle, safety boundary, and evidence. Use for LLM/API, tool, semantic, network, resource, process, container, state, or distributed fault injection with the chaos-jungle Python package.
---

# Chaos Jungle Injection

Use CJ at the smallest scope that reaches the real operation. Do not default to
`Scenario + ChaosRunner` for every request: a scenario is an orchestration and
evidence boundary, not the universally best injection interface.

## Establish The Boundary

Before writing code, determine:

1. Where the workload runs: current host, disposable Docker container, remote host, or HTTP-controlled agent.
2. What operation must be reached for the fault to trigger: LLM request, tool call, role/step, network path, process, resource, or durable state access.
3. What may be disrupted and what must remain unaffected.
4. What observable state proves activation, manifestation, and recovery.

If any of these are unknown, inspect the application and CJ package before choosing a fault. Do not silently broaden the target.

## Choose The Injection Path

- **Same-process supported HTTP client:** prefer `with inject(...)` for one scoped call or block. It has the smallest blast radius and automatic cleanup.
- **Whole function or test:** prefer `@chaos(...)`, `chaos_session(...)`, or `@chaos_measure(...)` when their scope matches the workload and no cross-process control is needed.
- **Managed LLM/API, response, semantic, and proxied tool experiment:** use the matching proxy fault with `ChaosRunner` only when requests cross a process/container boundary, CJ lifecycle evidence is required, multiple faults must be coordinated, or baseline-versus-fault measurement is requested.
- **Network/resource/process faults inside Docker:** use `DockerTarget(container_id)`. The container must already be running. Give it only the fault-specific capability and resource limits it needs.
- **Container kill/pause:** use `DockerContainerControllerTarget(container_id)`, restricted to that exact disposable container.
- **Remote host:** use `SSHTarget`; confirm authorization and blast radius.
- **HTTP-controlled worker:** use `HTTPTarget` only when the remote CJ control endpoint is intentionally deployed.
- **Host-local fault:** use `LocalTarget` only when the user explicitly intends to affect the current host. A containerized workload is not local merely because Docker runs locally.

Read [target selection](references/target-selection.md) when choosing or reviewing a target. Read [proxy routing](references/proxy-routing.md) for LLM, role-scoped, or tool faults.

For concrete implementation patterns, read [injection recipes](references/injection-recipes.md). Choose the narrowest recipe whose injection point is guaranteed to be reached; do not copy an infrastructure recipe onto the host merely because it is simpler.

## Define The Experiment

State the steady-state metric, resilience hypothesis, fault variable, expected evidence, blast-radius boundary, abort policy, observation window, and verified recovery condition before activation.

Prefer a healthy baseline, a CJ pass-through control, and a CJ fault condition when measuring impact. Keep task, model, seed, and configuration equivalent across conditions.

## Select The Simplest CJ Interface

Use this order unless the experiment requires a broader boundary:

1. `inject(...)` context manager for a small same-process block;
2. decorator/context helpers for one function or test;
3. `ChaosRunner.measure(...)` for local baseline-versus-fault measurement;
4. `Scenario + ChaosRunner` for managed lifecycle, evidence, scheduling, remote targets, compound faults, or reproducible campaigns;
5. injection groups/distributed coordination only for genuine multi-target activation.

Do not introduce a scenario merely to make a short code example look complete.

## Implement Against The Installed CJ Version

Inspect local signatures or documentation before emitting executable code. Import public names from `chaos_jungle` or their documented modules. Do not invent constructor parameters, lifecycle fields, CLI flags, or result attributes.

Use `try/finally` whenever lifecycle is managed manually:

```python
runner.start()
try:
    result = workload()
finally:
    runner.stop()
```

Prefer `runner.measure(...)` for baseline-versus-fault measurement. Record task outcomes separately from operational failures.

## Verify, Then Interpret

Treat configured, activated, triggered, manifested, reverted, and recovered as independent lifecycle stages. A successful `start()` is not manifestation evidence. A successful `stop()` is not recovery evidence. An active fault with no matching call is `untriggered`, not a failed agent and not a valid resilience observation.

Read [evidence and reporting](references/evidence.md) before claiming that an injection worked.

## Safety Invariants

- Never apply network, disk, CPU, memory, process, or similar infrastructure faults through `LocalTarget` when the intended subject is a container.
- Never fall back from `DockerTarget` to the host.
- Never mount the Docker socket into an experiment container or use Docker-in-Docker.
- Use disposable volumes for disk and storage faults.
- Do not use privileged containers when a narrow capability such as `NET_ADMIN` is sufficient.
- Validate identifiers, limits, paths, timeout, cleanup, and abort thresholds before activation.
- Keep secrets in environment variables; redact them from commands, logs, evidence, and artifacts.
- If required targeting or evidence is unsupported, report the combination as unsupported instead of simulating success.

## Completion Standard

Return the runnable experiment, target/routing explanation, safety boundary, and observed lifecycle evidence. Clearly distinguish what was statically checked, mocked, run locally, run in Docker, or left unverified.
