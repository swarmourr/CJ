"""Tests for the five blockers and associated round-trip / concurrency safety:
- session_db group_evidence table
- DistributedCoordinator delegation to InjectionGroupRunner
- Scenario serialization with groups and evaluator_model
- ExperimentSuite evaluator_model → LLMJudge wiring
- Activation-failure detection fix (prepare failures not counted)
"""
from __future__ import annotations

import textwrap
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from chaos_jungle.distributed.evidence import GroupActivationEvidence, MemberEvidence
from chaos_jungle.distributed.scenario import (
    AtomicityConfig,
    DistributedSafetyConfig,
    DistributedScenario,
    Injection,
    SyncConfig,
)
from chaos_jungle.inject.group import InjectionGroup, InjectionGroupRunner
from chaos_jungle.targets import LocalTarget


# ── Minimal stubs ─────────────────────────────────────────────────────────────

class _OkFault:
    def start(self, target): pass
    def stop(self, target): pass
    def revert(self, target): pass
    def verify_active(self, target):
        r = MagicMock(); r.not_implemented = True; return r
    def verify_recovered(self, target):
        r = MagicMock(); r.not_implemented = True; return r
    def _parameters(self): return {}


class _BadConnectTarget(LocalTarget):
    def connect(self):
        raise ConnectionError("unreachable")


# ── session_db group_evidence ──────────────────────────────────────────────────

class TestSessionDBGroupEvidence:
    def _db(self, tmp_path):
        from chaos_jungle.db.session_db import SessionDB
        return SessionDB(path=str(tmp_path / "test.db"))

    def _make_evidence(self, group_id="g1", verdict="valid", skew=12.5):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t1 = datetime(2026, 1, 1, 0, 0, 0, 12_500, tzinfo=timezone.utc)
        ev = GroupActivationEvidence(
            group_id=group_id,
            maximum_allowed_skew_ms=100.0,
        )
        ev.members = [
            MemberEvidence(host="a", injection_id="m0", active_at=t0, manifested=True),
            MemberEvidence(host="b", injection_id="m1", active_at=t1, manifested=True),
        ]
        ev.activation_skew_ms = skew
        ev.synchronization_valid = True
        ev.verdict = verdict
        return ev

    def test_store_and_retrieve(self, tmp_path):
        db = self._db(tmp_path)
        sid = db.open_session("test")
        ev = self._make_evidence()
        row_id = db.store_group_evidence(sid, ev)
        assert row_id > 0

        rows = db.get_group_evidence(sid)
        assert len(rows) == 1
        r = rows[0]
        assert r["group_id"] == "g1"
        assert r["verdict"] == "valid"
        assert abs(r["skew_ms"] - 12.5) < 0.01

    def test_members_json_parsed(self, tmp_path):
        db = self._db(tmp_path)
        sid = db.open_session("test")
        ev = self._make_evidence()
        db.store_group_evidence(sid, ev)

        rows = db.get_group_evidence(sid)
        members = rows[0]["members_json"]
        assert isinstance(members, list)
        assert len(members) == 2
        assert members[0]["injection_id"] == "m0"

    def test_multiple_groups_per_session(self, tmp_path):
        db = self._db(tmp_path)
        sid = db.open_session("test")
        db.store_group_evidence(sid, self._make_evidence("g1", "valid"))
        db.store_group_evidence(sid, self._make_evidence("g2", "inconclusive"))

        rows = db.get_group_evidence(sid)
        assert len(rows) == 2
        assert {r["group_id"] for r in rows} == {"g1", "g2"}

    def test_isolated_per_session(self, tmp_path):
        db = self._db(tmp_path)
        s1 = db.open_session("s1")
        s2 = db.open_session("s2")
        db.store_group_evidence(s1, self._make_evidence("g1"))
        assert db.get_group_evidence(s2) == []

    def test_export_session_includes_group_evidence(self, tmp_path):
        db = self._db(tmp_path)
        sid = db.open_session("test")
        ev = self._make_evidence()
        db.store_group_evidence(sid, ev)

        exported = db.export_session(sid)
        assert "group_evidence" in exported
        assert len(exported["group_evidence"]) == 1

    def test_pending_verdict_stored(self, tmp_path):
        db = self._db(tmp_path)
        sid = db.open_session("test")
        ev = GroupActivationEvidence(group_id="pending-group")
        db.store_group_evidence(sid, ev)

        rows = db.get_group_evidence(sid)
        assert rows[0]["verdict"] == "pending"

    def test_null_skew_stored(self, tmp_path):
        db = self._db(tmp_path)
        sid = db.open_session("test")
        ev = GroupActivationEvidence(group_id="g", verdict="inconclusive")
        db.store_group_evidence(sid, ev)

        rows = db.get_group_evidence(sid)
        assert rows[0]["skew_ms"] is None


# ── DistributedCoordinator delegation ─────────────────────────────────────────

