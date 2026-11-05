"""Package-level exposure verdict and target provenance regression tests."""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from chaos_jungle import DockerTarget as PublicDockerTarget
from chaos_jungle.core.runner import ChaosRunner, _target_info
from chaos_jungle.core.scenario import Scenario
from chaos_jungle.db.session_db import SessionDB
from chaos_jungle.distributed.evidence import GroupActivationEvidence, MemberEvidence
from chaos_jungle.distributed.scenario import Injection
from chaos_jungle.exposure import classify_fault_exposure
from chaos_jungle.faults.base import Fault, VerificationResult
from chaos_jungle.faults.resources import CPUStress
from chaos_jungle.inject.group import InjectionGroup
from chaos_jungle.plan import ExperimentPlan
from chaos_jungle.scripts.llm_proxy import llm_proxy
from chaos_jungle.targets.docker import DockerContainerControllerTarget, DockerTarget
from chaos_jungle.targets.local import LocalTarget


class VerifiedFault(Fault):
    def start(self, target):
        pass

    def stop(self, target):
        pass

    def revert(self, target):
        pass

    def verify_active(self, target):
        return VerificationResult(True, "active")

    def verify_recovered(self, target):
        return VerificationResult(True, "recovered")


def _fault_row(**changes):
    row = {"id": 1, "status": "reverted", "verified_active": 1,
           "verified_recovered": 1, "parameters": '{"fault":"latency"}'}
    row.update(changes)
    return row


def _call(*, matched=True, triggered=True, manifested=True):
    return {"fault_evidence_json": json.dumps([{
        "fault_type": "latency", "target_matched": matched,
        "triggered": triggered, "manifested": manifested,
    }])}


def test_no_traffic_is_inconclusive_not_untriggered():
    result = classify_fault_exposure(_fault_row(), proxy_calls=[])
    assert result.verdict == "inconclusive"
    assert result.activated is True


def test_target_effect_without_measurement_is_inconclusive():
    result = classify_fault_exposure(
        _fault_row(parameters="{}"),
        observation={"triggered": True, "manifested": True},
        policy_compliant=True,
    )
    assert result.verdict == "inconclusive"
    assert "supporting evidence" in result.reason


def test_policy_and_recovery_are_separate_from_effect():
    observation = {"triggered": True, "manifested": True,
                   "evidence": {"observed_status": 503}}
    recovered_later = classify_fault_exposure(
        _fault_row(status="revert_failed"), observation=observation,
        policy_compliant=True,
    )
    assert recovered_later.verdict == "valid"
    assert recovered_later.recovered is False
    policy_failure = classify_fault_exposure(
        _fault_row(), observation=observation, policy_compliant=False,
    )
    assert policy_failure.verdict == "invalid"


def test_unknown_policy_is_not_valid_exposure():
    result = classify_fault_exposure(_fault_row(), proxy_calls=[_call()])
    assert result.verdict == "inconclusive"
    assert result.policy_compliant is None
    assert "policy" in result.reason


@pytest.mark.parametrize("call,expected", [
    (_call(matched=False, triggered=False, manifested=False), "untriggered"),
    (_call(), "valid"),
    (_call(manifested=False), "invalid"),
])
def test_proxy_exposure_states(call, expected):
    assert classify_fault_exposure(
        _fault_row(), proxy_calls=[call], policy_compliant=True,
    ).verdict == expected


def test_explicit_reachability_reason_and_failed_activation():
    unreachable = classify_fault_exposure(
        _fault_row(parameters="{}"), observation={
            "triggered": False, "event_reached": False,
            "untriggered_reason": "workflow_stage_not_reached",
            "evidence": {"trace_id": "run-1"},
        }, policy_compliant=True,
    )
    assert unreachable.verdict == "untriggered"
    assert unreachable.untriggered_reason == "workflow_stage_not_reached"
    mismatch = classify_fault_exposure(
        _fault_row(parameters="{}"), observation={
            "triggered": False, "event_reached": False,
            "untriggered_reason": "selector_mismatch",
            "evidence": {"matched_calls": 0, "role_headers_seen": 2},
        }, policy_compliant=True,
    )
    assert mismatch.untriggered_reason == "selector_mismatch"
    failed = classify_fault_exposure(_fault_row(status="injection_failed"))
    assert failed.verdict == "invalid"


