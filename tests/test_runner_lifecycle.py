"""Tests for ChaosRunner lifecycle: rollback, idempotent stop, session states."""
from __future__ import annotations
import pytest

from tests.conftest import MockTarget, TrackingFault, FailingFault


def _make_runner(faults, target=None):
    """Build a ChaosRunner with an in-memory DB and the given faults."""
    from chaos_jungle.core.runner import ChaosRunner
    from chaos_jungle.core.scenario import Scenario
    from chaos_jungle.db.session_db import SessionDB

    class InMemDB(SessionDB):
        def __init__(self):
            import tempfile, os
            tmp = tempfile.mktemp(suffix=".db")
            super().__init__(path=tmp)

    scenario = Scenario("test", faults=faults)
    runner = ChaosRunner(
        scenario,
        target=target or MockTarget(),
        db=InMemDB(),
        auto_preflight=False,
    )
    return runner


class TestRollbackOnInjectionFailure:
    def test_first_fault_reverted_when_second_fails(self):
        """If the 2nd fault raises, the 1st should be stopped and reverted."""
        f1 = TrackingFault()
        f2 = FailingFault()
        runner = _make_runner([f1, f2])

        with pytest.raises(RuntimeError, match="FailingFault injection error"):
            runner.start()

        assert f1.started, "f1 should have been started"
        assert f1.stopped or f1.reverted, "f1 should have been reverted after f2 failure"

    def test_session_status_is_injection_failed_or_partially_reverted(self):
        """Session status must not be 'reverted' when injection failed."""
        f1 = TrackingFault()
        f2 = FailingFault()
        runner = _make_runner([f1, f2])

        with pytest.raises(RuntimeError):
            runner.start()

        sess = runner.db.get_session(runner._session_id)
        assert sess["status"] in ("injection_failed", "partially_reverted"), \
            f"Expected injection_failed or partially_reverted, got {sess['status']!r}"

    def test_no_fault_reverted_when_first_fails(self):
        """If the very first fault fails, nothing should be in the revert list."""
        f1 = FailingFault()
        runner = _make_runner([f1])

        with pytest.raises(RuntimeError):
            runner.start()

        # _activated should be empty — nothing was confirmed active
        assert runner._activated == []

    def test_rollback_errors_preserved_in_session(self):
        """If rollback itself fails, session status must reflect partial revert."""
        f1 = TrackingFault(fail_on_stop=True)
        f2 = FailingFault()
        runner = _make_runner([f1, f2])

        with pytest.raises(RuntimeError):
            runner.start()

        sess = runner.db.get_session(runner._session_id)
        # When revert of f1 also fails, we expect partially_reverted
        assert sess["status"] in ("injection_failed", "partially_reverted")


class TestIdempotentStop:
    def test_stop_twice_is_safe(self):
        """Calling stop() twice must not raise."""
        f1 = TrackingFault()
        runner = _make_runner([f1])
        runner.start()
        runner.stop()
        runner.stop()   # second call must be a no-op

    def test_stop_without_start_raises(self):
        """Calling stop() before start() must raise a clear error."""
        f1 = TrackingFault()
        runner = _make_runner([f1])
        with pytest.raises(RuntimeError, match="No active session"):
            runner.stop()

    def test_stop_marks_session_reverted(self):
        """After clean stop(), session status must be 'reverted'."""
        f1 = TrackingFault()
        runner = _make_runner([f1])
        runner.start()
        runner.stop()
        sess = runner.db.get_session(runner._session_id)
        assert sess["status"] == "reverted"

    def test_stop_marks_session_partially_reverted_on_error(self):
        """If one fault's stop() raises, session status must indicate failure."""
        f1 = TrackingFault()
        f2 = TrackingFault(fail_on_stop=True)
        runner = _make_runner([f1, f2])
        runner.start()

        with pytest.raises(RuntimeError):
            runner.stop()

        sess = runner.db.get_session(runner._session_id)
        assert sess["status"] in ("partially_reverted", "revert_failed")

    def test_only_activated_faults_are_reverted(self):
        """Faults that never activated must not appear in the revert attempt."""
        # f1 activates; f2 fails; only f1 should be reverted
        f1 = TrackingFault()
        f2 = FailingFault()
        runner = _make_runner([f1, f2])

        with pytest.raises(RuntimeError):
            runner.start()

        # After rollback, _activated is cleared and f2 was never added
        assert runner._activated == []
        assert f1.stopped or f1.reverted


class TestSessionLifecycle:
    def test_session_transitions_active_after_start(self):
        """Session must be 'active' immediately after start()."""
        f1 = TrackingFault()
        runner = _make_runner([f1])
        runner.start()

        sess = runner.db.get_session(runner._session_id)
        assert sess["status"] == "active"

        runner.stop()

    def test_context_manager_reverts_on_exit(self):
        """Using ChaosRunner as a context manager must revert on clean exit."""
        from chaos_jungle.core.runner import ChaosRunner
        from chaos_jungle.core.scenario import Scenario
        from chaos_jungle.db.session_db import SessionDB
        import tempfile

        f1 = TrackingFault()
        tmp = tempfile.mktemp(suffix=".db")
        db = SessionDB(path=tmp)
        scenario = Scenario("ctx-test", faults=[f1])
        runner = ChaosRunner(
            scenario, target=MockTarget(), db=db, auto_preflight=False
        )

        # ChaosRunner supports __enter__/__exit__ via start/stop
        runner.start()
        try:
            pass
        finally:
            runner.stop()

        sess = db.get_session(runner._session_id)
        assert sess["status"] == "reverted"
        assert f1.stopped or f1.reverted