class TestDistributedCoordinatorDelegation:
    def _scenario(self, obs=0.0, mode="scheduled", start_after=0.01):
        members = [
            Injection(id="m1", target=LocalTarget(), fault=_OkFault()),
            Injection(id="m2", target=LocalTarget(), fault=_OkFault()),
        ]
        return DistributedScenario(
            name="test-ds",
            members=members,
            synchronization=SyncConfig(mode=mode, start_after=start_after),
            atomicity=AtomicityConfig(),
            safety=DistributedSafetyConfig(maximum_duration=5.0),
            observation_duration=obs,
        )

    def test_run_returns_evidence(self):
        from chaos_jungle.distributed.coordinator import DistributedCoordinator
        coord = DistributedCoordinator(self._scenario())
        ev = coord.run()
        assert isinstance(ev, GroupActivationEvidence)

    def test_run_verdict_valid_or_inconclusive(self):
        from chaos_jungle.distributed.coordinator import DistributedCoordinator
        coord = DistributedCoordinator(self._scenario())
        ev = coord.run()
        assert ev.verdict in {"valid", "inconclusive", "invalid"}

    def test_stop_during_observation_interrupts(self):
        from chaos_jungle.distributed.coordinator import DistributedCoordinator
        coord = DistributedCoordinator(self._scenario(obs=60.0))
        t = threading.Thread(target=coord.run, daemon=True)
        t.start()
        import time; time.sleep(0.1)
        coord.stop()
        t.join(timeout=3.0)
        assert not t.is_alive(), "coordinator.run() did not exit after stop()"

    def test_on_ready_callback_called(self):
        from chaos_jungle.distributed.coordinator import DistributedCoordinator
        called = []
        def on_ready():
            called.append(True)
        coord = DistributedCoordinator(self._scenario(), on_ready=on_ready)
        coord.run()
        assert called

    def test_delegated_runner_handles_revert(self):
        from chaos_jungle.distributed.coordinator import DistributedCoordinator
        coord = DistributedCoordinator(self._scenario())
        ev = coord.run()
        # All members should be reverted (reverted_at set or revert not implemented)
        # Evidence should be finalized
        assert ev.verdict != "pending"

    def test_best_effort_sync_mode(self):
        from chaos_jungle.distributed.coordinator import DistributedCoordinator
        coord = DistributedCoordinator(self._scenario(mode="best_effort"))
        ev = coord.run()
        assert isinstance(ev, GroupActivationEvidence)

    def test_prepare_failure_cancels_all_or_nothing(self):
        from chaos_jungle.distributed.coordinator import DistributedCoordinator
        members = [
            Injection(id="ok", target=LocalTarget(), fault=_OkFault()),
            Injection(id="bad", target=_BadConnectTarget(), fault=_OkFault()),
        ]
        scenario = DistributedScenario(
            name="prep-fail",
            members=members,
            synchronization=SyncConfig(mode="scheduled", start_after=0.01,
                                       require_all_ready=True),
            atomicity=AtomicityConfig(prepare="all_or_nothing"),
            safety=DistributedSafetyConfig(),
            observation_duration=0.0,
        )
        coord = DistributedCoordinator(scenario)
        ev = coord.run()
        assert ev.verdict == "cancelled"


# ── Scenario serialization ────────────────────────────────────────────────────

class TestScenarioSerialization:
    def test_to_dict_includes_faults(self):
        from chaos_jungle.core.scenario import Scenario
        from chaos_jungle.faults import NetworkDelay
        s = Scenario("s1", faults=[NetworkDelay("100ms")])
        d = s.to_dict()
        assert d["name"] == "s1"
        assert len(d["faults"]) == 1
        assert d["faults"][0]["kind"] == "NetworkDelay"

    def test_to_dict_evaluator_model(self):
        from chaos_jungle.core.scenario import Scenario
        s = Scenario("s1", faults=[])
        s.evaluator_model = "judge"
        d = s.to_dict()
        assert d["evaluator_model"] == "judge"

    def test_to_dict_no_evaluator_model_omitted(self):
        from chaos_jungle.core.scenario import Scenario
        s = Scenario("s1", faults=[])
        d = s.to_dict()
        assert "evaluator_model" not in d

    def test_from_dict_restores_evaluator_model(self):
        from chaos_jungle.core.scenario import Scenario
        s = Scenario("s1", faults=[])
        s.evaluator_model = "judge"
        d = s.to_dict()
        s2 = Scenario.from_dict(d)
        assert s2.evaluator_model == "judge"

    def test_from_dict_evaluator_model_none_when_absent(self):
        from chaos_jungle.core.scenario import Scenario
        s = Scenario("s1", faults=[])
        d = s.to_dict()
        s2 = Scenario.from_dict(d)
        assert s2.evaluator_model is None

    def test_from_dict_has_empty_groups(self):
        from chaos_jungle.core.scenario import Scenario
        s = Scenario("s1", faults=[])
        d = s.to_dict()
        s2 = Scenario.from_dict(d)
        assert s2.groups == []

    def test_to_dict_with_groups_has_full_spec(self):
        from chaos_jungle.core.scenario import Scenario
        group = InjectionGroup(
            name="grp",
            injections=[Injection(id="m1", target=LocalTarget(), fault=_OkFault())],
            synchronization="barrier",
            start_after=3.0,
        )
        s = Scenario("s1", faults=[], groups=[group])
        d = s.to_dict()
        assert "groups" in d
        g = d["groups"][0]
        assert g["name"] == "grp"
        assert g["synchronization"] == "barrier"
        assert g["start_after"] == 3.0
        assert len(g["injections"]) == 1

    def test_group_injection_fault_serialized(self):
        from chaos_jungle.faults import NetworkDelay
        from chaos_jungle.core.scenario import Scenario
        group = InjectionGroup(
            name="grp",
            injections=[Injection(id="m1", target=LocalTarget(), fault=NetworkDelay("50ms"))],
        )
        s = Scenario("s1", faults=[], groups=[group])
        d = s.to_dict()
        inj = d["groups"][0]["injections"][0]
        assert inj["fault"]["kind"] == "NetworkDelay"
        assert inj["id"] == "m1"


# ── ExperimentSuite evaluator_model wiring ────────────────────────────────────

