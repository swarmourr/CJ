"""Fault implementations for chaos-jungle."""

from chaos_jungle.faults.base import Fault, PreflightError
from chaos_jungle.faults.network import (
    NetworkCorrupt,
    NetworkDelay,
    NetworkDuplicate,
    NetworkLoss,
)
from chaos_jungle.faults.storage import StorageCorrupt
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
    SkillFileUnavailable,
    SkillFileInstructionCorrupt,
    SkillFileVersionSkew,
    SkillFileBadOutput,
    SkillFileMemoryStale,
    SkillFileConflict,
    SkillFilePermissionDenied,
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
    # Storage
    "StorageCorrupt",
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
    "SkillFileUnavailable",
    "SkillFileInstructionCorrupt",
    "SkillFileVersionSkew",
    "SkillFileBadOutput",
    "SkillFileMemoryStale",
    "SkillFileConflict",
    "SkillFilePermissionDenied",
]