def test_proxy_distinguishes_selector_from_operation_reachability():
    config = {"fault": "tool_fault", "selector": {"agent_role": "coder"}}
    request = {"messages": [{"role": "user", "content": "write code"}]}
    no_tool = llm_proxy._build_lifecycle_chain(
        [config], [], {}, 0, request, {"agent_role": "coder"},
    )[0]
    assert no_tool["selector_matched"] is True
    assert no_tool["operation_reached"] is False
    assert no_tool["target_matched"] is False
    untriggered = classify_fault_exposure(
        _fault_row(parameters='{"fault":"tool_fault"}'),
        proxy_calls=[{"fault_evidence_json": json.dumps([no_tool])}],
        policy_compliant=True,
    )
    assert untriggered.verdict == "untriggered"
    assert untriggered.untriggered_reason == "operation_not_reached"

    other_role = llm_proxy._build_lifecycle_chain(
        [config], [], {}, 1, request, {"agent_role": "planner"},
    )[0]
    assert other_role["selector_matched"] is False
    untriggered = classify_fault_exposure(
        _fault_row(parameters='{"fault":"tool_fault"}'),
        proxy_calls=[{"fault_evidence_json": json.dumps([other_role])}],
        policy_compliant=True,
    )
    assert untriggered.untriggered_reason == "selected_request_not_observed"


def test_runner_preserves_old_verdict_and_writes_new_one(tmp_path):
    db = SessionDB(str(tmp_path / "sessions.db"))
    fault = VerifiedFault()
    runner = ChaosRunner(Scenario("exposure", [fault]), LocalTarget(), db=db,
                         auto_preflight=False)
    runner.start()
    runner.stop()
    session = db.get_session(runner._session_id)
    assert session["verdict"] == "VALID"
    assert runner.exposure_report()["verdict"] == "inconclusive"

    runner2 = ChaosRunner(Scenario("observed", [VerifiedFault()]), LocalTarget(),
                          db=db, auto_preflight=False)
    runner2.start()
    runner2.record_exposure(runner2.scenario.faults[0], triggered=True,
                            manifested=True, evidence={"measured_delay_s": 3.01})
    runner2.stop()
    report = runner2.exposure_report()
    assert report["schema_version"] == 1
    assert report["verdict"] == "valid"
    assert report["faults"][0]["evidence_source"] == "target_observation"


def test_proxy_counts_use_all_rows_and_duplicate_fault_types_fail_closed(tmp_path):
    db = SessionDB(str(tmp_path / "sessions.db"))
    sid = db.open_session("proxy")
    fid = db.record_fault(sid, "LLMLatency", {"fault": "latency"})
    db.update_fault_verification(fid, verified_active=True, verified_recovered=True)
    for index in range(25):
        db._conn.execute(
            "INSERT INTO llm_calls(session_id, call_index, timestamp, fault_evidence_json) "
            "VALUES(?,?,?,?)",
            (sid, index, "now", _call()["fault_evidence_json"]),
        )
    db._conn.commit()
    assert db.classify_session_exposure(sid)["verdict"] == "inconclusive"
    report = db.classify_session_exposure(sid, policy_compliant=True)
    assert report["verdict"] == "valid"
    assert report["faults"][0]["affected_calls"] == 25
    assert len(report["faults"][0]["proxy_call_ids_preview"]) == 20
    db.record_fault(sid, "LLMLatency", {"fault": "latency"})
    assert db.classify_session_exposure(sid, policy_compliant=True)["verdict"] == "inconclusive"


def test_group_effect_requires_member_observations(tmp_path):
    db = SessionDB(str(tmp_path / "sessions.db"))
    sid = db.open_session("group")
    now = datetime.now(timezone.utc)
    group = GroupActivationEvidence(group_id="g", synchronization_valid=True,
                                     verdict="valid")
    group.members = [MemberEvidence(host="docker", injection_id="coder",
                                    active_at=now, manifested=True, reverted_at=now)]
    db.store_group_evidence(sid, group)
    assert db.classify_session_exposure(sid, policy_compliant=True)["verdict"] == "inconclusive"
    group.members[0].triggered = True
    group.members[0].effect_manifested = True
    group.members[0].effect_evidence = {"cpu_percent": 82.0}
    sid2 = db.open_session("group-effect")
    db.store_group_evidence(sid2, group)
    report = db.classify_session_exposure(sid2, policy_compliant=True)
    assert report["verdict"] == "valid"
    assert report["groups"][0]["members"][0]["manifested"] is True

    group.synchronization_valid = None
    sid3 = db.open_session("unknown-sync")
    db.store_group_evidence(sid3, group)
    assert db.classify_session_exposure(sid3, policy_compliant=True)["verdict"] == "inconclusive"

    group.synchronization_valid = True
    group.verdict = "recovery_invalid"
    sid4 = db.open_session("failed-recovery")
    db.store_group_evidence(sid4, group)
    assert db.classify_session_exposure(sid4, policy_compliant=True)["verdict"] == "invalid"


