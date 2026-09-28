"""Distributed injection coordinator.

Implements the two-phase prepare/commit protocol described in the CJ
architecture for simultaneous multi-host fault injection:

Phase 1 — Prepare (concurrent across all members)
    Each member thread:
      1. Calls ``fault.prepare(target)`` (if the fault supports it), or
         falls back to verifying target reachability via ``target.connect()``.
      2. Pre-establishes the connection so commit-phase latency is minimised.
      3. Probes the remote clock offset.
      4. Signals READY (or records an error).

Phase 2 — Commit (concurrent, precisely timed)
    Coordinator computes ``T = now + start_after`` after all READY.
    Each member thread:
      1. Busy-sleeps until time T (coarse sleep + spin for the last 1 ms).
      2. Calls ``fault.start(target)``.
      3. Records the actual ``active_at`` timestamp.

Observe
    Coordinator sleeps for ``observation_duration`` seconds or until
    ``stop()`` is called (emergency stop).

Revert (concurrent)
    Each member calls ``fault.stop(target)`` and records ``reverted_at``.

Evidence
    :class:`~chaos_jungle.distributed.evidence.GroupActivationEvidence` is
    populated with per-member timestamps; ``finalize()`` computes skew
    and sets the verdict.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from .clock import ClockProbe
from .evidence import GroupActivationEvidence, MemberEvidence
from .scenario import AtomicityConfig, DistributedScenario, Injection, SyncConfig

log = logging.getLogger(__name__)


def _host_of(target: object) -> str:
    for attr in ("host", "url", "base_url"):
        v = getattr(target, attr, None)
        if v:
            return str(v)
    return repr(target)


class _MemberState:
    """Per-member mutable state shared between coordinator and member threads."""

    def __init__(self, injection: Injection) -> None:
        self.injection = injection
        self.ready_event = threading.Event()
        self.evidence = MemberEvidence(
            host=_host_of(injection.target),
            injection_id=injection.id,
        )
        self.error: str | None = None
        self.connected: bool = False


class DistributedCoordinator:
    """Orchestrate the full distributed lifecycle for a DistributedScenario.

    Usage::

        coordinator = DistributedCoordinator(scenario)
        evidence = coordinator.run()          # blocks
        # or
        coordinator.stop()                    # emergency stop from another thread

    Parameters
    ----------
    scenario :
        The :class:`~chaos_jungle.distributed.scenario.DistributedScenario`
        to execute.
    on_ready :
        Optional callback called once all members are READY, before commit.
        Receives the list of :class:`_MemberState` objects.
    """

    def __init__(
        self,
        scenario: DistributedScenario,
        on_ready: Callable[[list[_MemberState]], None] | None = None,
    ) -> None:
        self.scenario = scenario
        self.on_ready = on_ready
        self._states: list[_MemberState] = [
            _MemberState(inj) for inj in scenario.members
        ]
        self._stop_event = threading.Event()
        self.evidence = GroupActivationEvidence(
            group_id=scenario.name,
            maximum_allowed_skew_ms=scenario.synchronization.maximum_skew_ms,
            strict_skew=scenario.synchronization.strict_skew,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self) -> GroupActivationEvidence:
        """Execute the full distributed lifecycle. Returns populated evidence."""
        sync = self.scenario.synchronization
        atomicity = self.scenario.atomicity
        safety = self.scenario.safety

        # ── Phase 1: Prepare ─────────────────────────────────────────────────
        prepare_threads = [
            threading.Thread(
                target=self._prepare_member, args=(s,), daemon=True, name=f"cj-prepare-{s.injection.id}"
            )
            for s in self._states
        ]
        for t in prepare_threads:
            t.start()

        all_ready = self._wait_all_ready(timeout=sync.prepare_timeout_s)

        failed = [s for s in self._states if s.error]
        if failed:
            log.warning(
                "Group %s: %d member(s) failed prepare: %s",
                self.scenario.name,
                len(failed),
                [s.injection.id for s in failed],
            )

        if not all_ready and (
            atomicity.prepare == "all_or_nothing" or sync.require_all_ready
        ):
            log.warning("Group %s: cancelling — not all members ready", self.scenario.name)
            self._stop_event.set()
            for t in prepare_threads:
                t.join(timeout=5)
            self.evidence.verdict = "cancelled"
            self.evidence.members = [s.evidence for s in self._states]
            return self.evidence

        if self.on_ready:
            self.on_ready(self._states)

        # ── Phase 2: Commit ───────────────────────────────────────────────────
        now = time.time()
        if sync.mode == "scheduled":
            commit_at = now + sync.start_after
        else:
            commit_at = now  # barrier / best_effort: activate immediately

        self.evidence.requested_start = datetime.fromtimestamp(commit_at, tz=timezone.utc)
        log.info(
            "Group %s: committing %d member(s) at %s  (mode=%s)",
            self.scenario.name,
            len(self._states),
            self.evidence.requested_start.isoformat(),
            sync.mode,
        )

        commit_threads = [
            threading.Thread(
                target=self._commit_member, args=(s, commit_at), daemon=True,
                name=f"cj-commit-{s.injection.id}",
            )
            for s in self._states
        ]
        for t in commit_threads:
            t.start()

        # Wait for all commits to finish (with generous timeout)
        for t in commit_threads:
            t.join(timeout=sync.start_after + 30)
        for t in prepare_threads:
            t.join(timeout=2)

        # Check for activation failures
        activation_failed = [s for s in self._states if s.evidence.active_at is None and not s.error]
        if activation_failed and atomicity.activation_failure == "rollback_all":
            log.error("Group %s: activation failed — rolling back all members", self.scenario.name)
            self._revert_all()
            self.evidence.members = [s.evidence for s in self._states]
            self.evidence.verdict = "cancelled"
            return self.evidence

        # ── Observe ───────────────────────────────────────────────────────────
        obs_remaining = max(0.0, self.scenario.observation_duration)
        safety_remaining = max(0.0, safety.maximum_duration)
        wait_for = min(obs_remaining, safety_remaining)

        self._stop_event.wait(timeout=wait_for)

        # ── Revert ────────────────────────────────────────────────────────────
        self._revert_all()

        # ── Evidence ─────────────────────────────────────────────────────────
        self.evidence.members = [s.evidence for s in self._states]
        self.evidence.finalize()
        return self.evidence

    def stop(self) -> None:
        """Emergency stop — trigger immediate revert from any thread."""
        log.warning("Group %s: emergency stop requested", self.scenario.name)
        self._stop_event.set()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _prepare_member(self, state: _MemberState) -> None:
        """Phase 1 worker: connect, validate, probe clock, signal READY."""
        inj = state.injection
        try:
            # Pre-establish connection to minimise commit-phase latency
            if hasattr(inj.target, "connect"):
                try:
                    inj.target.connect()
                    state.connected = True
                except Exception as exc:
                    raise ConnectionError(
                        f"Cannot connect to {_host_of(inj.target)}: {exc}"
                    ) from exc

            # Fault-level prepare (optional interface)
            if hasattr(inj.fault, "prepare"):
                inj.fault.prepare(inj.target)

            # Clock offset probe
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

    def _commit_member(self, state: _MemberState, commit_at: float) -> None:
        """Phase 2 worker: sleep until commit_at then activate fault."""
        inj = state.injection
        if state.error:
            return

        # High-precision timed activation: coarse sleep + spin for last 1 ms
        slack = commit_at - time.time()
        if slack > 0.001:
            time.sleep(slack - 0.001)
        while time.time() < commit_at:
            pass

        try:
            inj.fault.start(inj.target)
            state.evidence.active_at = datetime.now(timezone.utc)
            log.debug(
                "Member %s ACTIVE at %s",
                inj.id,
                state.evidence.active_at.isoformat(),
            )
        except Exception as exc:
            state.error = str(exc)
            state.evidence.error = str(exc)
            log.error("Member %s activation FAILED: %s", inj.id, exc)
            if self.scenario.atomicity.activation_failure == "rollback_all":
                self._stop_event.set()

    def _revert_member(self, state: _MemberState) -> None:
        inj = state.injection
        if state.evidence.active_at is None:
            return  # never activated — nothing to revert
        try:
            inj.fault.stop(inj.target)
            state.evidence.reverted_at = datetime.now(timezone.utc)
            log.debug("Member %s REVERTED", inj.id)
        except Exception as exc:
            log.error("Member %s revert FAILED: %s", inj.id, exc)
            if self.scenario.atomicity.recovery_failure == "mark_invalid":
                self.evidence.verdict = "recovery_invalid"
        finally:
            # Disconnect if we connected in prepare
            if state.connected and hasattr(inj.target, "disconnect"):
                try:
                    inj.target.disconnect()
                except Exception:
                    pass

    def _revert_all(self) -> None:
        threads = [
            threading.Thread(
                target=self._revert_member, args=(s,), daemon=True,
                name=f"cj-revert-{s.injection.id}",
            )
            for s in self._states
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

    def _wait_all_ready(self, timeout: float) -> bool:
        """Block until every member signals ready. Returns True if all succeeded."""
        deadline = time.monotonic() + timeout
        for state in self._states:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            state.ready_event.wait(timeout=max(0.0, remaining))
        return all(s.error is None for s in self._states)
