"""YAML configuration loader for chaos-jungle scenarios and suites.

YAML Schema
-----------

Suite config (``chaos-jungle suite --config my-suite.yml``)::

    apiVersion: cj.io/v1          # required — enables schema validation
    kind: ExperimentSuite          # required
    duration: 10m                  # default duration for all experiments (optional)
    conflict: raise                # raise | warn | force  (default: raise)
    auto_install: false            # auto apt-get install missing deps (default: false)

    experiments:
      - name: baseline
        target: local
        faults: []

      - name: llm-latency
        target: local
        duration: 5m
        faults:
          - kind: LLMLatency
            delay_s: 2.0

      - name: net-delay
        target: ssh://ubuntu@node1
        duration: 5m
        faults:
          - kind: NetworkDelay
            delay: 100ms
            jitter: 10ms

      - name: net-loss
        target: ssh://ubuntu@node2
        faults:
          - kind: NetworkLoss
            rate: 5%

      - name: storage-corrupt
        target: ssh://ubuntu@node4
        faults:
          - kind: StorageCorrupt
            pattern: "*.pdb"
            directory: /scratch/data

      - kind: SilentNetworkCorrupt
        rate: 5000
        hook: tc

Supported fault ``kind`` values (allowlisted — no arbitrary imports)
---------------------------------------------------------------------
Network layer:
  NetworkDelay, NetworkLoss, NetworkCorrupt, NetworkDuplicate,
  NetworkBandwidthLimit, NetworkReorder, NetworkReset, NetworkPartition,
  SilentNetworkCorrupt

LLM layer (study faults):
  LLMLatency, LLMTimeout, LLMRateLimit, LLMResponseCorrupt, LLMUnavailable,
  LLMHallucination, LLMStreamInterrupt, LLMTokenStarvation,
  LLMUnauthorized, LLMForbidden, LLMAuthExpiry, LLMContextLengthExceeded,
  SemanticCorrupt, ToolFault, MCPFault

Process / service / container:
  ProcessKill, ServiceFault, ContainerKill

Storage:
  StorageCorrupt, StorageCorruptImmediate, SQLiteCorrupt

State:
  RedisStateCorrupt, JsonStateCorrupt, PostgresStateCorrupt

Resource exhaustion:
  DiskFull, CPUStress, MemoryStress, IOStress, InodeFull, FDExhaust,
  ProcessExhaust

AI Gateway:
  GatewayRouteMisconfig, GatewayFallbackBroken, GatewayPolicyBlock,
  GatewayPolicyBypass, GatewayCacheStale, GatewayCachePoison,
  GatewayTenantLeak, GatewayHeaderStrip, GatewayToolSchemaDrop,
  GatewayResponseRewrite, GatewayBudgetDesync, GatewayRetryStorm

Skill / tool:
  SkillUnavailable, SkillMisroute, SkillInstructionCorrupt,
  SkillDependencyMissing, SkillTimeout, SkillBadOutput, SkillVersionSkew,
  SkillPermissionDenied, SkillMemoryStale, ConflictingSkills,
  SkillFileUnavailable, SkillFileInstructionCorrupt, SkillFileVersionSkew,
  SkillFileBadOutput, SkillFileMemoryStale, SkillFileConflict,
  SkillFilePermissionDenied, SkillJSONCorrupt

GPU:
  GPUThrottle, GPUMemoryPressure, GPUClockLock

Target formats
--------------
* ``local``                  — run on the local machine
* ``ssh://user@host``        — SSH target
* ``ssh://user@host:port``   — SSH target with custom port
* ``http://host:port``       — HTTP daemon target
* ``https://host:port``      — HTTP daemon target (TLS)

Schema validation
-----------------
Run ``chaos-jungle suite validate --config my-suite.yml`` before executing
to catch unknown fields, unsupported fault types, invalid durations, etc.

Dry-run plan output
-------------------
Run ``chaos-jungle suite plan --config my-suite.yml`` to see the resolved
ExperimentPlan for each experiment without running anything.
"""

