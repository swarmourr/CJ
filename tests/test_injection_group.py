"""Tests for InjectionGroup / InjectionGroupRunner.

All tests use lightweight stubs — no SSH / HTTP infrastructure needed.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from chaos_jungle.inject.group import InjectionGroup, InjectionGroupRunner
from chaos_jungle.distributed.scenario import Injection
from chaos_jungle.distributed.evidence import GroupActivationEvidence, MemberEvidence
from chaos_jungle.targets import LocalTarget


# ── Stubs ─────────────────────────────────────────────────────────────────────

class _NoOpFault:
    _fault_name = "noop"
    default_metrics: list = []
    def start(self, t): pass
    def stop(self, t): pass


class _RecordingFault:
    _fault_name = "rec"
    default_metrics: list = []
    def __init__(self): self.starts = 0; self.stops = 0
    def start(self, t): self.starts += 1
    def stop(self, t): self.stops += 1


class _FailStart:
    _fault_name = "failstart"
    default_metrics: list = []
    def start(self, t): raise RuntimeError("boom")
    def stop(self, t): pass


def _inj(id="m1", fault=None):
    return Injection(id=id, target=LocalTarget(), fault=fault or _NoOpFault())


def _group(n=2, **kw):
    injections = [_inj(f"m{i}") for i in range(n)]
    return InjectionGroup(name="tg", injections=injections, **kw)


# ── InjectionGroup validation ─────────────────────────────────────────────────

class TestInjectionGroupValidation:
    def test_defaults(self):
        g = InjectionGroup(name="g")
        assert g.synchronization == "scheduled"
        assert g.atomic is True
        assert g.maximum_skew_ms == 100.0
        assert g.start_after == 5.0
        assert g.watchdog is True

    def test_group_id_auto_assigned(self):
        g1 = InjectionGroup(name="a")
        g2 = InjectionGroup(name="b")
        assert g1.group_id != g2.group_id

    @pytest.mark.parametrize("bad", ["unknown", "", "sync"])
    def test_invalid_synchronization_raises(self, bad):
        with pytest.raises(ValueError, match="synchronization"):
            InjectionGroup(name="g", synchronization=bad)

    @pytest.mark.parametrize("bad", ["ignore", "skip"])
    def test_invalid_on_prepare_failure_raises(self, bad):
        with pytest.raises(ValueError):
            InjectionGroup(name="g", on_prepare_failure=bad)

    @pytest.mark.parametrize("bad", ["ignore", "abort"])
    def test_invalid_on_activation_failure_raises(self, bad):
        with pytest.raises(ValueError):
            InjectionGroup(name="g", on_activation_failure=bad)

    @pytest.mark.parametrize("bad", ["silent", "abort"])
    def test_invalid_on_skew_violation_raises(self, bad):
        with pytest.raises(ValueError):
            InjectionGroup(name="g", on_skew_violation=bad)

    @pytest.mark.parametrize("mode", ["best_effort", "barrier", "scheduled"])
    def test_valid_synchronization_modes(self, mode):
        g = InjectionGroup(name="g", synchronization=mode)
        assert g.synchronization == mode


# ── InjectionGroupRunner lifecycle ────────────────────────────────────────────

class TestInjectionGroupRunnerLifecycle:
    def test_start_stop_returns_evidence(self):
        runner = InjectionGroupRunner(_group(2, synchronization="best_effort", watchdog=False))
        runner.start()
        ev = runner.stop()
        assert isinstance(ev, GroupActivationEvidence)

    def test_members_count_matches_injections(self):
        runner = InjectionGroupRunner(_group(3, synchronization="best_effort", watchdog=False))
        runner.start()
        ev = runner.stop()
        assert len(ev.members) == 3

    def test_members_have_active_at(self):
        runner = InjectionGroupRunner(_group(2, synchronization="best_effort", watchdog=False))
        runner.start()
        ev = runner.stop()
        for m in ev.members:
            assert m.active_at is not None

    def test_members_have_reverted_at_after_stop(self):
        runner = InjectionGroupRunner(_group(2, synchronization="best_effort", watchdog=False))
        runner.start()
        ev = runner.stop()
        for m in ev.members:
            assert m.reverted_at is not None

    def test_stop_before_start_is_cancelled(self):
        runner = InjectionGroupRunner(InjectionGroup(name="g", watchdog=False))
        ev = runner.stop()
        assert ev.verdict == "cancelled"

    def test_stop_idempotent(self):
        runner = InjectionGroupRunner(_group(1, synchronization="best_effort", watchdog=False))
        runner.start()
        ev1 = runner.stop()
        ev2 = runner.stop()
        assert ev1 is ev2

    def test_fault_start_and_stop_each_called_once(self):
        f1, f2 = _RecordingFault(), _RecordingFault()
        injections = [
            Injection(id="a", target=LocalTarget(), fault=f1),
            Injection(id="b", target=LocalTarget(), fault=f2),
        ]
        group = InjectionGroup(name="rec", injections=injections,
                               synchronization="best_effort", watchdog=False)
        runner = InjectionGroupRunner(group)
        runner.start()
        runner.stop()
        assert f1.starts == 1 and f1.stops == 1
        assert f2.starts == 1 and f2.stops == 1

    def test_barrier_mode_activates_all(self):
        runner = InjectionGroupRunner(_group(2, synchronization="barrier", watchdog=False))
        runner.start()
        ev = runner.stop()
        assert ev.verdict != "cancelled"

    def test_scheduled_mode_start_after_zero(self):
        runner = InjectionGroupRunner(
            _group(2, synchronization="scheduled", start_after=0.0, watchdog=False)
        )
        runner.start()
        ev = runner.stop()
        assert len(ev.members) == 2


# ── Prepare failure policies ──────────────────────────────────────────────────

class TestPrepareFailure:
    class _BadTarget:
        host = "bad"
        def connect(self): raise OSError("unreachable")
        def disconnect(self): pass

    def test_atomic_cancel_raises(self):
        injections = [
            Injection(id="ok", target=LocalTarget(), fault=_NoOpFault()),
            Injection(id="bad", target=self._BadTarget(), fault=_NoOpFault()),
        ]
        group = InjectionGroup(name="atomic-cancel", injections=injections,
                               synchronization="best_effort", atomic=True, watchdog=False)
        with pytest.raises(RuntimeError, match="cancelled"):
            InjectionGroupRunner(group).start()

    def test_best_effort_prepare_continues(self):
        injections = [
            Injection(id="ok", target=LocalTarget(), fault=_NoOpFault()),
            Injection(id="bad", target=self._BadTarget(), fault=_NoOpFault()),
        ]
        group = InjectionGroup(
            name="be-prep", injections=injections,
            synchronization="best_effort",
            atomic=False, require_all_ready=False,
            on_prepare_failure="best_effort", watchdog=False,
        )
        runner = InjectionGroupRunner(group)
        runner.start()
        ev = runner.stop()
        assert ev.verdict != "cancelled"


# ── GroupActivationEvidence.group_valid ───────────────────────────────────────

class TestGroupValid:
    def _ev(self, skew_us=10000, skew_limit=100.0, manifested=(True, True), error=None):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t1 = datetime(2026, 1, 1, 0, 0, 0, skew_us, tzinfo=timezone.utc)
        ev = GroupActivationEvidence(group_id="g", maximum_allowed_skew_ms=skew_limit)
        ev.members = [
            MemberEvidence(host="a", injection_id="m0", active_at=t0,
                           manifested=manifested[0], error=error),
            MemberEvidence(host="b", injection_id="m1", active_at=t1,
                           manifested=manifested[1]),
        ]
        return ev

    def test_valid_when_skew_ok_and_manifested(self):
        ev = self._ev()
        ev.finalize()
        assert ev.group_valid is True

    def test_invalid_when_skew_exceeded(self):
        ev = self._ev(skew_us=200_000, skew_limit=50.0)
        ev.finalize()
        assert ev.group_valid is False

    def test_invalid_when_member_not_manifested(self):
        ev = self._ev(manifested=(True, False))
        ev.finalize()
        assert ev.group_valid is False

    def test_invalid_when_manifested_unknown(self):
        ev = self._ev(manifested=(None, None))
        ev.finalize()
        # Unknown manifestation → verdict="inconclusive" → group_valid must be False
        assert ev.verdict == "inconclusive"
        assert ev.group_valid is False

    def test_invalid_when_cancelled(self):
        ev = GroupActivationEvidence(group_id="g")
        ev.verdict = "cancelled"
        assert ev.group_valid is False

    def test_invalid_when_member_has_error(self):
        ev = self._ev(error="boom")
        ev.verdict = "valid"
        assert ev.group_valid is False


# ── Watchdog ──────────────────────────────────────────────────────────────────

class TestWatchdog:
    def test_watchdog_fires_and_stops_group(self):
        group = _group(1, synchronization="best_effort",
                       watchdog=True, safety_maximum_duration=0.1)
        runner = InjectionGroupRunner(group)
        runner.start()
        time.sleep(0.3)
        # watchdog should have fired; evidence is populated
        assert runner.evidence is not None

    def test_watchdog_disabled_does_not_auto_stop(self):
        group = _group(1, synchronization="best_effort", watchdog=False)
        runner = InjectionGroupRunner(group)
        runner.start()
        time.sleep(0.05)
        assert not runner._stop_event.is_set()
        runner.stop()


# ── YAML validation ───────────────────────────────────────────────────────────

class TestYAMLValidation:
    def _data(self):
        return {
            "apiVersion": "cj.io/v1",
            "kind": "InjectionGroup",
            "name": "g",
            "synchronization": "scheduled",
            "injections": [
                {"id": "m1", "target": "local", "fault": {"kind": "NetworkDelay", "delay": "100ms"}},
                {"id": "m2", "target": "local", "fault": {"kind": "NetworkDelay", "delay": "200ms"}},
            ],
        }

    def test_valid_has_no_errors(self):
        from chaos_jungle.config import ConfigLoader
        assert ConfigLoader.validate_injection_group_dict(self._data()) == []

    def test_missing_name_is_error(self):
        from chaos_jungle.config import ConfigLoader
        d = self._data(); del d["name"]
        assert any("name" in e for e in ConfigLoader.validate_injection_group_dict(d))

    def test_empty_injections_is_error(self):
        from chaos_jungle.config import ConfigLoader
        d = self._data(); d["injections"] = []
        assert any("injections" in e for e in ConfigLoader.validate_injection_group_dict(d))

    def test_invalid_sync_mode_is_error(self):
        from chaos_jungle.config import ConfigLoader
        d = self._data(); d["synchronization"] = "quantum"
        assert any("synchronization" in e for e in ConfigLoader.validate_injection_group_dict(d))

    def test_invalid_on_prepare_failure_is_error(self):
        from chaos_jungle.config import ConfigLoader
        d = self._data(); d["on_prepare_failure"] = "ignore"
        assert any("on_prepare_failure" in e for e in ConfigLoader.validate_injection_group_dict(d))

    def test_unknown_key_is_error(self):
        from chaos_jungle.config import ConfigLoader
        d = self._data(); d["bogus"] = 1
        assert any("unknown" in e.lower() for e in ConfigLoader.validate_injection_group_dict(d))

    def test_validate_file_dispatches(self, tmp_path):
        import yaml
        from chaos_jungle.config import ConfigLoader
        p = tmp_path / "ig.yaml"
        p.write_text(yaml.dump(self._data()))
        assert ConfigLoader.validate_file(str(p)) == []


# ── InjectionGroupSpec IR ─────────────────────────────────────────────────────

class TestInjectionGroupSpec:
    def test_spec_to_dict(self):
        from chaos_jungle.plan import InjectionGroupSpec, InjectionMemberSpec, TargetSpec, FaultSpec
        spec = InjectionGroupSpec(
            name="g",
            injections=[InjectionMemberSpec(
                id="m1",
                target=TargetSpec(kind="local"),
                fault=FaultSpec(fault_class="NetworkDelay"),
            )],
        )
        d = spec.to_dict()
        assert d["name"] == "g"
        assert d["injections"][0]["id"] == "m1"

    def test_experiment_plan_has_groups_field(self):
        from chaos_jungle.plan import ExperimentPlan, ScenarioSpec
        plan = ExperimentPlan(scenario=ScenarioSpec(name="s"))
        assert plan.groups == []

    def test_plan_to_dict_includes_groups(self):
        from chaos_jungle.plan import ExperimentPlan, ScenarioSpec, InjectionGroupSpec
        plan = ExperimentPlan(scenario=ScenarioSpec(name="s"), groups=[InjectionGroupSpec(name="g")])
        d = plan.to_dict()
        assert len(d["groups"]) == 1 and d["groups"][0]["name"] == "g"
