"""Tests for the refactoring items:
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
        assert isinstance(result.judge, LLMJudge)
        assert result.judge.model == "gpt-4o-mini"

    def test_run_one_no_judge_when_no_models(self, tmp_path):
        from chaos_jungle.core.suite import ExperimentSuite
        from chaos_jungle.core.scenario import Scenario

        suite = ExperimentSuite(duration=0)
        scenario = Scenario("e1", faults=[])
        suite.add(scenario, LocalTarget(), duration=0)

        results = suite.run(parallel=False)
        assert results["e1"].judge is None

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
        assert results["e1"].judge is None


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

    def test_prepare_failure_not_counted_as_activation_failure(self):
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
