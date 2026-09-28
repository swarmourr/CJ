"""InjectionGroup — the canonical CJ abstraction for coordinated fault injection.

Hierarchy
---------
Injection          one fault on one target
InjectionGroup     coordinated faults on one or more targets (this module)
Scenario           workload + hypothesis + one or more injection groups
Campaign           repeated scenarios across tasks / models / configs

Quick start
-----------
Local group (multiple faults, single target)::

    from chaos_jungle.inject.group import InjectionGroup, InjectionGroupRunner
    from chaos_jungle.distributed import Injection
    from chaos_jungle.faults import LLMLatency, NetworkLoss
    from chaos_jungle.targets import LocalTarget

    group = InjectionGroup(
        name="local-compound",
        synchronization="barrier",
        injections=[
            Injection("llm-delay", LocalTarget(), LLMLatency(delay_s=0.5)),
            Injection("net-loss",  LocalTarget(), NetworkLoss("5%")),
        ],
    )
    runner = InjectionGroupRunner(group)
    runner.start()
    # ... workload ...
    evidence = runner.stop()
    print(evidence.group_valid, evidence.activation_skew_ms, "ms")

Distributed group (multiple targets, scheduled activation)::

    group = InjectionGroup(
        name="distributed-agent-degradation",
        synchronization="scheduled",
        atomic=True,
        maximum_skew_ms=100,
        start_after=5.0,
        injections=[
            Injection("llm-delay",   HTTPTarget("http://agent-node:8080"), LLMLatency(delay_s=1.0)),
            Injection("tool-loss",   SSHTarget("tool-node", user="ubuntu"), NetworkLoss("5%")),
            Injection("db-pressure", SSHTarget("db-node",   user="ubuntu"), IOStress(workers=2)),
        ],
    )
"""
from __future__ import annotations

import threading
import time
import uuid
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chaos_jungle.distributed.evidence import GroupActivationEvidence

log = logging.getLogger(__name__)


@dataclass
class InjectionGroup:
    """Coordinated fault injection across one or more targets.

    Faults are activated using a two-phase prepare/commit protocol so that
    all injections become active at approximately the same wall-clock time.

    Parameters
    ----------
    name :
        Human-readable name used as ``group_id`` in evidence records.
    injections :
        List of :class:`~chaos_jungle.distributed.scenario.Injection` objects,
        one per (target, fault) pair.
    synchronization :
        ``"best_effort"`` — activate each member ASAP (no timing guarantee).
        ``"barrier"``     — activate after every member signals READY.
        ``"scheduled"``   — activate at a shared timestamp ``start_after``
                           seconds after all READY (default).
    atomic :
        When ``True`` (default) any prepare or activation failure triggers
        rollback of the entire group.
    maximum_skew_ms :
        Maximum tolerated difference in ms between the earliest and latest
        actual activation timestamps. Exceeding this applies
        ``on_skew_violation``. Default ``100``.
    start_after :
        Seconds to wait between all-ready barrier and commit. Only used in
        ``"scheduled"`` mode. Default ``5.0``.
    require_all_ready :
        Cancel the group if any member fails preparation. When ``atomic``
        is ``True`` this is always effectively ``True``. Default ``True``.
    on_prepare_failure :
        ``"cancel"`` (default) — abort the group if any member fails.
        ``"best_effort"`` — activate successfully-prepared members.
    on_activation_failure :
        ``"rollback_all"`` (default) — revert all active members on failure.
        ``"continue"`` — leave running members active.
    on_skew_violation :
        ``"rollback_all"`` — revert all members and mark evidence ``invalid``.
        ``"mark_invalid"`` (default) — mark evidence ``invalid`` but keep
        faults running so the workload still experiences them.
        ``"continue"`` — record skew but do not change verdict.
    safety_maximum_duration :
        Hard upper bound in seconds for how long the group can be active.
        A background watchdog will call ``stop()`` after this time if not
        already called. Default ``90.0``.
    watchdog :
        Enable the safety watchdog. Default ``True``.
    group_id :
        Stable identifier for this group. Auto-generated UUID if empty.

    Notes
    -----
    The validity formula for a strictly-synchronized group is:

        Valid(G) = Ready(G) ∧ Skew(G) ≤ ε ∧ ∧ Manifested(F_i)

    where ``Manifested(F_i)`` is ``True`` or ``None`` (unknown, counted as
    valid). If one required fault does not manifest, the group is not
    valid and the run should not be analyzed as a compound-fault experiment.
    """

    name: str
    injections: list = field(default_factory=list)
    synchronization: str = "scheduled"
    atomic: bool = True
    maximum_skew_ms: float = 100.0
    start_after: float = 5.0
    require_all_ready: bool = True
    on_prepare_failure: str = "cancel"
    on_activation_failure: str = "rollback_all"
    on_skew_violation: str = "mark_invalid"
    safety_maximum_duration: float = 90.0
    watchdog: bool = True
    group_id: str = ""

    def __post_init__(self) -> None:
        if not self.group_id:
            self.group_id = str(uuid.uuid4())[:8]
        if self.synchronization not in {"best_effort", "barrier", "scheduled"}:
            raise ValueError(
                f"synchronization must be best_effort|barrier|scheduled, "
                f"got {self.synchronization!r}"
            )
        if self.on_prepare_failure not in {"cancel", "best_effort"}:
            raise ValueError(f"on_prepare_failure must be cancel|best_effort")
        if self.on_activation_failure not in {"rollback_all", "continue"}:
            raise ValueError(f"on_activation_failure must be rollback_all|continue")
        if self.on_skew_violation not in {"rollback_all", "mark_invalid", "continue"}:
            raise ValueError(f"on_skew_violation must be rollback_all|mark_invalid|continue")


