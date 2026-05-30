"""YAML configuration loader for chaos-jungle scenarios and suites.

YAML Schema
-----------

Suite config (``chaos-jungle suite --config my-suite.yml``)::

    duration: 10m          # default duration for all experiments (optional)
    conflict: raise        # raise | warn | force  (default: raise)
    auto_install: false    # auto apt-get install missing deps (default: false)

    experiments:
      - name: baseline
        target: local
        faults: []

      - name: net-delay
        target: ssh://ubuntu@node1
        duration: 5m       # per-experiment override
        faults:
          - kind: NetworkDelay
            delay: 100ms
            jitter: 10ms

      - name: net-loss
        target: ssh://ubuntu@node2
        faults:
          - kind: NetworkLoss
            rate: 5%

      - name: corruption
        target: ssh://ubuntu@node3
        faults:
          - kind: NetworkCorrupt
            rate: 1%

      - name: storage-corrupt
        target: ssh://ubuntu@node4
        faults:
          - kind: StorageCorrupt
            pattern: "*.pdb"
            directory: /scratch/data
            interval: 10m
            recursive: false

      - kind: SilentNetworkCorrupt
        rate: 5000
        hook: tc

Fault ``kind`` values
---------------------
* ``NetworkDelay``     — delay, jitter (optional), iface (optional)
* ``NetworkLoss``      — rate, iface (optional)
* ``NetworkCorrupt``   — rate, iface (optional)
* ``NetworkDuplicate`` — rate, iface (optional)
* ``StorageCorrupt``   — pattern, directory, interval (optional), recursive (optional)
* ``SilentNetworkCorrupt`` — rate (int), hook (tc|xdp, optional), iface (optional)

Target formats
--------------
* ``local``                  — run on the local machine
* ``ssh://user@host``        — SSH target
* ``ssh://user@host:port``   — SSH target with custom port
* ``http://host:port``       — HTTP daemon target
* ``https://host:port``      — HTTP daemon target (TLS)
"""

from __future__ import annotations
from typing import Any


# ── Fault builder ─────────────────────────────────────────────────

_FAULT_REGISTRY: dict[str, type] = {}


def _register_faults() -> None:
    """Lazily populate the fault registry on first use."""
    if _FAULT_REGISTRY:
        return
    from chaos_jungle.faults.network import (
        NetworkDelay, NetworkLoss, NetworkCorrupt, NetworkDuplicate,
    )
    from chaos_jungle.faults.storage import StorageCorrupt
    from chaos_jungle.faults.bpf import SilentNetworkCorrupt

    _FAULT_REGISTRY.update({
        "NetworkDelay": NetworkDelay,
        "NetworkLoss": NetworkLoss,
        "NetworkCorrupt": NetworkCorrupt,
        "NetworkDuplicate": NetworkDuplicate,
        "StorageCorrupt": StorageCorrupt,
        "SilentNetworkCorrupt": SilentNetworkCorrupt,
    })


def build_fault(spec: dict[str, Any]):
    """Build a :class:`~chaos_jungle.faults.base.Fault` from a dict.

    Parameters
    ----------
    spec :
        Dictionary with at least a ``kind`` key matching one of the
        supported fault class names.

    Returns
    -------
    Fault

    Raises
    ------
    ValueError
        If ``kind`` is missing or unknown.
    TypeError
        If the fault class rejects the supplied keyword arguments.

    Examples
    --------
    ::

        fault = build_fault({"kind": "NetworkDelay", "delay": "100ms", "jitter": "10ms"})
    """
    _register_faults()
    spec = dict(spec)
    kind = spec.pop("kind", None)
    if kind is None:
        raise ValueError("Each fault entry must have a 'kind' field.")
    cls = _FAULT_REGISTRY.get(kind)
    if cls is None:
        raise ValueError(
            f"Unknown fault kind: {kind!r}. "
            f"Valid kinds: {sorted(_FAULT_REGISTRY)}"
        )
    # Map YAML key names to constructor parameter names
    renamed = _rename_keys(kind, spec)
    return cls(**renamed)


