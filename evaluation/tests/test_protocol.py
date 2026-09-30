"""Tests for baseline/fault isolation and lifecycle evidence extraction.

Covers acceptance criteria:
  - Baseline/fault isolation
  - Lifecycle evidence extraction
  - CJ fault smoke verification (dry-run mode)
"""

import os
import pytest

from evaluation.agents.autogen_style import AutoGenStyleAgent
from evaluation.agents.base import ModelClient
from evaluation.benchmarks.humanevalplus import _bundled_tasks
from evaluation.experiments.protocol import ExperimentProtocol, RunRecord, LifecycleEvidence
from evaluation.output import load_jsonl


def _ok_executor(code): return True, "OK"
def _fail_executor(code): return False, "FAIL"


@pytest.fixture
def task():
    return _bundled_tasks()[0]


@pytest.fixture
def agent(client):
    return AutoGenStyleAgent(client=client, executor=_ok_executor, max_turns=2)


# ── Isolation ─────────────────────────────────────────────────────────────────

class TestBaselineFaultIsolation:

    def test_baseline_record_has_fault_none(self, agent, task, tmp_path):
        proto = ExperimentProtocol(agent, "none", str(tmp_path), dry_run=True)
        records = proto.run_task(task, seed=0)
        baseline_recs = [r for r in records if r.phase == "baseline"]
        assert len(baseline_recs) >= 1
        for r in baseline_recs:
            assert r.fault_type == "none"

    def test_baseline_and_fault_have_same_task_id(self, agent, task, tmp_path):
        proto = ExperimentProtocol(agent, "llm_timeout", str(tmp_path), dry_run=True)
        records = proto.run_task(task, seed=0)
        task_ids = {r.task_id for r in records}
        assert len(task_ids) == 1
        assert task.task_id in task_ids

    def test_baseline_phase_is_baseline(self, agent, task, tmp_path):
        proto = ExperimentProtocol(agent, "llm_timeout", str(tmp_path), dry_run=True)
        records = proto.run_task(task, seed=0)
        for r in records:
            assert r.phase in ("baseline", "fault")

    def test_repeats_produce_multiple_records(self, agent, task, tmp_path):
        proto = ExperimentProtocol(agent, "none", str(tmp_path), dry_run=True)
        records = proto.run_task(task, seed=0, repeats=3)
        assert len(records) == 3  # 3 baseline only (fault_name=none)

    def test_different_seeds_per_repeat(self, agent, task, tmp_path):
        proto = ExperimentProtocol(agent, "none", str(tmp_path), dry_run=True)
        records = proto.run_task(task, seed=42, repeats=2)
        # Each record has unique run_id
        ids = [r.run_id for r in records]
        assert len(set(ids)) == len(ids)

    def test_no_memory_leak_between_runs(self, agent, task, tmp_path):
        """Each run gets a fresh agent.run() call — message history not shared."""
        proto = ExperimentProtocol(agent, "none", str(tmp_path), dry_run=True)
        # Run twice — if histories leaked, the second run would have extra turns
        r1 = proto.run_task(task, seed=0, repeats=1)[0]
        r2 = proto.run_task(task, seed=1, repeats=1)[0]
        # Both should have similar (small) turn counts, not accumulating
        assert r1.turns <= 10
        assert r2.turns <= 10


# ── Lifecycle evidence ────────────────────────────────────────────────────────

class TestLifecycleEvidence:

    def test_dry_run_sets_configured_true(self, agent, task, tmp_path):
        proto = ExperimentProtocol(agent, "llm_timeout", str(tmp_path), dry_run=True)
        records = proto.run_task(task, seed=0)
        fault_recs = [r for r in records if r.phase == "fault"]
        assert len(fault_recs) == 1
        assert fault_recs[0].lifecycle.configured is True

    def test_dry_run_verdict_inconclusive(self, agent, task, tmp_path):
        proto = ExperimentProtocol(agent, "llm_timeout", str(tmp_path), dry_run=True)
        records = proto.run_task(task, seed=0)
        fault_recs = [r for r in records if r.phase == "fault"]
        assert fault_recs[0].lifecycle.verdict == "INCONCLUSIVE"
        assert fault_recs[0].validity == "inconclusive"

    def test_baseline_lifecycle_not_set(self, agent, task, tmp_path):
        proto = ExperimentProtocol(agent, "llm_timeout", str(tmp_path), dry_run=True)
        records = proto.run_task(task, seed=0)
        baseline_recs = [r for r in records if r.phase == "baseline"]
        # Baseline has no lifecycle evidence (configured=False, verdict=pending)
        assert baseline_recs[0].lifecycle.configured is False

    def test_lifecycle_evidence_dataclass_defaults(self):
        ev = LifecycleEvidence()
        assert ev.configured is False
        assert ev.verdict == "pending"
        assert ev.activated is None
        assert ev.recovered is None


# ── Fault smoke verification (dry-run) ────────────────────────────────────────

class TestFaultSmokeVerification:

    def test_fault_configured_in_catalog(self):
        from evaluation.experiments.fault_campaign import FAULT_CATALOG, get_fault_spec
        for entry in FAULT_CATALOG:
            spec = get_fault_spec(entry["name"])
            assert spec["cj_class"]
            assert "parameters" in spec

    def test_build_cj_fault_returns_fault_object(self):
        from evaluation.experiments.fault_campaign import build_cj_fault
        from chaos_jungle.faults.base import Fault
        fault = build_cj_fault("llm_timeout")
        assert isinstance(fault, Fault)

    def test_all_catalog_faults_buildable(self):
        from evaluation.experiments.fault_campaign import FAULT_CATALOG, build_cj_fault
        from chaos_jungle.faults.base import Fault
        for entry in FAULT_CATALOG:
            fault = build_cj_fault(entry["name"])
            assert isinstance(fault, Fault), f"Failed for {entry['name']}"

    def test_unknown_fault_raises(self):
        from evaluation.experiments.fault_campaign import build_cj_fault
        with pytest.raises(KeyError, match="unknown_fault"):
            build_cj_fault("unknown_fault")

    def test_jsonl_appended_per_run(self, agent, task, tmp_path):
        proto = ExperimentProtocol(agent, "none", str(tmp_path), dry_run=True)
        proto.run_task(task, seed=0, repeats=2)
        records = load_jsonl(str(tmp_path / "runs.jsonl"))
        assert len(records) == 2

    def test_record_to_dict_is_json_serializable(self, agent, task, tmp_path):
        import json
        proto = ExperimentProtocol(agent, "none", str(tmp_path), dry_run=True)
        records = proto.run_task(task, seed=0)
        for r in records:
            d = r.to_dict()
            serialized = json.dumps(d)  # must not raise
            assert serialized