class InjectionGroupRunner:
    """Execute an :class:`InjectionGroup` with a split start()/stop() interface.

    Unlike :class:`~chaos_jungle.distributed.coordinator.DistributedCoordinator`
    (which blocks for the observation window), this runner separates activation
    from observation so the caller controls the workload timing:

        runner = InjectionGroupRunner(group)
        runner.start()              # activate all faults (blocks until active)
        workload()
        evidence = runner.stop()   # revert + return evidence

    Parameters
    ----------
    group :
        The :class:`InjectionGroup` to execute.
    """

    def __init__(self, group: InjectionGroup) -> None:
        self.group = group
        self._states: list[_MemberState] = [
            _MemberState(inj) for inj in group.injections
        ]
        self._stop_event = threading.Event()
        self._watchdog_thread: threading.Thread | None = None
        self._evidence: GroupActivationEvidence | None = None
        self._started = False

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Prepare and activate all members. Blocks until all faults are active.

        Runs the two-phase prepare/commit protocol:

        1. Pre-connect + optional ``fault.prepare(target)`` on each member.
        2. Wait for all-ready barrier (or abort if ``on_prepare_failure == "cancel"``).
        3. Compute commit timestamp based on synchronization mode.
        4. All members activate at commit time.

        Raises
        ------
        RuntimeError
            If the group was cancelled during preparation and ``atomic=True``.
        """
        from chaos_jungle.distributed.evidence import GroupActivationEvidence, MemberEvidence

        self._started = True
        group = self.group
        self._evidence = GroupActivationEvidence(
            group_id=group.group_id,
            maximum_allowed_skew_ms=group.maximum_skew_ms,
            strict_skew=(group.on_skew_violation in {"rollback_all", "mark_invalid"}),
        )

        # ── Phase 1: Prepare (concurrent) ────────────────────────────────────
        prep_threads = [
            threading.Thread(
                target=self._prepare_member, args=(s,), daemon=True,
                name=f"cj-grp-prep-{s.injection.id}",
            )
            for s in self._states
        ]
        for t in prep_threads:
            t.start()

        timeout = group.start_after + 30 if group.synchronization == "scheduled" else 30
        all_ready = self._wait_all_ready(timeout=30)
        failed = [s for s in self._states if s.error]

        if failed:
            log.warning(
                "Group %s: %d member(s) failed prepare: %s",
                group.name, len(failed), [s.injection.id for s in failed],
            )

        cancel = (not all_ready) and (
            group.atomic or
            group.on_prepare_failure == "cancel" or
            group.require_all_ready
        )
        if cancel:
            log.warning("Group %s: cancelled — not all members ready", group.name)
            self._stop_event.set()
            for t in prep_threads:
                t.join(timeout=5)
            self._evidence.verdict = "cancelled"
            self._evidence.members = [s.evidence for s in self._states]
            if group.atomic:
                raise RuntimeError(
                    f"InjectionGroup {group.name!r}: cancelled during prepare "
                    f"— {len(failed)} member(s) failed"
                )
            return

        # ── Phase 2: Commit (concurrent, timed) ──────────────────────────────
        now = time.time()
        if group.synchronization == "scheduled":
            commit_at = now + group.start_after
        else:
            commit_at = now  # barrier / best_effort

        self._evidence.requested_start = datetime.fromtimestamp(commit_at, tz=timezone.utc)
        log.info(
            "Group %s: committing %d member(s) at %s  (mode=%s)",
            group.name, len(self._states),
            self._evidence.requested_start.isoformat(),
            group.synchronization,
        )

        commit_threads = [
            threading.Thread(
                target=self._commit_member, args=(s, commit_at), daemon=True,
                name=f"cj-grp-commit-{s.injection.id}",
            )
            for s in self._states
        ]
        for t in commit_threads:
            t.start()
        for t in commit_threads:
            t.join(timeout=group.start_after + 30)
        for t in prep_threads:
            t.join(timeout=2)

        # ── Skew check ───────────────────────────────────────────────────────
        self._evidence.members = [s.evidence for s in self._states]
        skew = self._evidence.compute_skew()
        if skew is not None and skew > group.maximum_skew_ms:
            log.warning(
                "Group %s: activation skew %.1f ms exceeds limit %.1f ms",
                group.name, skew, group.maximum_skew_ms,
            )
            if group.on_skew_violation == "rollback_all":
                self._revert_all()
                self._evidence.activation_skew_ms = skew
                self._evidence.synchronization_valid = False
                self._evidence.verdict = "invalid"
                if group.atomic:
                    raise RuntimeError(
                        f"InjectionGroup {group.name!r}: skew {skew:.1f} ms > "
                        f"limit {group.maximum_skew_ms:.1f} ms — rolled back"
                    )
                return

        # ── Watchdog ─────────────────────────────────────────────────────────
        if group.watchdog and group.safety_maximum_duration > 0:
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_fn, daemon=True, name=f"cj-grp-wdog-{group.group_id}"
            )
            self._watchdog_thread.start()

        log.info("Group %s: all faults ACTIVE", group.name)

    def stop(self) -> "GroupActivationEvidence":
        """Revert all active members and return the populated evidence.

        Safe to call multiple times (idempotent). Always triggers revert
        and disables the watchdog.

        Returns
        -------
        GroupActivationEvidence
            Full synchronization evidence with per-member timestamps,
            skew, and group validity verdict.
        """
        if self._evidence is None:
            from chaos_jungle.distributed.evidence import GroupActivationEvidence
            self._evidence = GroupActivationEvidence(group_id=self.group.group_id)
            self._evidence.verdict = "cancelled"
            return self._evidence

        self._stop_event.set()

        if self._evidence.verdict not in {"cancelled", "invalid"}:
            self._revert_all()
            self._evidence.members = [s.evidence for s in self._states]
            self._evidence.finalize()

        return self._evidence

    @property
    def evidence(self) -> "GroupActivationEvidence | None":
        """The evidence record. Populated after :meth:`start` and complete after :meth:`stop`."""
        return self._evidence

    def emergency_stop(self) -> None:
        """Trigger an immediate stop from any thread."""
        log.warning("Group %s: emergency stop", self.group.name)
        self._stop_event.set()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _prepare_member(self, state: "_MemberState") -> None:
        from chaos_jungle.distributed.clock import ClockProbe
        inj = state.injection
        try:
            if hasattr(inj.target, "connect"):
                try:
                    inj.target.connect()
                    state.connected = True
                except Exception as exc:
                    raise ConnectionError(
                        f"Cannot connect to target for {inj.id}: {exc}"
                    ) from exc

            if hasattr(inj.fault, "prepare"):
                inj.fault.prepare(inj.target)

            try:
                probe = ClockProbe.for_target(inj.target)
                state.evidence.clock_offset_ms = probe.estimated_offset_ms
            except Exception:
                pass

            state.evidence.prepared_at = datetime.now(timezone.utc)
            log.debug("Member %s READY", inj.id)
        except Exception as exc:
            state.error = str(exc)
            state.evidence.error = str(exc)
            log.warning("Member %s FAILED prepare: %s", inj.id, exc)
        finally:
            state.ready_event.set()

    def _commit_member(self, state: "_MemberState", commit_at: float) -> None:
        inj = state.injection
        if state.error:
            return

        # High-precision activation: coarse sleep + 1ms spin
        slack = commit_at - time.time()
        if slack > 0.001:
            time.sleep(slack - 0.001)
        while time.time() < commit_at:
            pass

        try:
            inj.fault.start(inj.target)
            state.evidence.active_at = datetime.now(timezone.utc)
            log.debug("Member %s ACTIVE at %s", inj.id, state.evidence.active_at.isoformat())
        except Exception as exc:
            state.error = str(exc)
            state.evidence.error = str(exc)
            log.error("Member %s activation FAILED: %s", inj.id, exc)
            if self.group.atomic or self.group.on_activation_failure == "rollback_all":
                self._stop_event.set()

    def _revert_member(self, state: "_MemberState") -> None:
        inj = state.injection
        if state.evidence.active_at is None:
            return
        try:
            inj.fault.stop(inj.target)
            state.evidence.reverted_at = datetime.now(timezone.utc)
            log.debug("Member %s REVERTED", inj.id)
        except Exception as exc:
            log.error("Member %s revert FAILED: %s", inj.id, exc)
            if self._evidence is not None:
                self._evidence.verdict = "recovery_invalid"
        finally:
            if state.connected and hasattr(inj.target, "disconnect"):
                try:
                    inj.target.disconnect()
                except Exception:
                    pass

    def _revert_all(self) -> None:
        threads = [
            threading.Thread(
                target=self._revert_member, args=(s,), daemon=True,
                name=f"cj-grp-revert-{s.injection.id}",
            )
            for s in self._states
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

    def _wait_all_ready(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        for state in self._states:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            state.ready_event.wait(timeout=max(0.0, remaining))
        return all(s.error is None for s in self._states)

    def _watchdog_fn(self) -> None:
        """Background thread: auto-stop after safety_maximum_duration."""
        triggered = self._stop_event.wait(timeout=self.group.safety_maximum_duration)
        if not triggered:
            log.warning(
                "Group %s: watchdog triggered after %.0fs — stopping",
                self.group.name, self.group.safety_maximum_duration,
            )
            self.stop()


class _MemberState:
    """Per-member mutable state shared between runner and worker threads."""

    def __init__(self, injection: object) -> None:
        from chaos_jungle.distributed.evidence import MemberEvidence
        self.injection = injection
        self.ready_event = threading.Event()
        host = _host_of(injection.target)
        self.evidence = MemberEvidence(host=host, injection_id=injection.id)
        self.error: str | None = None
        self.connected: bool = False


def _host_of(target: object) -> str:
    for attr in ("host", "url", "base_url"):
        v = getattr(target, attr, None)
        if v:
            return str(v)
    return repr(target)