def _rename_keys(kind: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Rename YAML-friendly keys to constructor parameter names where they differ."""
    # NetworkLoss / NetworkCorrupt / NetworkDuplicate: "rate" is positional arg
    # StorageCorrupt: "pattern" and "directory" are positional args
    # SilentNetworkCorrupt: "rate" is int
    if kind == "SilentNetworkCorrupt" and "rate" in spec:
        spec["rate"] = int(spec["rate"])
    return spec


# ── Target builder ────────────────────────────────────────────────

def build_target(target_str: str | None):
    """Build a :class:`~chaos_jungle.targets.base.Target` from a string.

    Parameters
    ----------
    target_str :
        One of:

        * ``None`` or ``"local"`` → :class:`~chaos_jungle.targets.local.LocalTarget`
        * ``ssh://user@host``     → :class:`~chaos_jungle.targets.ssh.SSHTarget`
        * ``ssh://user@host:port``
        * ``http://host:port``    → :class:`~chaos_jungle.targets.http.HTTPTarget`
        * ``https://host:port``

    Returns
    -------
    Target
    """
    from chaos_jungle.targets.local import LocalTarget
    from chaos_jungle.targets.ssh import SSHTarget
    from chaos_jungle.targets.http import HTTPTarget

    if not target_str or target_str.lower() == "local":
        return LocalTarget()
    if target_str.startswith("ssh://"):
        rest = target_str[6:]
        user, hostport = rest.split("@", 1)
        if ":" in hostport:
            host, port = hostport.rsplit(":", 1)
            return SSHTarget(host, user=user, port=int(port))
        return SSHTarget(hostport, user=user)
    if target_str.startswith("http://") or target_str.startswith("https://"):
        return HTTPTarget(target_str)
    raise ValueError(
        f"Unknown target format: {target_str!r}. "
        "Use 'local', 'ssh://user@host', or 'http://host:port'."
    )


# ── Scenario loader ───────────────────────────────────────────────

def load_scenario(spec: dict[str, Any]) -> tuple:
    """Build a ``(Scenario, Target, duration)`` tuple from a dict.

    Parameters
    ----------
    spec :
        Dict with keys: ``name``, ``target`` (optional), ``faults``,
        ``duration`` (optional).

    Returns
    -------
    tuple[Scenario, Target, str | None]
    """
    from chaos_jungle.scenario import Scenario

    name = spec.get("name")
    if not name:
        raise ValueError("Each experiment must have a 'name' field.")

    target_str = spec.get("target", "local")
    target = build_target(target_str)

    fault_specs = spec.get("faults", [])
    faults = [build_fault(f) for f in fault_specs]

    duration = spec.get("duration", None)
    return Scenario(name, faults), target, duration


# ── Suite loader ──────────────────────────────────────────────────

def load_suite(path: str):
    """Build an :class:`~chaos_jungle.suite.ExperimentSuite` from a YAML file.

    Parameters
    ----------
    path :
        Path to the YAML file.

    Returns
    -------
    ExperimentSuite

    Raises
    ------
    FileNotFoundError
        If the YAML file does not exist.
    ImportError
        If PyYAML is not installed.
    ValueError
        If the YAML is missing required fields.
    """
    try:
        import yaml
    except ImportError as exc:
        raise ImportError(
            "PyYAML is required to load YAML configs. "
            "Install it with: pip install pyyaml"
        ) from exc

    import os
    from chaos_jungle.suite import ExperimentSuite

    if not os.path.exists(path):
        raise FileNotFoundError(f"Suite config not found: {path}")

    with open(path, "r") as fh:
        data = yaml.safe_load(fh) or {}

    suite = ExperimentSuite(
        duration=data.get("duration", None),
        conflict=data.get("conflict", "raise"),
        auto_install=bool(data.get("auto_install", False)),
    )

    experiments = data.get("experiments", [])
    if not experiments:
        raise ValueError(f"Suite config {path!r} has no 'experiments' entries.")

    for exp_spec in experiments:
        scenario, target, duration = load_scenario(exp_spec)
        suite.add(scenario, target, duration=duration)

    return suite
