"""Tests for benchmark loading and deterministic scoring.

Covers acceptance criteria:
  - Benchmark loading and deterministic scoring
  - Timeout and sandbox behavior
  - Cleanup after exceptions
"""

import time

import pytest

from evaluation.benchmarks.base import BenchmarkTask
from evaluation.benchmarks.humanevalplus import HumanEvalPlusLoader, _bundled_tasks
from evaluation.benchmarks.mbppplus import MBPPPlusLoader, _bundled_tasks as mbpp_bundled
from evaluation.benchmarks.executor import sandbox_exec, score_task


# ── Benchmark loading ─────────────────────────────────────────────────────────

class TestBenchmarkLoading:

    def test_humanevalplus_smoke_loads_5(self):
        loader = HumanEvalPlusLoader(subset="smoke")
        tasks = loader.load(seed=0)
        assert len(tasks) == 5

    def test_mbppplus_smoke_loads_5(self):
        loader = MBPPPlusLoader(subset="smoke")
        tasks = loader.load(seed=0)
        assert len(tasks) == 5

    def test_tasks_have_required_fields(self):
        tasks = _bundled_tasks()
        for t in tasks:
            assert t.task_id
            assert t.prompt
            assert t.entry_point
            assert t.test_code
            assert t.benchmark == "humanevalplus"

    def test_task_ids_are_unique(self):
        tasks = _bundled_tasks()
        ids = [t.task_id for t in tasks]
        assert len(ids) == len(set(ids))

    def test_subset_subset_same_order(self):
        loader = HumanEvalPlusLoader(subset="smoke")
        tasks1 = loader.load(seed=42)
        tasks2 = loader.load(seed=42)
        assert [t.task_id for t in tasks1] == [t.task_id for t in tasks2]

    def test_different_seeds_may_differ(self):
        loader = HumanEvalPlusLoader(subset="smoke")
        loader.smoke_n = 3
        # With only 5 bundled tasks and n=3, different seeds pick different subsets
        tasks1 = loader.load(seed=1)
        tasks2 = loader.load(seed=99)
        # They may or may not differ — just check they are valid lists
        assert len(tasks1) == 3
        assert len(tasks2) == 3

    def test_agent_prompt_equals_prompt(self):
        task = _bundled_tasks()[0]
        assert task.agent_prompt() == task.prompt

    def test_full_exec_code_contains_test(self):
        task = _bundled_tasks()[0]
        code = "def has_close_elements(numbers, threshold): return False"
        full = task.full_exec_code(code)
        assert code in full
        assert task.test_code in full

    def test_mbpp_bundled_tasks_valid(self):
        tasks = mbpp_bundled()
        assert len(tasks) == 5
        for t in tasks:
            assert t.benchmark == "mbppplus"
            assert t.entry_point


# ── Sandbox executor ──────────────────────────────────────────────────────────

class TestSandboxExecutor:

    def test_correct_code_succeeds(self):
        code = (
            "def add(a, b): return a + b\n"
            "assert add(1, 2) == 3\n"
            "assert add(0, 0) == 0\n"
        )
        ok, output = sandbox_exec(code)
        assert ok is True
        assert output == "" or isinstance(output, str)

    def test_failing_assertion_returns_false(self):
        code = "assert 1 == 2, 'wrong'"
        ok, output = sandbox_exec(code)
        assert ok is False
        assert "AssertionError" in output

    def test_syntax_error_returns_false(self):
        code = "def bad(: return 1"
        ok, output = sandbox_exec(code)
        assert ok is False

    def test_timeout_enforced(self):
        code = "import time\ntime.sleep(60)\n"
        t0 = time.time()
        ok, output = sandbox_exec(code, timeout_s=1.0)
        elapsed = time.time() - t0
        assert ok is False
        assert "TIMEOUT" in output
        assert elapsed < 5  # should not take 60 s

    def test_cleanup_after_exception(self):
        """Temp dir is cleaned up even when code raises."""
        import os, tempfile
        before = set(os.listdir(tempfile.gettempdir()))
        code = "raise RuntimeError('boom')"
        ok, _ = sandbox_exec(code)
        after = set(os.listdir(tempfile.gettempdir()))
        # cj_eval_ dirs from this run should not persist
        lingering = [d for d in (after - before) if d.startswith("cj_eval_")]
        assert lingering == [], f"Temp dirs not cleaned up: {lingering}"

    def test_empty_code_fails_gracefully(self):
        ok, output = sandbox_exec("")
        # Empty file exits 0 in Python
        assert ok is True

    def test_no_api_key_in_env(self):
        """Sandbox should not expose CJ_EVAL_API_KEY to executed code."""
        code = (
            "import os\n"
            "key = os.environ.get('CJ_EVAL_API_KEY', '')\n"
            "assert key == '', f'key leaked: {key}'\n"
        )
        ok, output = sandbox_exec(code)
        assert ok is True


# ── Score task ────────────────────────────────────────────────────────────────

class TestScoreTask:

    def test_canonical_solution_passes(self):
        task = _bundled_tasks()[2]  # truncate_number
        code = "def truncate_number(number): return number % 1.0"
        ok, tp, tt, _ = score_task(task, code)
        assert ok is True
        assert tp > 0
        assert tt > 0

    def test_wrong_solution_fails(self):
        task = _bundled_tasks()[2]
        code = "def truncate_number(number): return 0.0"  # wrong
        ok, tp, tt, _ = score_task(task, code)
        assert ok is False

    def test_empty_code_returns_false(self):
        task = _bundled_tasks()[0]
        ok, tp, tt, output = score_task(task, "")
        assert ok is False
        assert "EMPTY" in output or tp == 0
