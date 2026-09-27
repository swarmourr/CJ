"""Integration tests: injection verification, recovery, and emergency abort."""
from __future__ import annotations

import tempfile
import threading
import time

import pytest

from tests.conftest import MockTarget, TrackingFault
from chaos_jungle.faults.base import Fault
from chaos_jungle.core.runner import ChaosRunner
from chaos_jungle.core.scenario import Scenario
from chaos_jungle.db.session_db import SessionDB


# ── Test helpers ──────────────────────────────────────────────────────────────

def _make_runner(faults, target=None, policy=None):
    tmp = tempfile.mktemp(suffix=".db")
    db = SessionDB(path=tmp)
    scenario = Scenario("integration-test", faults=faults)
    return ChaosRunner(
        scenario,
        target=target or MockTarget(),
        db=db,
        auto_preflight=False,
        policy=policy,
    )


class UnverifiedFault(Fault):
    """Fault that does NOT override verify_active / verify_recovered.

    The base-class defaults return ``not_implemented=True`` for both so the
    runner logs a warning but does not roll back.
    """

    danger_level = 0
    default_metrics: list = []
    dependencies: list = []
    pip_dependencies: list = []

    def start(self, target) -> None:
        pass

    def stop(self, target) -> None:
        pass

    def revert(self, target) -> None:
        pass

    def preflight(self, target, auto_install=False) -> None:
        pass

    def _parameters(self) -> dict:
        return {}


# ── Verdict tests ─────────────────────────────────────────────────────────────

class TestInjectionVerdict:
    def test_unverified_fault_gives_inconclusive_verdict(self):
        """A fault with no verify override must produce an INCONCLUSIVE verdict."""
        f = UnverifiedFault()
        runner = _make_runner([f])
        runner.start()
        runner.stop()

        sess = runner.db.get_session(runner._session_id)
        assert sess["verdict"] == "INCONCLUSIVE", (
            f"Expected INCONCLUSIVE, got {sess['verdict']!r}"
        )

    def test_verified_fault_gives_valid_verdict(self):
        """A fault with real verify_active + verify_recovered → VALID verdict."""
        f = TrackingFault()
        runner = _make_runner([f])
        runner.start()
        runner.stop()

        sess = runner.db.get_session(runner._session_id)
        assert sess["verdict"] == "VALID", (
            f"Expected VALID, got {sess['verdict']!r}"
        )

    def test_injection_verified_flag_set_in_db(self):
        """After a successful start(), the DB fault record has injection_verified = 1."""
        f = TrackingFault()
        runner = _make_runner([f])
        runner.start()

        row = runner.db._conn.execute(
            "SELECT injection_verified FROM faults WHERE session_id = ?",
            (runner._session_id,),
        ).fetchone()
        assert row is not None
        assert row[0] == 1, f"Expected injection_verified=1, got {row[0]!r}"

        runner.stop()


# ── Lifecycle tests ───────────────────────────────────────────────────────────

class TestStartStopLifecycle:
    def test_start_stop_completes_without_deadlock(self):
        """Full start/stop must finish within 10 seconds (no deadlock)."""
        f = TrackingFault()
        runner = _make_runner([f])

        done = threading.Event()
        exc_holder: list[Exception] = []

        def _run() -> None:
            try:
                runner.start()
                runner.stop()
            except Exception as exc:
                exc_holder.append(exc)
            finally:
                done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        assert done.wait(timeout=10), "start/stop deadlocked or timed out after 10s"

        if exc_holder:
            raise exc_holder[0]

        sess = runner.db.get_session(runner._session_id)
        assert sess["status"] == "reverted"
        assert f.started
        assert f.stopped or f.reverted

    def test_shared_resource_stop_before_verify_recovered(self):
        """stop() calls _stop_shared_resources() before verify_recovered() so
        verify_recovered sees a clean environment (no shared proxy running)."""
        # UnverifiedFault: verify_recovered() uses the base default (not_implemented)
        # so it never checks the env — but TrackingFault checks fault.stopped/reverted
        # AFTER stop() ran, which works correctly in the two-phase design.
        f = TrackingFault()
        runner = _make_runner([f])
        runner.start()
        runner.stop()

        # If verify_recovered ran BEFORE stop() the TrackingFault would see
        # stopped=False and return verified=False, giving INVALID verdict.
        # The correct two-phase order → VALID.
        sess = runner.db.get_session(runner._session_id)
        assert sess["verdict"] == "VALID"


# ── Emergency abort tests ─────────────────────────────────────────────────────

class TestEmergencyAbort:
    def test_emergency_stop_sets_status_aborted(self):
        """Calling policy.emergency_stop() while the runner is active must set
        session status to 'aborted' without deadlocking."""
        from chaos_jungle.core.guardrails import SafetyPolicy

        f = TrackingFault()
        # Short monitor_interval_s so the abort loop fires quickly in tests.
        policy = SafetyPolicy(max_duration_s=3600, monitor_interval_s=0.05)
        runner = _make_runner([f], policy=policy)
        runner.start()

        # Trigger emergency stop from the test thread.
        policy.emergency_stop(reason="test-triggered abort")

        # Wait up to 5 s for the abort-stop thread to fully close the session.
        # "stopping" is a transient state; we wait for a terminal state.
        deadline = time.time() + 5
        while time.time() < deadline:
            sess = runner.db.get_session(runner._session_id)
            if sess["status"] not in ("active", "stopping"):
                break
            time.sleep(0.05)

        sess = runner.db.get_session(runner._session_id)
        assert sess["status"] == "aborted", (
            f"Expected 'aborted', got {sess['status']!r}"
        )

    def test_emergency_abort_does_not_deadlock(self):
        """Emergency abort (launched in the abort-loop thread) must not deadlock.
        Verified by the test completing within a timeout."""
        from chaos_jungle.core.guardrails import SafetyPolicy

        f = TrackingFault()
        policy = SafetyPolicy(max_duration_s=3600, monitor_interval_s=0.05)
        runner = _make_runner([f], policy=policy)

        done = threading.Event()

        def _lifecycle() -> None:
            runner.start()
            policy.emergency_stop(reason="deadlock-check")
            # Wait for the runner's abort-stop thread to finish.
            deadline = time.time() + 5
            while time.time() < deadline:
                sess = runner.db.get_session(runner._session_id)
                if sess["status"] != "active":
                    break
                time.sleep(0.05)
            done.set()

        t = threading.Thread(target=_lifecycle, daemon=True)
        t.start()
        assert done.wait(timeout=10), "Emergency abort deadlocked (did not complete in 10s)"
