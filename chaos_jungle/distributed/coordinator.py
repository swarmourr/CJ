"""Distributed injection coordinator.

Implements the two-phase prepare/commit protocol for simultaneous multi-host
fault injection. Delegates the protocol implementation to
:class:`~chaos_jungle.inject.group.InjectionGroupRunner` so both the
single-scenario API (:class:`DistributedCoordinator`) and the multi-group API
(:class:`~chaos_jungle.inject.group.InjectionGroupRunner`) share one code path.

Lifecycle (blocking ``run()``)
──────────────────────────────
1. Convert ``DistributedScenario`` → ``InjectionGroup`` (parameter mapping).
2. ``InjectionGroupRunner.start()`` — concurrent prepare + timed commit.
3. Wait for ``observation_duration`` (or until ``stop()`` is called).
4. ``InjectionGroupRunner.stop()`` — concurrent revert + verify_recovered.
5. Return :class:`~chaos_jungle.distributed.evidence.GroupActivationEvidence`.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .evidence import GroupActivationEvidence
from .scenario import DistributedScenario

log = logging.getLogger(__name__)


def _scenario_to_group(scenario: DistributedScenario):
    """Convert a DistributedScenario to an InjectionGroup for InjectionGroupRunner."""
    from chaos_jungle.inject.group import InjectionGroup

    sync = scenario.synchronization
    atomicity = scenario.atomicity
    safety = scenario.safety

    on_skew = "mark_invalid" if sync.strict_skew else "continue"
    on_prepare = "cancel" if atomicity.prepare == "all_or_nothing" else "best_effort"
    on_activation = atomicity.activation_failure  # "rollback_all" | "continue"

    return InjectionGroup(
        name=scenario.name,
        injections=list(scenario.members),
        synchronization=sync.mode,
        atomic=False,  # use explicit flags; runner won't raise on cancellation
        maximum_skew_ms=sync.maximum_skew_ms,
        start_after=sync.start_after,
        require_all_ready=sync.require_all_ready,
        on_prepare_failure=on_prepare,
        on_activation_failure=on_activation,
        on_skew_violation=on_skew,
        safety_maximum_duration=safety.maximum_duration,
        watchdog=safety.emergency_stop,
    )


class DistributedCoordinator:
    """Orchestrate the full distributed lifecycle for a DistributedScenario.

    Delegates to :class:`~chaos_jungle.inject.group.InjectionGroupRunner`
    for the prepare/commit/revert protocol, then adds the blocking
    observation window from ``DistributedScenario.observation_duration``.

    Usage::

        coordinator = DistributedCoordinator(scenario)
        evidence = coordinator.run()   # blocks for observation_duration
        # or (from another thread)
        coordinator.stop()             # emergency stop

    Parameters
    ----------
    scenario :
        The :class:`~chaos_jungle.distributed.scenario.DistributedScenario`
        to execute.
    on_ready :
        Optional callback called once all members are READY, before commit.
        Signature: ``on_ready() -> None``.
    """

    def __init__(
        self,
        scenario: DistributedScenario,
        on_ready: Callable[[], None] | None = None,
    ) -> None:
        self.scenario = scenario
        self.on_ready = on_ready
        self._stop_event = threading.Event()
        self._runner = None
        self.evidence = GroupActivationEvidence(
            group_id=scenario.name,
            maximum_allowed_skew_ms=scenario.synchronization.maximum_skew_ms,
            strict_skew=scenario.synchronization.strict_skew,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self) -> GroupActivationEvidence:
        """Execute the full distributed lifecycle. Returns populated evidence."""
        from chaos_jungle.inject.group import InjectionGroupRunner

        group = _scenario_to_group(self.scenario)
        self._runner = InjectionGroupRunner(group)

        try:
            self._runner.start()
        except RuntimeError:
            # Cancelled during prepare or activation (only raised when atomic=True,
            # but we set atomic=False so this shouldn't fire — kept for safety)
            self.evidence = self._runner._evidence or self.evidence
            return self.evidence

        if self.on_ready is not None:
            try:
                self.on_ready()
            except Exception as exc:
                log.warning("on_ready callback raised: %s", exc)

        # ── Observe ───────────────────────────────────────────────────────────
        obs_remaining = max(0.0, self.scenario.observation_duration)
        safety_remaining = max(0.0, self.scenario.safety.maximum_duration)
        self._stop_event.wait(timeout=min(obs_remaining, safety_remaining))

        # ── Revert ────────────────────────────────────────────────────────────
        self.evidence = self._runner.stop()
        return self.evidence

    def stop(self) -> None:
        """Emergency stop — trigger immediate revert from any thread."""
        log.warning("Group %s: emergency stop requested", self.scenario.name)
        self._stop_event.set()
        if self._runner is not None:
            self._runner.emergency_stop()
