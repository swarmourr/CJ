# chaos-jungle examples

Six self-contained scenarios that demonstrate each unique capability of the library.

| File | Capability | Fault(s) |
|---|---|---|
| `measure_auto.py` | Automatic baseline vs fault measurement | `StorageCorrupt` |
| `fault_scheduling.py` | Inject fault mid-workload via `start_after` | `NetworkDelay` |
| `fault_composition.py` | Multiple faults simultaneously, compounding delta | `StorageCorrupt` + `NetworkDelay` |
| `silent_corrupt_detect.py` | BPF silent corruption — TCP passes, app-layer catches | `SilentNetworkCorrupt` |
| `llm_resilience.py` | LLM agent chain under latency, rate-limit, outage, hallucination | `LLMLatency` `LLMRateLimit` `LLMUnavailable` `LLMHallucination` |
| `ssh_remote.py` | Same scenario on a remote node — one-line target swap | `StorageCorrupt` via `SSHTarget` |

## Requirements

```bash
pip install chaos-jungle
sudo apt-get install -y iproute2   # tc — needed for NetworkDelay / NetworkCorrupt
sudo apt-get install -y bpfcc-tools linux-headers-$(uname -r)  # BCC — needed for SilentNetworkCorrupt
pip install openai                 # needed for llm_resilience.py
```

`silent_corrupt_detect.py` requires Linux + BCC and must run as root.
`llm_resilience.py` requires Ollama running locally (`ollama serve`).

## Run

```bash
# Scenario 1 — measurement framework
sudo python3 examples/measure_auto.py

# Scenario 2 — fault scheduling
sudo python3 examples/fault_scheduling.py

# Scenario 3 — fault composition
sudo python3 examples/fault_composition.py

# Scenario 4 — silent corruption (Linux + BCC only)
sudo python3 examples/silent_corrupt_detect.py

# Scenario 5 — LLM resilience (Ollama required)
python3 examples/llm_resilience.py

# Scenario 6 — remote SSH
python3 examples/ssh_remote.py --host worker1 --user ubuntu --password secret
```