def test_runner_records_group_member_exposure(tmp_path):
    db = SessionDB(str(tmp_path / "groups.db"))
    group = InjectionGroup(
        name="g", injections=[Injection("member", LocalTarget(), VerifiedFault())],
        synchronization="best_effort", start_after=0, watchdog=False,
        maximum_skew_ms=1000,
    )
    runner = ChaosRunner(Scenario("group-exposure", groups=[group]), LocalTarget(),
                         db=db, auto_preflight=False)
    runner.start()
    with pytest.raises(TypeError, match="booleans"):
        runner.record_group_exposure("g", "member", triggered="yes")
    runner.record_group_exposure("g", "member", triggered=True, manifested=True,
                                 evidence={"observed": "effect"})
    runner.stop()
    report = runner.exposure_report()
    assert report["verdict"] == "valid"
    assert report["groups"][0]["members"][0]["manifested"] is True


def test_docker_target_identity_survives_plan_and_scenario_round_trip():
    target = DockerTarget("coder-1", exec_user="1000")
    controller = DockerContainerControllerTarget("coder-2")
    group = InjectionGroup(name="docker-group", injections=[
        Injection("cpu", target, CPUStress()),
        Injection("kill", controller, CPUStress()),
    ])
    scenario = Scenario("docker", groups=[group])
    plan = ExperimentPlan.from_scenario(scenario, target)
    restored = ExperimentPlan.from_dict(plan.to_dict())
    assert restored.target.kind == "docker"
    assert restored.target.container_id == "coder-1"
    assert restored.groups[0].injections[1].target.kind == "docker_controller"
    assert restored.groups[0].injections[1].target.container_id == "coder-2"
    plan.validate()
    scenario_copy = Scenario.from_dict(scenario.to_dict())
    assert isinstance(scenario_copy.groups[0].injections[0].target, DockerTarget)
    assert isinstance(scenario_copy.groups[0].injections[1].target,
                      DockerContainerControllerTarget)
    assert scenario_copy.groups[0].injections[0].target.container_id == "coder-1"
    assert _target_info(target) == ("docker", "coder-1")
    assert _target_info(controller) == ("docker_controller", "coder-2")


def test_public_docker_target_executes_only_in_selected_container(monkeypatch):
    assert PublicDockerTarget is DockerTarget
    target = PublicDockerTarget("coder-1")
    commands = []

    def fake_docker(args, *, timeout):
        commands.append(args)
        return 0, "true\n" if args[0] == "inspect" else "ok\n", ""

    monkeypatch.setattr("chaos_jungle.targets.docker.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(target, "_docker", fake_docker)
    assert target.run("printf ok") == (0, "ok\n", "")
    assert target.sudo("id -u") == (0, "ok\n", "")
    assert commands[1] == ["exec", "coder-1", "/bin/sh", "-lc", "printf ok"]
    assert commands[2] == ["exec", "--user", "0", "coder-1", "/bin/sh", "-lc", "id -u"]
    with pytest.raises(ValueError, match="unsafe command"):
        target.run("docker ps")


def test_missing_docker_identity_fails_closed():
    group = {"name": "bad", "injections": [{
        "id": "member", "target": {"kind": "docker"},
        "fault": {"kind": "CPUStress", "params": {}},
    }]}
    with pytest.raises(ValueError, match="container_id"):
        Scenario.from_dict({"id": "s", "name": "bad", "groups": [group]})


def test_existing_session_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sessions(id INTEGER PRIMARY KEY, name TEXT, "
                     "started_at TEXT, stopped_at TEXT, status TEXT, "
                     "target_type TEXT, target_addr TEXT, abort_reason TEXT, verdict TEXT)")
        conn.execute("INSERT INTO sessions(id, name, started_at, status, verdict) "
                     "VALUES(1, 'old', 'now', 'reverted', 'VALID')")
    db = SessionDB(str(path))
    assert db.get_session(1)["verdict"] == "VALID"
    assert db.get_exposure_report(1) is None
    assert db.get_session(1)["exposure_verdict"] == "inconclusive"