from __future__ import annotations
import os
import re
from typing import Any


def _parse_duration(value: "str | int | float") -> float:
    """Convert a duration string or number to seconds (float).

    Accepted formats: ``"5s"``, ``"100ms"``, ``"2m"``, ``"1h"``, or a bare
    number (already in seconds).
    """
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h)?", s, re.IGNORECASE)
    if not m:
        raise ValueError(f"Cannot parse duration: {value!r}")
    n, unit = float(m.group(1)), (m.group(2) or "s").lower()
    return n * {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}[unit]

# Known YAML top-level keys — unknown keys trigger validation warnings.
_SUITE_KNOWN_KEYS = frozenset({
    "apiVersion", "kind", "duration", "conflict", "auto_install", "experiments",
})
_EXPERIMENT_KNOWN_KEYS = frozenset({
    "name", "target", "faults", "duration", "description", "hypothesis",
    "observations", "safety", "cleanup",
})
_FAULT_KNOWN_KEYS = frozenset({
    "kind",
    # Network
    "delay", "jitter", "iface", "rate", "hook",
    # LLM
    "delay_s", "max_requests", "timeout_s", "mode", "text",
    "after", "max_tokens", "tool_name", "error_type", "method",
    "wrong_skill", "conflict_text", "upstream", "port",
    "budget_input_price", "budget_output_price",
    # Storage
    "pattern", "directory", "interval", "recursive",
    # Process
    "service", "container", "signal",
    # Resources
    "size_mb", "percent", "workers", "read_bps", "write_bps",
    "count", "path",
    # State
    "key_pattern", "table", "column",
    # GPU
    "clock_mhz", "mem_fraction",
    # Skill
    "skill_id", "target_skill",
    # Gateway
    "route", "header", "policy",
})


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
        # ── Network ────────────────────────────────────────────────────────
        from chaos_jungle.faults.network import (
            NetworkDelay, NetworkLoss, NetworkCorrupt, NetworkDuplicate,
            NetworkBandwidthLimit, NetworkReorder, NetworkReset, NetworkPartition,
        )
        from chaos_jungle.faults.bpf import SilentNetworkCorrupt
        # ── LLM / Tool / MCP ───────────────────────────────────────────────
        from chaos_jungle.faults.llm import (
            LLMLatency, LLMTimeout, LLMRateLimit, LLMResponseCorrupt,
            LLMUnavailable, LLMHallucination, LLMStreamInterrupt,
            LLMTokenStarvation, LLMUnauthorized, LLMForbidden,
            LLMAuthExpiry, LLMContextLengthExceeded, SemanticCorrupt,
            ToolFault, MCPFault,
        )
        # ── Storage ────────────────────────────────────────────────────────
        from chaos_jungle.faults.storage import (
            StorageCorrupt, StorageCorruptImmediate, SQLiteCorrupt,
        )
        # ── Process / service / container ─────────────────────────────────
        from chaos_jungle.faults.process import (
            ProcessKill, ServiceFault, ContainerKill,
        )
        # ── State ──────────────────────────────────────────────────────────
        from chaos_jungle.faults.state import (
            RedisStateCorrupt, JsonStateCorrupt, PostgresStateCorrupt,
        )
        # ── Resource exhaustion ────────────────────────────────────────────
        from chaos_jungle.faults.resources import (
            DiskFull, CPUStress, MemoryStress, IOStress,
            InodeFull, FDExhaust, ProcessExhaust,
        )
        # ── GPU ────────────────────────────────────────────────────────────
        from chaos_jungle.faults.gpu import (
            GPUThrottle, GPUMemoryPressure, GPUClockLock,
        )
        # ── Skill / tool ───────────────────────────────────────────────────
        from chaos_jungle.faults.skill import (
            SkillUnavailable, SkillMisroute, SkillInstructionCorrupt,
            SkillDependencyMissing, SkillTimeout, SkillBadOutput,
            SkillVersionSkew, SkillPermissionDenied, SkillMemoryStale,
            ConflictingSkills,
        )
        from chaos_jungle.faults.skill_file import (
            SkillFileUnavailable, SkillFileInstructionCorrupt,
            SkillFileVersionSkew, SkillFileBadOutput, SkillFileMemoryStale,
            SkillFileConflict, SkillFilePermissionDenied, SkillJSONCorrupt,
        )
        # ── AI Gateway ─────────────────────────────────────────────────────
        from chaos_jungle.faults.gateway import (
            GatewayRouteMisconfig, GatewayFallbackBroken, GatewayPolicyBlock,
            GatewayPolicyBypass, GatewayCacheStale, GatewayCachePoison,
            GatewayTenantLeak, GatewayHeaderStrip, GatewayToolSchemaDrop,
            GatewayResponseRewrite, GatewayBudgetDesync, GatewayRetryStorm,
        )

        cls._FAULT_REGISTRY.update({
            # Network
            "NetworkDelay": NetworkDelay,
            "NetworkLoss": NetworkLoss,
            "NetworkCorrupt": NetworkCorrupt,
            "NetworkDuplicate": NetworkDuplicate,
            "NetworkBandwidthLimit": NetworkBandwidthLimit,
            "NetworkReorder": NetworkReorder,
            "NetworkReset": NetworkReset,
            "NetworkPartition": NetworkPartition,
            "SilentNetworkCorrupt": SilentNetworkCorrupt,
            # LLM / Tool / MCP
            "LLMLatency": LLMLatency,
            "LLMTimeout": LLMTimeout,
            "LLMRateLimit": LLMRateLimit,
            "LLMResponseCorrupt": LLMResponseCorrupt,
            "LLMUnavailable": LLMUnavailable,
            "LLMHallucination": LLMHallucination,
            "LLMStreamInterrupt": LLMStreamInterrupt,
            "LLMTokenStarvation": LLMTokenStarvation,
            "LLMUnauthorized": LLMUnauthorized,
            "LLMForbidden": LLMForbidden,
            "LLMAuthExpiry": LLMAuthExpiry,
            "LLMContextLengthExceeded": LLMContextLengthExceeded,
            "SemanticCorrupt": SemanticCorrupt,
            "ToolFault": ToolFault,
            "MCPFault": MCPFault,
            # Storage
            "StorageCorrupt": StorageCorrupt,
            "StorageCorruptImmediate": StorageCorruptImmediate,
            "SQLiteCorrupt": SQLiteCorrupt,
            # Process
            "ProcessKill": ProcessKill,
            "ServiceFault": ServiceFault,
            "ContainerKill": ContainerKill,
            # State
            "RedisStateCorrupt": RedisStateCorrupt,
            "JsonStateCorrupt": JsonStateCorrupt,
            "PostgresStateCorrupt": PostgresStateCorrupt,
            # Resources
            "DiskFull": DiskFull,
            "CPUStress": CPUStress,
            "MemoryStress": MemoryStress,
            "IOStress": IOStress,
            "InodeFull": InodeFull,
            "FDExhaust": FDExhaust,
            "ProcessExhaust": ProcessExhaust,
            # GPU
            "GPUThrottle": GPUThrottle,
            "GPUMemoryPressure": GPUMemoryPressure,
            "GPUClockLock": GPUClockLock,
            # Skill / tool
            "SkillUnavailable": SkillUnavailable,
            "SkillMisroute": SkillMisroute,
            "SkillInstructionCorrupt": SkillInstructionCorrupt,
            "SkillDependencyMissing": SkillDependencyMissing,
            "SkillTimeout": SkillTimeout,
            "SkillBadOutput": SkillBadOutput,
            "SkillVersionSkew": SkillVersionSkew,
            "SkillPermissionDenied": SkillPermissionDenied,
            "SkillMemoryStale": SkillMemoryStale,
            "ConflictingSkills": ConflictingSkills,
            "SkillFileUnavailable": SkillFileUnavailable,
            "SkillFileInstructionCorrupt": SkillFileInstructionCorrupt,
            "SkillFileVersionSkew": SkillFileVersionSkew,
            "SkillFileBadOutput": SkillFileBadOutput,
            "SkillFileMemoryStale": SkillFileMemoryStale,
            "SkillFileConflict": SkillFileConflict,
            "SkillFilePermissionDenied": SkillFilePermissionDenied,
            "SkillJSONCorrupt": SkillJSONCorrupt,
            # Gateway
            "GatewayRouteMisconfig": GatewayRouteMisconfig,
            "GatewayFallbackBroken": GatewayFallbackBroken,
            "GatewayPolicyBlock": GatewayPolicyBlock,
            "GatewayPolicyBypass": GatewayPolicyBypass,
            "GatewayCacheStale": GatewayCacheStale,
            "GatewayCachePoison": GatewayCachePoison,
            "GatewayTenantLeak": GatewayTenantLeak,
            "GatewayHeaderStrip": GatewayHeaderStrip,
            "GatewayToolSchemaDrop": GatewayToolSchemaDrop,
            "GatewayResponseRewrite": GatewayResponseRewrite,
            "GatewayBudgetDesync": GatewayBudgetDesync,
            "GatewayRetryStorm": GatewayRetryStorm,
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
    def registered_kinds(cls) -> list[str]:
        """Return sorted list of all YAML-supported fault kind names."""
        cls._register_faults()
        return sorted(cls._FAULT_REGISTRY)

    @classmethod
    def _rename_keys(cls, kind: str, spec: dict[str, Any]) -> dict[str, Any]:
        if kind == "SilentNetworkCorrupt" and "rate" in spec:
            spec["rate"] = int(spec["rate"])
        return spec

    # ── Schema validation ─────────────────────────────────────────────────

    @classmethod
    def validate_suite_dict(cls, data: dict, path: str = "<yaml>") -> list[str]:
        """Validate a loaded YAML dict; return list of error strings.

        Returns an empty list if valid.
        """
        cls._register_faults()
        errors: list[str] = []

        # Top-level unknown keys
        unknown = set(data) - _SUITE_KNOWN_KEYS
        if unknown:
            errors.append(f"Unknown top-level keys: {sorted(unknown)}")

        # apiVersion / kind check (warn, not error — backwards compat)
        api = data.get("apiVersion", "")
        kind = data.get("kind", "")
        if api and api != "cj.io/v1":
            errors.append(f"apiVersion must be 'cj.io/v1', got {api!r}")
        if kind and kind not in {"ExperimentSuite", "Experiment", "DistributedScenario", "InjectionGroup"}:
            errors.append(
                f"kind must be 'ExperimentSuite', 'Experiment', 'DistributedScenario', or 'InjectionGroup', got {kind!r}"
            )

        # conflict policy
        conflict = data.get("conflict", "raise")
        if conflict not in {"raise", "warn", "force"}:
            errors.append(f"conflict must be raise|warn|force, got {conflict!r}")

        experiments = data.get("experiments", [])
        if not experiments:
            errors.append("experiments list is empty or missing")
        if not isinstance(experiments, list):
            errors.append("experiments must be a list")
            return errors

        for idx, exp in enumerate(experiments):
            prefix = f"experiments[{idx}]"
            if not isinstance(exp, dict):
                errors.append(f"{prefix}: must be a mapping")
                continue
            if not exp.get("name"):
                errors.append(f"{prefix}: missing required field 'name'")

            unknown_exp = set(exp) - _EXPERIMENT_KNOWN_KEYS
            if unknown_exp:
                errors.append(f"{prefix}: unknown keys {sorted(unknown_exp)}")

            for fidx, fault_spec in enumerate(exp.get("faults", [])):
                fprefix = f"{prefix}.faults[{fidx}]"
                if not isinstance(fault_spec, dict):
                    errors.append(f"{fprefix}: must be a mapping")
                    continue
                fkind = fault_spec.get("kind")
                if not fkind:
                    errors.append(f"{fprefix}: missing required field 'kind'")
                    continue
                if fkind not in cls._FAULT_REGISTRY:
                    errors.append(
                        f"{fprefix}: unsupported fault kind {fkind!r}. "
                        f"Supported: {sorted(cls._FAULT_REGISTRY)[:5]}... "
                        f"(run ConfigLoader.registered_kinds() for full list)"
                    )

                unknown_fault = set(fault_spec) - _FAULT_KNOWN_KEYS
                if unknown_fault:
                    errors.append(f"{fprefix}: unknown fault parameters {sorted(unknown_fault)}")

        return errors

    @classmethod
    def validate_file(cls, path: str) -> list[str]:
        """Load and validate a YAML file (suite or distributed scenario)."""
        try:
            import yaml
        except ImportError:
            return ["PyYAML is not installed: pip install pyyaml"]

        if not os.path.exists(path):
            return [f"File not found: {path}"]

        with open(path) as fh:
            try:
                data = yaml.safe_load(fh) or {}
            except Exception as exc:
                return [f"YAML parse error: {exc}"]

        if data.get("kind") == "DistributedScenario":
            return cls.validate_distributed_dict(data, path=path)
        if data.get("kind") == "InjectionGroup":
            return cls.validate_injection_group_dict(data, path=path)
        return cls.validate_suite_dict(data, path=path)

    _DISTRIBUTED_KNOWN_KEYS = frozenset({
        "apiVersion", "kind", "metadata",
        "synchronization", "atomicity", "injections",
        "observation", "safety",
    })
    _SYNC_KNOWN_KEYS = frozenset({
        "mode", "start_after", "maximum_skew_ms", "require_all_ready",
        "prepare_timeout_s", "strict_skew",
    })
    _ATOMICITY_KNOWN_KEYS = frozenset({
        "prepare", "activation_failure", "recovery_failure",
    })
    _INJECTION_KNOWN_KEYS = frozenset({"id", "target", "fault"})

    _INJECTION_GROUP_KNOWN_KEYS = frozenset({
        "apiVersion", "kind", "metadata",
        "name", "synchronization", "atomic", "maximum_skew_ms", "start_after",
        "require_all_ready", "on_prepare_failure", "on_activation_failure",
        "on_skew_violation", "safety_maximum_duration", "watchdog",
        "injections",
    })
    _INJECTION_GROUP_INJECTION_KNOWN_KEYS = frozenset({"id", "target", "fault"})

    @classmethod
    def validate_distributed_dict(cls, data: dict, path: str = "<yaml>") -> list[str]:
        """Validate a DistributedScenario YAML dict; return list of error strings."""
        cls._register_faults()
        errors: list[str] = []

        unknown = set(data) - cls._DISTRIBUTED_KNOWN_KEYS
        if unknown:
            errors.append(f"Unknown top-level keys: {sorted(unknown)}")

        api = data.get("apiVersion", "")
        if api and api != "cj.io/v1":
            errors.append(f"apiVersion must be 'cj.io/v1', got {api!r}")

        metadata = data.get("metadata", {})
        if not metadata.get("name"):
            errors.append("metadata.name is required")

        sync = data.get("synchronization", {})
        if isinstance(sync, dict):
            unknown_sync = set(sync) - cls._SYNC_KNOWN_KEYS
            if unknown_sync:
                errors.append(f"synchronization: unknown keys {sorted(unknown_sync)}")
            mode = sync.get("mode", "scheduled")
            if mode not in {"best_effort", "barrier", "scheduled"}:
                errors.append(
                    f"synchronization.mode must be best_effort|barrier|scheduled, got {mode!r}"
                )

        atomicity = data.get("atomicity", {})
        if isinstance(atomicity, dict):
            unknown_at = set(atomicity) - cls._ATOMICITY_KNOWN_KEYS
            if unknown_at:
                errors.append(f"atomicity: unknown keys {sorted(unknown_at)}")

        injections = data.get("injections", [])
        if not injections:
            errors.append("injections list is empty or missing")
        for idx, inj in enumerate(injections or []):
            prefix = f"injections[{idx}]"
            if not isinstance(inj, dict):
                errors.append(f"{prefix}: must be a mapping")
                continue
            if not inj.get("id"):
                errors.append(f"{prefix}: missing required field 'id'")
            if not inj.get("target"):
                errors.append(f"{prefix}: missing required field 'target'")
            fault_spec = inj.get("fault")
            if not fault_spec:
                errors.append(f"{prefix}: missing required field 'fault'")
            elif isinstance(fault_spec, dict):
                fkind = fault_spec.get("kind")
                if not fkind:
                    errors.append(f"{prefix}.fault: missing required field 'kind'")
                elif fkind not in cls._FAULT_REGISTRY:
                    errors.append(
                        f"{prefix}.fault: unsupported fault kind {fkind!r}"
                    )
            unknown_inj = set(inj) - cls._INJECTION_KNOWN_KEYS
            if unknown_inj:
                errors.append(f"{prefix}: unknown keys {sorted(unknown_inj)}")

        return errors

    @classmethod
    def validate_injection_group_dict(cls, data: dict, path: str = "<yaml>") -> list[str]:
        """Validate a ``kind: InjectionGroup`` YAML dict; return list of error strings."""
        cls._register_faults()
        errors: list[str] = []

        unknown = set(data) - cls._INJECTION_GROUP_KNOWN_KEYS
        if unknown:
            errors.append(f"Unknown top-level keys: {sorted(unknown)}")

        api = data.get("apiVersion", "")
        if api and api != "cj.io/v1":
            errors.append(f"apiVersion must be 'cj.io/v1', got {api!r}")

        name = data.get("name") or (data.get("metadata") or {}).get("name")
        if not name:
            errors.append("'name' (or metadata.name) is required")

        sync = data.get("synchronization", "scheduled")
        if isinstance(sync, str) and sync not in {"best_effort", "barrier", "scheduled"}:
            errors.append(
                f"synchronization must be best_effort|barrier|scheduled, got {sync!r}"
            )

        on_prep = data.get("on_prepare_failure", "cancel")
        if on_prep not in {"cancel", "best_effort"}:
            errors.append(f"on_prepare_failure must be cancel|best_effort, got {on_prep!r}")

        on_act = data.get("on_activation_failure", "rollback_all")
        if on_act not in {"rollback_all", "continue"}:
            errors.append(f"on_activation_failure must be rollback_all|continue, got {on_act!r}")

        on_skew = data.get("on_skew_violation", "mark_invalid")
        if on_skew not in {"rollback_all", "mark_invalid", "continue"}:
            errors.append(
                f"on_skew_violation must be rollback_all|mark_invalid|continue, got {on_skew!r}"
            )

        injections = data.get("injections", [])
        if not injections:
            errors.append("injections list is empty or missing")
        for idx, inj in enumerate(injections or []):
            prefix = f"injections[{idx}]"
            if not isinstance(inj, dict):
                errors.append(f"{prefix}: must be a mapping")
                continue
            if not inj.get("id"):
                errors.append(f"{prefix}: missing required field 'id'")
            if not inj.get("target"):
                errors.append(f"{prefix}: missing required field 'target'")
            fault_spec = inj.get("fault")
            if not fault_spec:
                errors.append(f"{prefix}: missing required field 'fault'")
            elif isinstance(fault_spec, dict):
                fkind = fault_spec.get("kind")
                if not fkind:
                    errors.append(f"{prefix}.fault: missing required field 'kind'")
                elif fkind not in cls._FAULT_REGISTRY:
                    errors.append(f"{prefix}.fault: unsupported fault kind {fkind!r}")
            unknown_inj = set(inj) - cls._INJECTION_GROUP_INJECTION_KNOWN_KEYS
            if unknown_inj:
                errors.append(f"{prefix}: unknown keys {sorted(unknown_inj)}")

        return errors

    @classmethod
    def build_injection_group(cls, path: str) -> "InjectionGroup":
        """Load a ``kind: InjectionGroup`` YAML file and return an
        :class:`~chaos_jungle.inject.group.InjectionGroup`.

        Parameters
        ----------
        path :
            Path to the YAML file.

        Raises
        ------
        FileNotFoundError
            If the file does not exist.
        ValueError
            If the file fails validation.
        """
        try:
            import yaml
        except ImportError as exc:
            raise ImportError("PyYAML is not installed: pip install pyyaml") from exc

        from chaos_jungle.inject.group import InjectionGroup
        from chaos_jungle.distributed.scenario import Injection

        if not os.path.exists(path):
            raise FileNotFoundError(f"InjectionGroup config not found: {path}")

        with open(path) as fh:
            data = yaml.safe_load(fh) or {}

        errors = cls.validate_injection_group_dict(data, path=path)
        if errors:
            raise ValueError(
                f"InjectionGroup config {path!r} has {len(errors)} error(s):\n"
                + "\n".join(f"  • {e}" for e in errors)
            )

        name = data.get("name") or (data.get("metadata") or {}).get("name")
        members: list = []
        for inj in data.get("injections", []):
            inj_id = inj["id"]
            target = cls.build_target(inj["target"])
            fault = cls.build_fault(dict(inj["fault"]))
            members.append(Injection(id=inj_id, target=target, fault=fault))

        return InjectionGroup(
            name=name,
            injections=members,
            synchronization=data.get("synchronization", "scheduled"),
            atomic=data.get("atomic", True),
            maximum_skew_ms=float(data.get("maximum_skew_ms", 100.0)),
            start_after=float(data.get("start_after", 5.0)),
            require_all_ready=data.get("require_all_ready", True),
            on_prepare_failure=data.get("on_prepare_failure", "cancel"),
            on_activation_failure=data.get("on_activation_failure", "rollback_all"),
            on_skew_violation=data.get("on_skew_violation", "mark_invalid"),
            safety_maximum_duration=float(data.get("safety_maximum_duration", 90.0)),
            watchdog=data.get("watchdog", True),
        )

    @classmethod
    def build_distributed_scenario(cls, path: str) -> "DistributedScenario":
        """Load a ``kind: DistributedScenario`` YAML file and return a
        :class:`~chaos_jungle.distributed.DistributedScenario`.

        Parameters
        ----------
        path :
            Path to the YAML file.

        Raises
        ------
        FileNotFoundError
            If the file does not exist.
        ValueError
            If the YAML has schema errors or unknown fault kinds.
        """
        from chaos_jungle.distributed.scenario import (
            AtomicityConfig,
            DistributedSafetyConfig,
            DistributedScenario,
            Injection,
            SyncConfig,
        )

        try:
            import yaml
        except ImportError as exc:
            raise ImportError("PyYAML required: pip install pyyaml") from exc

        if not os.path.exists(path):
            raise FileNotFoundError(f"DistributedScenario config not found: {path}")

        with open(path) as fh:
            data = yaml.safe_load(fh) or {}

        errors = cls.validate_distributed_dict(data, path=path)
        if errors:
            raise ValueError(
                f"DistributedScenario config {path!r} has {len(errors)} error(s):\n"
                + "\n".join(f"  • {e}" for e in errors)
            )

        metadata = data.get("metadata", {})
        name = metadata.get("name", "unnamed")

        sync_d = data.get("synchronization", {})
        sync = SyncConfig(
            mode=sync_d.get("mode", "scheduled"),
            start_after=_parse_duration(sync_d.get("start_after", 5.0)),
            maximum_skew_ms=float(sync_d.get("maximum_skew_ms", 100.0)),
            require_all_ready=bool(sync_d.get("require_all_ready", True)),
            prepare_timeout_s=float(sync_d.get("prepare_timeout_s", 30.0)),
            strict_skew=bool(sync_d.get("strict_skew", True)),
        )

        atom_d = data.get("atomicity", {})
        atomicity = AtomicityConfig(
            prepare=atom_d.get("prepare", "all_or_nothing"),
            activation_failure=atom_d.get("activation_failure", "rollback_all"),
            recovery_failure=atom_d.get("recovery_failure", "mark_invalid"),
        )

        safety_d = data.get("safety", {})
        safety = DistributedSafetyConfig(
            emergency_stop=bool(safety_d.get("emergency_stop", True)),
            maximum_duration=_parse_duration(safety_d.get("maximum_duration", 90.0)),
            rollback_all=bool(safety_d.get("rollback_all", True)),
        )

        obs_d = data.get("observation", {})
        observation_duration = _parse_duration(obs_d.get("duration", 60.0))

        members: list[Injection] = []
        for inj_d in data.get("injections", []):
            inj_id = inj_d["id"]
            target = cls.build_target(inj_d["target"])
            fault = cls.build_fault(dict(inj_d["fault"]))
            members.append(Injection(id=inj_id, target=target, fault=fault))

        return DistributedScenario(
            name=name,
            members=members,
            synchronization=sync,
            atomicity=atomicity,
            observation_duration=observation_duration,
            safety=safety,
        )

    # ── ExperimentPlan compilation ────────────────────────────────────────

    @classmethod
    def build_plans(cls, path: str) -> "list[ExperimentPlan]":
        """Load a YAML suite file and return one ExperimentPlan per experiment.

        Used by ``chaos-jungle suite plan`` and conformance tests.
        """
        from chaos_jungle.plan import ExperimentPlan

        try:
            import yaml
        except ImportError as exc:
            raise ImportError("PyYAML required: pip install pyyaml") from exc

        if not os.path.exists(path):
            raise FileNotFoundError(f"Suite config not found: {path}")

        with open(path) as fh:
            data = yaml.safe_load(fh) or {}

        suite_defaults = {k: v for k, v in data.items() if k != "experiments"}
        plans = []
        for entry in data.get("experiments", []):
            plan = ExperimentPlan.from_yaml_entry(entry, suite_defaults=suite_defaults, source="yaml")
            plans.append(plan)
        return plans

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
    def load_suite(cls, path: str, validate: bool = False):
        """Build an :class:`~chaos_jungle.core.suite.ExperimentSuite` from a YAML file.

        Parameters
        ----------
        path :
            Path to the YAML file.
        validate : bool
            If True, run schema validation before loading and raise
            ``ValueError`` listing all errors if any are found.

        Raises
        ------
        FileNotFoundError
            If the YAML file does not exist.
        ImportError
            If PyYAML is not installed.
        ValueError
            If the YAML is missing required fields or (when validate=True)
            has schema errors.
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

        if validate:
            errors = cls.validate_suite_dict(data, path=path)
            if errors:
                raise ValueError(
                    f"Suite config {path!r} has {len(errors)} validation error(s):\n"
                    + "\n".join(f"  • {e}" for e in errors)
                )

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

def build_distributed_scenario(path: str):
    return ConfigLoader.build_distributed_scenario(path)

def build_injection_group(path: str):
    return ConfigLoader.build_injection_group(path)
