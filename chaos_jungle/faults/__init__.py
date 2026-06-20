"""Fault implementations for chaos-jungle."""

from chaos_jungle.faults.base import Fault, PreflightError
from chaos_jungle.faults.network import (
    NetworkCorrupt,
    NetworkDelay,
    NetworkDuplicate,
    NetworkLoss,
    NetworkBandwidthLimit,
    NetworkReorder,
    NetworkReset,
    NetworkPartition,
)
from chaos_jungle.faults.storage import (
    StorageCorrupt,
    StorageCorruptImmediate,
    SQLiteCorrupt,
)
from chaos_jungle.faults.bpf import SilentNetworkCorrupt
from chaos_jungle.faults.llm import (
    LLMLatency,
    LLMRateLimit,
    LLMTimeout,
    LLMResponseCorrupt,
    LLMUnavailable,
    ToolFault,
    LLMHallucination,
    LLMStreamInterrupt,
    LLMTokenStarvation,
    MCPFault,
    SemanticCorrupt,
    LLMUnauthorized,
    LLMForbidden,
    LLMAuthExpiry,
    LLMContextLengthExceeded,
)
from chaos_jungle.faults.state import (
    RedisStateCorrupt,
    JsonStateCorrupt,
    PostgresStateCorrupt,
)
from chaos_jungle.faults.process import (
    ProcessKill,
    ServiceFault,
    ContainerKill,
)
from chaos_jungle.faults.resources import (
    DiskFull,
    CPUStress,
    MemoryStress,
    IOStress,
    InodeFull,
    FDExhaust,
    ProcessExhaust,
)
from chaos_jungle.faults.gpu import (
    GPUThrottle,
    GPUMemoryPressure,
    GPUClockLock,
)
from chaos_jungle.faults.skill import (
    SkillUnavailable,
    SkillMisroute,
    SkillInstructionCorrupt,
    SkillDependencyMissing,
    SkillTimeout,
    SkillBadOutput,
    SkillVersionSkew,
    SkillPermissionDenied,
    SkillMemoryStale,
    ConflictingSkills,
)
from chaos_jungle.faults.skill_file import (
    LLMSkillFaultGenerator,
    SkillFileUnavailable,
    SkillFileInstructionCorrupt,
    SkillFileVersionSkew,
    SkillFileBadOutput,
    SkillFileMemoryStale,
    SkillFileConflict,
    SkillFilePermissionDenied,
    SkillJSONCorrupt,
)
from chaos_jungle.faults.gateway import (
    GatewayRouteMisconfig,
    GatewayFallbackBroken,
    GatewayPolicyBlock,
    GatewayPolicyBypass,
    GatewayCacheStale,
    GatewayCachePoison,
    GatewayTenantLeak,
    GatewayHeaderStrip,
    GatewayToolSchemaDrop,
    GatewayResponseRewrite,
    GatewayBudgetDesync,
    GatewayRetryStorm,
)

__all__ = [
    "Fault",
    "PreflightError",
    # Network
    "NetworkDelay",
    "NetworkLoss",
    "NetworkCorrupt",
    "NetworkDuplicate",
    "SilentNetworkCorrupt",
    "NetworkBandwidthLimit",
    "NetworkReorder",
    "NetworkReset",
    "NetworkPartition",
    # Storage
    "StorageCorrupt",
    "StorageCorruptImmediate",
    "SQLiteCorrupt",
    # LLM
    "LLMLatency",
    "LLMRateLimit",
    "LLMTimeout",
    "LLMResponseCorrupt",
    "LLMUnavailable",
    "ToolFault",
    "LLMHallucination",
    "LLMStreamInterrupt",
    "LLMTokenStarvation",
    "MCPFault",
    "SemanticCorrupt",
    "LLMUnauthorized",
    "LLMForbidden",
    "LLMAuthExpiry",
    "LLMContextLengthExceeded",
    # State
    "RedisStateCorrupt",
    "JsonStateCorrupt",
    "PostgresStateCorrupt",
    # Process / service / container
    "ProcessKill",
    "ServiceFault",
    "ContainerKill",
    # Resource exhaustion
    "DiskFull",
    "CPUStress",
    "MemoryStress",
    "IOStress",
    "InodeFull",
    "FDExhaust",
    "ProcessExhaust",
    # GPU
    "GPUThrottle",
    "GPUMemoryPressure",
    "GPUClockLock",
    # Skill / tool chaos (proxy-based)
    "SkillUnavailable",
    "SkillMisroute",
    "SkillInstructionCorrupt",
    "SkillDependencyMissing",
    "SkillTimeout",
    "SkillBadOutput",
    "SkillVersionSkew",
    "SkillPermissionDenied",
    "SkillMemoryStale",
    "ConflictingSkills",
    # Skill / tool chaos (local file-based)
    "LLMSkillFaultGenerator",
    "SkillFileUnavailable",
    "SkillFileInstructionCorrupt",
    "SkillFileVersionSkew",
    "SkillFileBadOutput",
    "SkillFileMemoryStale",
    "SkillFileConflict",
    "SkillFilePermissionDenied",
    "SkillJSONCorrupt",
    # AI Gateway faults
    "GatewayRouteMisconfig",
    "GatewayFallbackBroken",
    "GatewayPolicyBlock",
    "GatewayPolicyBypass",
    "GatewayCacheStale",
    "GatewayCachePoison",
    "GatewayTenantLeak",
    "GatewayHeaderStrip",
    "GatewayToolSchemaDrop",
    "GatewayResponseRewrite",
    "GatewayBudgetDesync",
    "GatewayRetryStorm",
]