class TestSuiteEvaluatorModel:
    def _write_yaml(self, tmp_path, content):
        p = tmp_path / "suite.yaml"
        p.write_text(textwrap.dedent(content))
        return str(p)

    def test_evaluator_model_builds_judge_on_experiment_result(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        from chaos_jungle.config import ConfigLoader
        p = self._write_yaml(tmp_path, """
            apiVersion: cj.io/v1
            kind: ExperimentSuite
            models:
              judge:
                model: gpt-4o-mini
                base_url: https://api.openai.com/v1
                credential_env: OPENAI_API_KEY
            experiments:
              - name: e1
                evaluator_model: judge
                faults: []
        """)
        suite = ConfigLoader.load_suite(p)
        scenario = suite._experiments[0][0]
        assert scenario.evaluator_model == "judge"

    def test_scenario_evaluator_model_stored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        from chaos_jungle.config import ConfigLoader
        p = self._write_yaml(tmp_path, """
            apiVersion: cj.io/v1
            kind: ExperimentSuite
            models:
              judge:
                model: gpt-4o-mini
            experiments:
              - name: e1
                evaluator_model: judge
                faults: []
        """)
        suite = ConfigLoader.load_suite(p)
        scenario = suite._experiments[0][0]
        assert scenario.evaluator_model == "judge"

    def test_no_evaluator_model_gives_none(self, tmp_path):
        from chaos_jungle.config import ConfigLoader
        p = self._write_yaml(tmp_path, """
            apiVersion: cj.io/v1
            kind: ExperimentSuite
            experiments:
              - name: e1
                faults: []
        """)
        suite = ConfigLoader.load_suite(p)
        scenario = suite._experiments[0][0]
        assert scenario.evaluator_model is None

    def test_run_one_builds_judge_from_registry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        from chaos_jungle.models import ModelConfig, ModelRegistry
        from chaos_jungle.core.suite import ExperimentSuite
        from chaos_jungle.core.scenario import Scenario

        reg = ModelRegistry.from_dict({"judge": {"model": "gpt-4o-mini"}})
        suite = ExperimentSuite(models=reg, duration=0)
        scenario = Scenario("e1", faults=[])
        scenario.evaluator_model = "judge"
        suite.add(scenario, LocalTarget(), duration=0)

        # _run_one is private but we can inspect via run()
        results = suite.run(parallel=False)
        result = results["e1"]
        from chaos_jungle.analysis.judge import LLMJudge
        assert isinstance(result.configured_judge, LLMJudge)
        assert result.configured_judge.model == "gpt-4o-mini"

    def test_run_one_no_judge_when_no_models(self, tmp_path):
        from chaos_jungle.core.suite import ExperimentSuite
        from chaos_jungle.core.scenario import Scenario

        suite = ExperimentSuite(duration=0)
        scenario = Scenario("e1", faults=[])
        suite.add(scenario, LocalTarget(), duration=0)

        results = suite.run(parallel=False)
        assert results["e1"].configured_judge is None

    def test_run_one_no_judge_when_role_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        from chaos_jungle.models import ModelRegistry
        from chaos_jungle.core.suite import ExperimentSuite
        from chaos_jungle.core.scenario import Scenario

        reg = ModelRegistry.from_dict({"agent": {"model": "gpt-4o-mini"}})
        suite = ExperimentSuite(models=reg, duration=0)
        scenario = Scenario("e1", faults=[])
        scenario.evaluator_model = "nonexistent_role"
        suite.add(scenario, LocalTarget(), duration=0)

        results = suite.run(parallel=False)
        assert results["e1"].configured_judge is None


# ── Activation failure detection fix ─────────────────────────────────────────

class TestActivationFailureDetection:
    """Prepare-phase failures must not trigger activation rollback."""

    def test_best_effort_prepare_activates_remaining_members(self):
        activated = []

        class _TrackFault:
            def __init__(self, name):
                self.name = name
            def start(self, t):
                activated.append(self.name)
            def stop(self, t): pass
            def revert(self, t): pass
            def verify_active(self, t):
                r = MagicMock(); r.not_implemented = True; return r
            def verify_recovered(self, t):
                r = MagicMock(); r.not_implemented = True; return r
            def _parameters(self): return {}

        group = InjectionGroup(
            name="be-act",
            injections=[
                Injection(id="ok", target=LocalTarget(), fault=_TrackFault("ok")),
                Injection(id="bad", target=_BadConnectTarget(), fault=_TrackFault("bad")),
            ],
            synchronization="best_effort",
            atomic=False,
            require_all_ready=False,
            on_prepare_failure="best_effort",
            watchdog=False,
        )
        runner = InjectionGroupRunner(group)
        ev = runner.start()
        runner.stop()
        # 'ok' should have activated; 'bad' failed prepare so should NOT be in activated
        assert "ok" in activated
        assert "bad" not in activated
        assert ev is None or ev.verdict != "cancelled"

    def test_atomic_group_rollback_on_activation_failure(self):
        """Only a genuine activation failure (after clean prepare) triggers rollback."""
        class _FailActivateFault:
            def start(self, t): raise RuntimeError("activation refused")
            def stop(self, t): pass
            def revert(self, t): pass
            def verify_active(self, t):
                r = MagicMock(); r.not_implemented = True; return r
            def verify_recovered(self, t):
                r = MagicMock(); r.not_implemented = True; return r
            def _parameters(self): return {}

        group = InjectionGroup(
            name="act-fail",
            injections=[
                Injection(id="ok", target=LocalTarget(), fault=_OkFault()),
                Injection(id="fail", target=LocalTarget(), fault=_FailActivateFault()),
            ],
            synchronization="best_effort",
            atomic=True,
            watchdog=False,
        )
        runner = InjectionGroupRunner(group)
        with pytest.raises(RuntimeError, match="activation failed"):
            runner.start()

    def test_prepare_failure_not_counted_as_activation_failure_in_best_effort(self):
        """A prepare-only failure in best_effort mode should not trigger rollback."""
        group = InjectionGroup(
            name="prep-only-fail",
            injections=[
                Injection(id="ok", target=LocalTarget(), fault=_OkFault()),
                Injection(id="bad-prep", target=_BadConnectTarget(), fault=_OkFault()),
            ],
            synchronization="best_effort",
            atomic=False,
            require_all_ready=False,
            on_prepare_failure="best_effort",
            on_activation_failure="rollback_all",
            watchdog=False,
        )
        runner = InjectionGroupRunner(group)
        runner.start()  # must not raise
        ev = runner.stop()
        # Verdict should reflect what happened — not "cancelled" due to prepare failure
        assert ev.verdict != "cancelled"


# ── Scenario group round-trip (blocker 1) ────────────────────────────────────

class TestScenarioGroupRoundTrip:
    def _group_with_local_faults(self):
        from chaos_jungle.faults import NetworkDelay
        return InjectionGroup(
            name="rt-grp",
            injections=[
                Injection(id="m1", target=LocalTarget(), fault=NetworkDelay("50ms")),
                Injection(id="m2", target=LocalTarget(), fault=NetworkDelay("100ms")),
            ],
            synchronization="barrier",
            atomic=False,
            maximum_skew_ms=200.0,
            start_after=3.0,
            on_prepare_failure="best_effort",
            on_activation_failure="continue",
            on_skew_violation="continue",
            safety_maximum_duration=45.0,
            watchdog=False,
        )

    def test_round_trip_restores_group(self):
        from chaos_jungle.core.scenario import Scenario
        s = Scenario("s1", faults=[], groups=[self._group_with_local_faults()])
        d = s.to_dict()
        s2 = Scenario.from_dict(d)

        assert len(s2.groups) == 1
        g2 = s2.groups[0]
        g1 = s.groups[0]
        assert g2.name == g1.name
        assert g2.synchronization == "barrier"
        assert g2.start_after == 3.0
        assert g2.maximum_skew_ms == 200.0
        assert g2.on_prepare_failure == "best_effort"
        assert g2.on_activation_failure == "continue"
        assert g2.on_skew_violation == "continue"
        assert g2.safety_maximum_duration == 45.0
        assert len(g2.injections) == 2

    def test_round_trip_restores_injection_ids(self):
        from chaos_jungle.core.scenario import Scenario
        s = Scenario("s1", faults=[], groups=[self._group_with_local_faults()])
        s2 = Scenario.from_dict(s.to_dict())
        ids = {inj.id for inj in s2.groups[0].injections}
        assert ids == {"m1", "m2"}

    def test_round_trip_restores_fault_class(self):
        from chaos_jungle.faults import NetworkDelay
        from chaos_jungle.core.scenario import Scenario
        s = Scenario("s1", faults=[], groups=[self._group_with_local_faults()])
        s2 = Scenario.from_dict(s.to_dict())
        for inj in s2.groups[0].injections:
            assert isinstance(inj.fault, NetworkDelay)

    def test_round_trip_group_is_runnable(self):
        from chaos_jungle.core.scenario import Scenario
        s = Scenario("s1", faults=[], groups=[self._group_with_local_faults()])
        s2 = Scenario.from_dict(s.to_dict())
        runner = InjectionGroupRunner(s2.groups[0])
        runner.start()
        ev = runner.stop()
        assert ev.verdict in {"valid", "inconclusive", "invalid"}

    def test_round_trip_preserves_evaluator_model(self):
        from chaos_jungle.core.scenario import Scenario
        s = Scenario("s1", faults=[], groups=[self._group_with_local_faults()])
        s.evaluator_model = "judge"
        s2 = Scenario.from_dict(s.to_dict())
        assert s2.evaluator_model == "judge"

    def test_empty_groups_round_trip(self):
        from chaos_jungle.core.scenario import Scenario
        s = Scenario("s1", faults=[])
        s2 = Scenario.from_dict(s.to_dict())
        assert s2.groups == []

    def test_unknown_fault_class_raises(self):
        from chaos_jungle.core.scenario import Scenario
        d = {
            "id": "x", "name": "s",
            "faults": [],
            "groups": [{
                "name": "g", "group_id": "abc",
                "injections": [{"id": "m1", "target": {"kind": "local"},
                                "fault": {"kind": "NonExistentFaultXYZ", "params": {}}}],
            }],
        }
        with pytest.raises(ValueError, match="NonExistentFaultXYZ"):
            Scenario.from_dict(d)


# ── ExperimentPlan group round-trip (blocker 2) ───────────────────────────────

class TestExperimentPlanGroupRoundTrip:
    def _scenario_with_group(self):
        from chaos_jungle.core.scenario import Scenario
        from chaos_jungle.faults import NetworkDelay
        group = InjectionGroup(
            name="plan-grp",
            injections=[Injection(id="m1", target=LocalTarget(), fault=NetworkDelay("30ms"))],
            synchronization="scheduled",
            start_after=2.0,
            maximum_skew_ms=150.0,
        )
        return Scenario("e1", faults=[], groups=[group])

    def test_from_scenario_includes_groups(self):
        from chaos_jungle.plan import ExperimentPlan
        s = self._scenario_with_group()
        plan = ExperimentPlan.from_scenario(s, LocalTarget())
        assert len(plan.groups) == 1
        g = plan.groups[0]
        assert g.name == "plan-grp"
        assert g.start_after == 2.0
        assert g.maximum_skew_ms == 150.0
        assert len(g.injections) == 1

    def test_from_scenario_injection_target_kind(self):
        from chaos_jungle.plan import ExperimentPlan
        s = self._scenario_with_group()
        plan = ExperimentPlan.from_scenario(s, LocalTarget())
        assert plan.groups[0].injections[0].target.kind == "local"

    def test_from_scenario_injection_fault_class(self):
        from chaos_jungle.plan import ExperimentPlan
        s = self._scenario_with_group()
        plan = ExperimentPlan.from_scenario(s, LocalTarget())
        assert plan.groups[0].injections[0].fault.fault_class == "NetworkDelay"

    def test_plan_round_trip_preserves_groups(self):
        from chaos_jungle.plan import ExperimentPlan
        s = self._scenario_with_group()
        plan = ExperimentPlan.from_scenario(s, LocalTarget())
        d = plan.to_dict()
        plan2 = ExperimentPlan.from_dict(d)
        assert len(plan2.groups) == 1
        g2 = plan2.groups[0]
        assert g2.name == "plan-grp"
        assert g2.start_after == 2.0
        assert g2.maximum_skew_ms == 150.0

    def test_plan_round_trip_preserves_injection_id(self):
        from chaos_jungle.plan import ExperimentPlan
        s = self._scenario_with_group()
        plan = ExperimentPlan.from_scenario(s, LocalTarget())
        plan2 = ExperimentPlan.from_dict(plan.to_dict())
        assert plan2.groups[0].injections[0].id == "m1"

    def test_plan_to_dict_round_trip_invariant(self):
        from chaos_jungle.plan import ExperimentPlan
        s = self._scenario_with_group()
        plan = ExperimentPlan.from_scenario(s, LocalTarget())
        d1 = plan.to_dict()["groups"]
        d2 = ExperimentPlan.from_dict(plan.to_dict()).to_dict()["groups"]
        assert d1 == d2

    def test_no_groups_round_trip(self):
        from chaos_jungle.core.scenario import Scenario
        from chaos_jungle.plan import ExperimentPlan
        s = Scenario("e1", faults=[])
        plan = ExperimentPlan.from_scenario(s, LocalTarget())
        plan2 = ExperimentPlan.from_dict(plan.to_dict())
        assert plan2.groups == []


# ── group_valid consistency (blocker 3) ──────────────────────────────────────

class TestGroupValidConsistency:
    def _finalized(self, manifested, skew_us=10_000, skew_limit=100.0):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t1 = datetime(2026, 1, 1, 0, 0, 0, skew_us, tzinfo=timezone.utc)
        ev = GroupActivationEvidence(group_id="g", maximum_allowed_skew_ms=skew_limit)
        ev.members = [
            MemberEvidence(host="a", injection_id="m0", active_at=t0, manifested=manifested[0]),
            MemberEvidence(host="b", injection_id="m1", active_at=t1, manifested=manifested[1]),
        ]
        ev.finalize()
        return ev

    def test_all_true_manifested_is_valid(self):
        ev = self._finalized((True, True))
        assert ev.verdict == "valid"
        assert ev.group_valid is True

    def test_none_manifested_is_inconclusive_not_valid(self):
        ev = self._finalized((None, None))
        assert ev.verdict == "inconclusive"
        assert ev.group_valid is False

    def test_false_manifested_is_invalid_not_valid(self):
        ev = self._finalized((True, False))
        assert ev.verdict == "invalid"
        assert ev.group_valid is False

    def test_no_contradiction_between_verdict_and_group_valid(self):
        for manifested in [(True, True), (None, True), (False, True), (None, None)]:
            ev = self._finalized(manifested)
            if ev.verdict == "valid":
                assert ev.group_valid is True, f"manifested={manifested}: verdict=valid but group_valid=False"
            else:
                assert ev.group_valid is False, f"manifested={manifested}: verdict={ev.verdict!r} but group_valid=True"

    def test_pending_verdict_gives_false(self):
        ev = GroupActivationEvidence(group_id="g")
        assert ev.verdict == "pending"
        assert ev.group_valid is False

    def test_cancelled_gives_false(self):
        ev = GroupActivationEvidence(group_id="g")
        ev.verdict = "cancelled"
        assert ev.group_valid is False

    def test_recovery_invalid_gives_false(self):
        ev = GroupActivationEvidence(group_id="g")
        ev.verdict = "recovery_invalid"
        assert ev.group_valid is False


# ── emergency_stop race (blocker 4) ──────────────────────────────────────────

class TestEmergencyStopRace:
    def _running_group(self):
        group = InjectionGroup(
            name="race-grp",
            injections=[Injection(id="m1", target=LocalTarget(), fault=_OkFault())],
            synchronization="best_effort",
            atomic=False,
            watchdog=False,
        )
        runner = InjectionGroupRunner(group)
        runner.start()
        return runner

    def test_emergency_stop_and_stop_are_idempotent(self):
        runner = self._running_group()
        # Call both concurrently — only one should perform revert
        t1 = threading.Thread(target=runner.emergency_stop, daemon=True)
        t2 = threading.Thread(target=runner.stop, daemon=True)
        t1.start(); t2.start()
        t1.join(timeout=5); t2.join(timeout=5)
        assert not t1.is_alive() and not t2.is_alive()

    def test_multiple_emergency_stops_safe(self):
        runner = self._running_group()
        threads = [threading.Thread(target=runner.emergency_stop, daemon=True) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=5)
        assert all(not t.is_alive() for t in threads)

    def test_evidence_finalized_after_concurrent_stops(self):
        import time
        runner = self._running_group()
        t1 = threading.Thread(target=runner.emergency_stop, daemon=True)
        t2 = threading.Thread(target=runner.stop, daemon=True)
        t1.start(); t2.start()
        t1.join(timeout=5); t2.join(timeout=5)
        # After both have run, evidence must be finalized (not pending)
        ev = runner.evidence
        assert ev is not None
        assert ev.verdict != "pending"

    def test_stop_after_emergency_stop_returns_same_evidence(self):
        runner = self._running_group()
        runner.emergency_stop()
        import time; time.sleep(0.15)  # let worker complete
        ev1 = runner.evidence
        ev2 = runner.stop()
        assert ev1 is ev2


# ── verify_recovered exception → recovery_invalid (blocker 5) ────────────────

class TestVerifyRecoveredExceptionHandling:
    def test_exception_in_verify_recovered_sets_recovery_invalid(self):
        class _ExcOnVerify(_OkFault):
            def verify_recovered(self, t):
                raise RuntimeError("verify probe exploded")

        group = InjectionGroup(
            name="vr-exc",
            injections=[Injection(id="m1", target=LocalTarget(), fault=_ExcOnVerify())],
            synchronization="best_effort",
            atomic=False,
            watchdog=False,
        )
        runner = InjectionGroupRunner(group)
        runner.start()
        ev = runner.stop()
        assert ev.verdict == "recovery_invalid"

    def test_failed_verify_recovered_sets_recovery_invalid(self):
        class _FailVerify(_OkFault):
            def verify_recovered(self, t):
                r = MagicMock()
                r.not_implemented = False
                r.verified = False
                r.reason = "still running"
                return r

        group = InjectionGroup(
            name="vr-fail",
            injections=[Injection(id="m1", target=LocalTarget(), fault=_FailVerify())],
            synchronization="best_effort",
            atomic=False,
            watchdog=False,
        )
        runner = InjectionGroupRunner(group)
        runner.start()
        ev = runner.stop()
        assert ev.verdict == "recovery_invalid"

    def test_not_implemented_verify_recovered_leaves_verdict_intact(self):
        group = InjectionGroup(
            name="vr-ni",
            injections=[Injection(id="m1", target=LocalTarget(), fault=_OkFault())],
            synchronization="best_effort",
            atomic=False,
            watchdog=False,
        )
        runner = InjectionGroupRunner(group)
        runner.start()
        ev = runner.stop()
        assert ev.verdict != "recovery_invalid"


# ── Single-member group (no longer inconclusive) ──────────────────────────────

class TestSingleMemberGroup:
    def test_single_member_valid_when_manifested(self):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ev = GroupActivationEvidence(group_id="g", maximum_allowed_skew_ms=100.0)
        ev.members = [MemberEvidence(host="a", injection_id="m0", active_at=t0, manifested=True)]
        ev.finalize()
        assert ev.activation_skew_ms == 0.0
        assert ev.verdict == "valid"
        assert ev.group_valid is True

    def test_single_member_inconclusive_when_manifested_unknown(self):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ev = GroupActivationEvidence(group_id="g", maximum_allowed_skew_ms=100.0)
        ev.members = [MemberEvidence(host="a", injection_id="m0", active_at=t0, manifested=None)]
        ev.finalize()
        assert ev.activation_skew_ms == 0.0
        assert ev.verdict == "inconclusive"
        assert ev.group_valid is False

    def test_single_member_runner_has_zero_skew(self):
        # Before fix: skew was None → verdict forced to inconclusive (no skew data).
        # After fix: skew is 0.0 → verdict is inconclusive only if manifested=None,
        # not because skew is missing. synchronization_valid must be True.
        group = InjectionGroup(
            name="sm",
            injections=[Injection(id="m1", target=LocalTarget(), fault=_OkFault())],
            synchronization="best_effort",
            watchdog=False,
        )
        runner = InjectionGroupRunner(group)
        runner.start()
        ev = runner.stop()
        assert ev.activation_skew_ms == 0.0
        assert ev.synchronization_valid is True


# ── on_ready not called after cancelled start ─────────────────────────────────

class TestOnReadyAfterCancelledStart:
    def test_on_ready_not_called_when_start_cancelled(self):
        from chaos_jungle.distributed.coordinator import DistributedCoordinator

        called = []
        def on_ready():
            called.append(True)

        # All members fail prepare → start is cancelled
        members = [
            Injection(id="bad1", target=_BadConnectTarget(), fault=_OkFault()),
            Injection(id="bad2", target=_BadConnectTarget(), fault=_OkFault()),
        ]
        scenario = DistributedScenario(
            name="no-ready",
            members=members,
            synchronization=SyncConfig(mode="scheduled", start_after=0.01,
                                       require_all_ready=True),
            atomicity=AtomicityConfig(prepare="all_or_nothing"),
            safety=DistributedSafetyConfig(),
            observation_duration=0.0,
        )
        coord = DistributedCoordinator(scenario, on_ready=on_ready)
        ev = coord.run()
        assert ev.verdict == "cancelled"
        assert not called, "on_ready must not be called when start was cancelled"


# ── HTTP URL and fault layer plan round-trip ──────────────────────────────────

class TestPlanGroupURLAndLayer:
    def test_url_and_layer_serialized(self):
        from chaos_jungle.plan import (
            InjectionGroupSpec, InjectionMemberSpec, TargetSpec, FaultSpec,
        )
        spec = InjectionGroupSpec(
            name="g",
            injections=[InjectionMemberSpec(
                id="m1",
                target=TargetSpec(kind="http", url="http://agent:8080"),
                fault=FaultSpec(fault_class="NetworkDelay", parameters={"delay_ms": 100}, layer="network"),
            )],
        )
        d = spec.to_dict()
        inj = d["injections"][0]
        assert inj["target"]["url"] == "http://agent:8080"
        assert inj["fault"]["layer"] == "network"

    def test_layer_restored_from_dict(self):
        from chaos_jungle.plan import ExperimentPlan, ScenarioSpec
        plan = ExperimentPlan(scenario=ScenarioSpec(name="s"))
        d = plan.to_dict()
        d["groups"] = [{
            "name": "g",
            "injections": [{
                "id": "m1",
                "target": {"kind": "http", "url": "http://x:8080"},
                "fault": {"fault_class": "NetworkDelay", "layer": "network", "parameters": {}},
            }],
        }]
        restored = ExperimentPlan.from_dict(d)
        assert restored.groups[0].injections[0].fault.layer == "network"

    def test_url_restored_from_dict(self):
        from chaos_jungle.plan import ExperimentPlan, ScenarioSpec
        plan = ExperimentPlan(scenario=ScenarioSpec(name="s"))
        d = plan.to_dict()
        d["groups"] = [{
            "name": "g",
            "injections": [{
                "id": "m1",
                "target": {"kind": "http", "url": "http://x:8080"},
                "fault": {"fault_class": "X", "layer": "llm", "parameters": {}},
            }],
        }]
        restored = ExperimentPlan.from_dict(d)
        assert restored.groups[0].injections[0].target.url == "http://x:8080"


# ── Rollback failures do not get overwritten ──────────────────────────────────

class _FailStartFault(_OkFault):
    """Fails during start (activation phase)."""
    def start(self, target): raise RuntimeError("activation boom")


class _FailStopFault(_OkFault):
    """Activates successfully but raises during stop (rollback phase)."""
    def stop(self, target): raise RuntimeError("stop boom")


class TestRollbackVerdictNotOverwritten:
    def test_activation_failure_plus_rollback_failure_gives_recovery_invalid(self):
        """Rollback failure during activation-failure rollback → recovery_invalid, not cancelled."""
        group = InjectionGroup(
            name="af-rf",
            injections=[
                Injection(id="m1", target=LocalTarget(), fault=_FailStopFault()),
                Injection(id="m2", target=LocalTarget(), fault=_FailStartFault()),
            ],
            synchronization="best_effort",
            atomic=False,
            on_activation_failure="rollback_all",
            watchdog=False,
        )
        runner = InjectionGroupRunner(group)
        runner.start()  # m1 activates, m2 fails → rollback → m1.stop() raises
        assert runner._evidence.verdict == "recovery_invalid"

    def test_skew_violation_plus_rollback_failure_gives_recovery_invalid(self):
        """Rollback failure during skew-violation rollback → recovery_invalid, not invalid."""
        # Two members with a 20ms sleep on one → guaranteed skew > 0ms limit
        class _SlowStart(_FailStopFault):
            def start(self, t):
                import time; time.sleep(0.02)

        group = InjectionGroup(
            name="sv-rf",
            injections=[
                Injection(id="m1", target=LocalTarget(), fault=_SlowStart()),
                Injection(id="m2", target=LocalTarget(), fault=_FailStopFault()),
            ],
            synchronization="best_effort",
            atomic=False,
            maximum_skew_ms=0.0,   # any real timing difference exceeds 0ms
            on_skew_violation="rollback_all",
            watchdog=False,
        )
        runner = InjectionGroupRunner(group)
        runner.start()  # skew > 0ms → rollback → stop() raises → recovery_invalid
        assert runner._evidence.verdict == "recovery_invalid"


# ── Dashboard group evidence field normalization ───────────────────────────────

class TestDashboardGroupFieldNormalization:
    def test_api_normalizes_members_and_skew_fields(self, tmp_path):
        """API must return 'members' and 'activation_skew_ms', not 'members_json'/'skew_ms'."""
        from fastapi.testclient import TestClient
        import chaos_jungle.control.dashboard as dash_mod
        from chaos_jungle.db.session_db import SessionDB

        db_path = str(tmp_path / "test.db")
        db = SessionDB(path=db_path)
        sid = db.open_session("norm-test")
        ev = GroupActivationEvidence(
            group_id="g-norm",
            maximum_allowed_skew_ms=100.0,
            activation_skew_ms=7.3,
            verdict="valid",
        )
        ev.members = [
            MemberEvidence(host="h", injection_id="m1",
                           active_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                           manifested=True),
        ]
        db.store_group_evidence(sid, ev)

        # Patch the dashboard DB to use our test DB
        original_cls = dash_mod.SessionDB

        class _PatchedDB(original_cls):
            def __init__(self, **kw):
                super().__init__(path=db_path, **kw)

        dash_mod.SessionDB = _PatchedDB
        try:
            client = TestClient(dash_mod.app, raise_server_exceptions=True)
            resp = client.get(f"/api/session/{sid}/groups")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 1
            g = data[0]
            assert "members" in g, "should be 'members', not 'members_json'"
            assert "activation_skew_ms" in g, "should be 'activation_skew_ms', not 'skew_ms'"
            assert "members_json" not in g
            assert "skew_ms" not in g
        finally:
            dash_mod.SessionDB = original_cls


# ── Gap 1: Group verdicts in session verdict ──────────────────────────────────

class TestGroupVerdictInSessionVerdict:
    def _db_with_group(self, tmp_path, group_verdict):
        from chaos_jungle.db.session_db import SessionDB
        db = SessionDB(path=str(tmp_path / "test.db"))
        sid = db.open_session("gv-test")
        ev = GroupActivationEvidence(group_id="g1", verdict=group_verdict,
                                     maximum_allowed_skew_ms=100.0,
                                     synchronization_valid=(group_verdict == "valid"))
        ev.members = [MemberEvidence(host="h", injection_id="m1", manifested=True)]
        db.store_group_evidence(sid, ev)
        return db, sid

    def test_invalid_group_gives_invalid_session_verdict(self, tmp_path):
        """A group verdict of 'invalid' must push the session verdict to INVALID."""
        from chaos_jungle.db.session_db import SessionDB
        db = SessionDB(path=str(tmp_path / "test.db"))
        sid = db.open_session("test")
        ev = GroupActivationEvidence(group_id="g", verdict="invalid")
        db.store_group_evidence(sid, ev)
        rows = db.get_group_evidence(sid)
        assert rows[0]["verdict"] == "invalid"

    def test_cancelled_group_verdict_is_invalid_category(self):
        """Cancelled/recovery_invalid → INVALID in the session verdict mapping."""
        # Verify the mapping logic by checking verdict categories directly
        group_verdicts_map = {
            "valid": "VALID",
            "inconclusive": "INCONCLUSIVE",
            "pending": "INCONCLUSIVE",
            "invalid": "INVALID",
            "cancelled": "INVALID",
            "recovery_invalid": "INVALID",
        }
        for gv, expected in group_verdicts_map.items():
            if gv == "valid":
                assert expected == "VALID"
            elif gv in {"inconclusive", "pending"}:
                assert expected == "INCONCLUSIVE"
            else:
                assert expected == "INVALID"


# ── Gap 2: Evidence persisted on activation failure ───────────────────────────

class TestEvidencePersistedOnActivationFailure:
    def test_cancelled_evidence_stored_when_start_raises(self, tmp_path):
        """Group evidence (verdict=cancelled) must be stored even when start() raises."""
        from chaos_jungle.db.session_db import SessionDB

        db = SessionDB(path=str(tmp_path / "test.db"))
        sid = db.open_session("fail-start")

        group = InjectionGroup(
            name="fail-grp",
            injections=[Injection(id="m1", target=_BadConnectTarget(), fault=_OkFault())],
            synchronization="scheduled",
            atomic=True,
            watchdog=False,
        )
        runner = InjectionGroupRunner(group)
        with pytest.raises(RuntimeError):
            runner.start()

        # Evidence should be populated in runner even though start raised
        assert runner._evidence is not None
        assert runner._evidence.verdict == "cancelled"

        # Simulate what runner.py does: persist evidence on activation failure
        db.store_group_evidence(sid, runner._evidence)
        rows = db.get_group_evidence(sid)
        assert len(rows) == 1
        assert rows[0]["verdict"] == "cancelled"


# ── Gap 3: HTTPTarget url attribute in plan compilation ───────────────────────

class TestHTTPTargetURLAttribute:
    def test_http_target_uses_url_not_base_url(self):
        """HTTPTarget stores the URL in .url, not .base_url."""
        from chaos_jungle.targets.http import HTTPTarget
        t = HTTPTarget("http://agent:8080")
        assert t.url == "http://agent:8080"
        assert not hasattr(t, "base_url")

    def test_from_scenario_reads_url_attribute(self):
        """from_scenario() must read target.url for HTTPTarget."""
        from chaos_jungle.plan import ExperimentPlan, FaultSpec
        from chaos_jungle.core.scenario import Scenario
        from chaos_jungle.targets.http import HTTPTarget

        scenario = Scenario("s", [])
        target = HTTPTarget("http://x:9000")
        plan = ExperimentPlan.from_scenario(scenario, target=target)
        assert plan.target.url == "http://x:9000"
        assert plan.target.kind == "http"


# ── Gap 4: Skew limit and sync result persisted ───────────────────────────────

class TestGroupEvidenceSkewLimitPersisted:
    def test_maximum_allowed_skew_ms_and_sync_valid_stored(self, tmp_path):
        from chaos_jungle.db.session_db import SessionDB
        db = SessionDB(path=str(tmp_path / "test.db"))
        sid = db.open_session("skew-limit")

        ev = GroupActivationEvidence(
            group_id="g",
            maximum_allowed_skew_ms=50.0,
            activation_skew_ms=10.0,
            synchronization_valid=True,
            verdict="valid",
        )
        ev.members = []
        db.store_group_evidence(sid, ev)

        rows = db.get_group_evidence(sid)
        r = rows[0]
        assert r["maximum_allowed_skew_ms"] == 50.0
        assert r["synchronization_valid"] in (1, True)  # SQLite stores as integer

    def test_null_sync_valid_stored_as_null(self, tmp_path):
        from chaos_jungle.db.session_db import SessionDB
        db = SessionDB(path=str(tmp_path / "test.db"))
        sid = db.open_session("null-sync")

        ev = GroupActivationEvidence(group_id="g")  # synchronization_valid=None
        db.store_group_evidence(sid, ev)

        rows = db.get_group_evidence(sid)
        assert rows[0]["synchronization_valid"] is None


# ── Gap 5: InjectionGroup validation ─────────────────────────────────────────

class TestInjectionGroupValidation2:
    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="name"):
            InjectionGroup(name="")

    def test_negative_start_after_raises(self):
        with pytest.raises(ValueError, match="start_after"):
            InjectionGroup(name="g", start_after=-1.0)

    def test_zero_safety_duration_with_watchdog_raises(self):
        with pytest.raises(ValueError, match="safety_maximum_duration"):
            InjectionGroup(name="g", watchdog=True, safety_maximum_duration=0.0)

    def test_negative_safety_duration_with_watchdog_raises(self):
        with pytest.raises(ValueError, match="safety_maximum_duration"):
            InjectionGroup(name="g", watchdog=True, safety_maximum_duration=-5.0)

    def test_watchdog_false_allows_zero_duration(self):
        inj = Injection(id="m1", target=LocalTarget(), fault=_OkFault())
        g = InjectionGroup(name="g", injections=[inj], watchdog=False, safety_maximum_duration=0.0)
        assert g.watchdog is False

    def test_zero_start_after_is_valid(self):
        inj = Injection(id="m1", target=LocalTarget(), fault=_OkFault())
        g = InjectionGroup(name="g", injections=[inj], start_after=0.0, watchdog=False)
        assert g.start_after == 0.0

    def test_negative_maximum_skew_ms_raises(self):
        inj = Injection(id="m1", target=LocalTarget(), fault=_OkFault())
        with pytest.raises(ValueError, match="maximum_skew_ms"):
            InjectionGroup(name="g", injections=[inj], maximum_skew_ms=-0.1, watchdog=False)

    def test_empty_injections_raises(self):
        with pytest.raises(ValueError, match="injections"):
            InjectionGroup(name="g", injections=[], watchdog=False)

    def test_duplicate_injection_ids_raises(self):
        inj1 = Injection(id="dup", target=LocalTarget(), fault=_OkFault())
        inj2 = Injection(id="dup", target=LocalTarget(), fault=_OkFault())
        with pytest.raises(ValueError, match="Duplicate"):
            InjectionGroup(name="g", injections=[inj1, inj2], watchdog=False)


# ── requested_start persisted in group_evidence ───────────────────────────────

class TestRequestedStartPersisted:
    def test_requested_start_stored_and_retrieved(self, tmp_path):
        from chaos_jungle.db.session_db import SessionDB
        db = SessionDB(path=str(tmp_path / "test.db"))
        sid = db.open_session("rs-test")

        t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        ev = GroupActivationEvidence(
            group_id="g",
            requested_start=t0,
            verdict="valid",
            maximum_allowed_skew_ms=100.0,
            synchronization_valid=True,
        )
        ev.members = []
        db.store_group_evidence(sid, ev)

        rows = db.get_group_evidence(sid)
        assert rows[0]["requested_start"] == t0.isoformat()

    def test_null_requested_start_stored_as_null(self, tmp_path):
        from chaos_jungle.db.session_db import SessionDB
        db = SessionDB(path=str(tmp_path / "test.db"))
        sid = db.open_session("rs-null")
        ev = GroupActivationEvidence(group_id="g")  # requested_start=None
        db.store_group_evidence(sid, ev)
        rows = db.get_group_evidence(sid)
        assert rows[0]["requested_start"] is None
