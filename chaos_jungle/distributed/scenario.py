"""DistributedScenario and supporting dataclasses for multi-host coordinated injection."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chaos_jungle.faults.base import Fault
    from chaos_jungle.targets.base import Target


@dataclass
class Injection:
    """One (target, fault) member of a DistributedScenario."""

    id: str
    target: "Target"
    fault: "Fault"


@dataclass
class SyncConfig:
    """Synchronization settings for a DistributedScenario.

    Parameters
    ----------
    mode :
        ``best_effort`` — activate each member ASAP, no timing guarantee.
        ``barrier``     — wait for all READY, then activate simultaneously.
        ``scheduled``   — wait for all READY, then activate at a shared
                          future timestamp (``start_after`` seconds from now).
    start_after :
        Seconds between all-ready barrier and the commit timestamp.
        Only used in ``scheduled`` mode. Default ``5.0``.
    maximum_skew_ms :
        Maximum tolerated difference (ms) between the earliest and latest
        actual activation times. Exceeding this makes the verdict ``invalid``
        (or ``inconclusive`` if ``strict_skew=False``). Default ``100``.
    require_all_ready :
        Cancel the entire group if any member fails the prepare phase.
        Default ``True``.
    prepare_timeout_s :
        Per-member prepare deadline in seconds. Default ``30``.
    strict_skew :
        If ``True`` (default) a skew violation makes the verdict ``invalid``.
        If ``False`` a violation makes it ``inconclusive``.
    """

    mode: str = "scheduled"
    start_after: float = 5.0
    maximum_skew_ms: float = 100.0
    require_all_ready: bool = True
    prepare_timeout_s: float = 30.0
    strict_skew: bool = True


@dataclass
class AtomicityConfig:
    """Failure handling policies.

    Parameters
    ----------
    prepare :
        ``all_or_nothing`` (default) — abort the group if any member fails
        preparation. ``best_effort`` — activate successfully-prepared members.
    activation_failure :
        ``rollback_all`` (default) — revert already-activated members if any
        activation fails. ``continue`` — leave activated members running.
    recovery_failure :
        ``mark_invalid`` (default) — mark group recovery invalid if any member
        cannot confirm revert. ``warn`` — log a warning and continue.
    """

    prepare: str = "all_or_nothing"
    activation_failure: str = "rollback_all"
    recovery_failure: str = "mark_invalid"


@dataclass
class DistributedSafetyConfig:
    """Safety constraints for a DistributedScenario."""

    emergency_stop: bool = True
    maximum_duration: float = 90.0
    rollback_all: bool = True


@dataclass
class DistributedScenario:
    """Coordinated fault injection across multiple targets.

    Uses a two-phase prepare/commit protocol so that all faults activate
    at approximately the same wall-clock time, regardless of SSH/HTTP
    connection latency differences.

    Lifecycle
    ---------
    1. **Prepare** (concurrent): every member validates params, checks deps,
       pre-connects. Reports READY or FAILED.
    2. **Barrier**: coordinator waits for all READY (or aborts on failure).
    3. **Commit** (concurrent, timed): all members activate at timestamp T.
    4. **Observe**: coordinator waits ``observation_duration`` seconds.
    5. **Revert** (concurrent): all members stop/undo their fault.
    6. **Evidence**: activation timestamps, skew, and verdict are recorded.

    Parameters
    ----------
    name :
        Human-readable scenario name (used as ``group_id`` in evidence).
    members :
        One :class:`Injection` per host/target.
    synchronization :
        How activation is coordinated across members.
    atomicity :
        Failure handling policies.
    observation_duration :
        Seconds to observe after activation before reverting. Default ``60``.
    safety :
        Emergency stop and max-duration constraints.

    Examples
    --------
    >>> from chaos_jungle.distributed import DistributedScenario, Injection
    >>> from chaos_jungle.faults import LLMLatency, NetworkLoss
    >>> from chaos_jungle.targets import HTTPTarget, SSHTarget
    >>>
    >>> scenario = DistributedScenario(
    ...     name="agent-failure-test",
    ...     members=[
    ...         Injection("llm-delay",   HTTPTarget("http://agent:8080"), LLMLatency(delay_s=1.0)),
    ...         Injection("net-loss",    SSHTarget("tool-node", user="ubuntu"), NetworkLoss("5%")),
    ...     ],
    ... )
    >>> from chaos_jungle.distributed import DistributedCoordinator
    >>> evidence = DistributedCoordinator(scenario).run()
    >>> print(evidence.verdict, evidence.activation_skew_ms, "ms skew")
    """

    name: str
    members: list[Injection] = field(default_factory=list)
    synchronization: SyncConfig = field(default_factory=SyncConfig)
    atomicity: AtomicityConfig = field(default_factory=AtomicityConfig)
    observation_duration: float = 60.0
    safety: DistributedSafetyConfig = field(default_factory=DistributedSafetyConfig)
