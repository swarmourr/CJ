# Target Selection

Choose the target from the fault's execution namespace, not from the controller's location.

| Intended subject | CJ target | Required condition | Do not use |
| --- | --- | --- | --- |
| Current host process or OS | `LocalTarget` | User explicitly accepts host impact | For a Docker-contained subject |
| Running experiment container namespace | `DockerTarget(container_id)` | Exact running container; needed tools/capabilities installed | Host fallback, Docker socket in container |
| Container object itself | `DockerContainerControllerTarget(container_id)` | Exact disposable container | General host shell commands |
| Remote machine over SSH | `SSHTarget` | Authorized host and credentials | Implicit localhost fallback |
| Deliberate remote CJ HTTP worker | `HTTPTarget` | Authenticated/restricted control endpoint | Arbitrary public endpoint |
| Same-process supported HTTP clients | `inject(...)` | Workload and interceptor share a Python process | Separate process/container traffic |

## Docker Rules

`DockerTarget` executes through `docker exec` inside one validated container. The container must be running before the target connects. Infrastructure orchestration therefore uses:

1. create the constrained container;
2. start it in an idle/running state;
3. construct and connect `DockerTarget`;
4. activate and verify the fault;
5. execute the workload in the same container;
6. revert and verify recovery;
7. remove the container in `finally`.

Network faults typically need only `NET_ADMIN`. Resource faults need bounded container CPU/memory limits and `stress-ng`. Disk faults must point at a size-limited disposable mount. Process faults require deterministic PID or event targeting.

Use `DockerContainerControllerTarget` only when the container itself is the subject, such as `ContainerKill`. It is intentionally restricted to supported Docker actions on its configured container ID.

## Reachability Check

Before activation, confirm the workload will exercise the fault:

- LLM fault: the client uses the CJ proxy base URL.
- Tool fault: a real matching tool call is guaranteed or observed.
- Role selector: the client sends the internal run/role/step metadata CJ expects.
- Network fault: traffic traverses the modified interface.
- Resource fault: the workload runs while the stressor is active.
- Process fault: the selected process exists and a deterministic trigger occurs.
- State fault: the application reads the corrupted checkpoint/state after mutation.

If the operation is not reached, classify the run as untriggered. Do not change selectors or metrics after seeing the outcome merely to improve results.
