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
import os
from typing import Any


class ConfigLoader:
    """Build chaos-jungle objects (Faults, Targets, Scenarios, Suites) from
    YAML configuration dicts or file paths.

    All methods are class methods — no instance needed::

        suite = ConfigLoader.load_suite("my-suite.yml")
        fault = ConfigLoader.build_fault({"kind": "NetworkDelay", "delay": "100ms"})
        target = ConfigLoader.build_target("ssh://ubuntu@node1")
    """

    _FAULT_REGISTRY: dict[str, type] = {}

    @classmethod
    def _register_faults(cls) -> None:
        if cls._FAULT_REGISTRY:
            return
        from chaos_jungle.faults.network import (
            NetworkDelay, NetworkLoss, NetworkCorrupt, NetworkDuplicate,
        )
        from chaos_jungle.faults.storage import StorageCorrupt
        from chaos_jungle.faults.bpf import SilentNetworkCorrupt

        cls._FAULT_REGISTRY.update({
            "NetworkDelay": NetworkDelay,
            "NetworkLoss": NetworkLoss,
            "NetworkCorrupt": NetworkCorrupt,
            "NetworkDuplicate": NetworkDuplicate,
            "StorageCorrupt": StorageCorrupt,
            "SilentNetworkCorrupt": SilentNetworkCorrupt,
        })

    @classmethod
    def build_fault(cls, spec: dict[str, Any]):
        """Build a :class:`~chaos_jungle.faults.base.Fault` from a dict.

        Parameters
        ----------
        spec :
            Dictionary with at least a ``kind`` key matching one of the
            supported fault class names.

        Raises
        ------
        ValueError
            If ``kind`` is missing or unknown.
        """
        cls._register_faults()
        spec = dict(spec)
        kind = spec.pop("kind", None)
        if kind is None:
            raise ValueError("Each fault entry must have a 'kind' field.")
        klass = cls._FAULT_REGISTRY.get(kind)
        if klass is None:
            raise ValueError(
                f"Unknown fault kind: {kind!r}. "
                f"Valid kinds: {sorted(cls._FAULT_REGISTRY)}"
            )
        return klass(**cls._rename_keys(kind, spec))

    @classmethod
    def _rename_keys(cls, kind: str, spec: dict[str, Any]) -> dict[str, Any]:
        if kind == "SilentNetworkCorrupt" and "rate" in spec:
            spec["rate"] = int(spec["rate"])
        return spec

    @classmethod
    def build_target(cls, target_str: str | None):
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

    @classmethod
    def load_scenario(cls, spec: dict[str, Any]) -> tuple:
        """Build a ``(Scenario, Target, duration)`` tuple from a dict.

        Parameters
        ----------
        spec :
            Dict with keys: ``name``, ``target`` (optional), ``faults``,
            ``duration`` (optional).
        """
        from chaos_jungle.core.scenario import Scenario

        name = spec.get("name")
        if not name:
            raise ValueError("Each experiment must have a 'name' field.")

        target = cls.build_target(spec.get("target", "local"))
        faults = [cls.build_fault(f) for f in spec.get("faults", [])]
        duration = spec.get("duration", None)
        return Scenario(name, faults), target, duration

    @classmethod
    def load_suite(cls, path: str):
        """Build an :class:`~chaos_jungle.core.suite.ExperimentSuite` from a YAML file.

        Parameters
        ----------
        path :
            Path to the YAML file.

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

        from chaos_jungle.core.suite import ExperimentSuite

        if not os.path.exists(path):
            raise FileNotFoundError(f"Suite config not found: {path}")

        with open(path) as fh:
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
            scenario, target, duration = cls.load_scenario(exp_spec)
            suite.add(scenario, target, duration=duration)

        return suite


# ── Module-level aliases for backwards compatibility ──────────────
def build_fault(spec: dict[str, Any]):
    return ConfigLoader.build_fault(spec)

def build_target(target_str: str | None):
    return ConfigLoader.build_target(target_str)

def load_scenario(spec: dict[str, Any]) -> tuple:
    return ConfigLoader.load_scenario(spec)

def load_suite(path: str):
    return ConfigLoader.load_suite(path)
