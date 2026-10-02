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


# ── EvalPlus scorer unit tests ────────────────────────────────────────────────

@pytest.mark.real_eval
class TestEvalPlusScoring:
    """Verify score_task() with the official evalplus 0.3.1 evaluator.

    All evalplus calls are mocked so no network access is required.
    The tests exercise the wrapping logic in _score_evalplus / score_task,
    not the evalplus library itself.
    """

    # A minimal BenchmarkTask that looks like it came from the evalplus loader
    def _make_task(self, task_id: str = "HumanEval/0") -> "BenchmarkTask":
        from evaluation.benchmarks.base import BenchmarkTask
        problem = {
            "task_id":            task_id,
            "prompt":             "def f(x): ...",
            "entry_point":        "f",
            "canonical_solution": "    return x + 1\n",
            "base_input":         [[1], [2], [3]],
            "plus_input":         [[10], [20], [30], [40], [50]],
        }
        return BenchmarkTask(
            task_id=task_id,
            benchmark="humanevalplus",
            prompt=problem["prompt"],
            entry_point=problem["entry_point"],
            test_code="",
            canonical_solution=problem["canonical_solution"],
            metadata={
                "source":           "evalplus",
                "evalplus_dataset": "humaneval",
                "evalplus_problem": problem,
            },
        )

    def _patch_evalplus(self, monkeypatch, base_result, plus_result):
        """Patch check_correctness and _get_evalplus_groundtruth for one test."""
        import evalplus.evaluate
        monkeypatch.setattr(
            evalplus.evaluate,
            "check_correctness",
            lambda *a, **kw: {"base": base_result, "plus": plus_result},
        )
        import evaluation.benchmarks.executor as ex
        monkeypatch.setattr(
            ex, "_get_evalplus_groundtruth",
            lambda d: {"HumanEval/0": {"base": [], "plus": []}},
        )

    def test_canonical_solution_passes(self, monkeypatch):
        """All base + plus inputs pass → ok=True, tests counted correctly."""
        self._patch_evalplus(
            monkeypatch,
            base_result=("pass", [True, True, True]),
            plus_result=("pass", [True, True, True, True, True]),
        )
        task = self._make_task()
        ok, tp, tt, out = score_task(task, "def f(x): return x + 1")
        assert ok is True, f"Canonical solution should pass: {out}"
        assert tt == 8, f"3 base + 5 plus = 8 total, got {tt}"
        assert tp == 8

    def test_incorrect_solution_fails(self, monkeypatch):
        """Wrong solution: base fails → ok=False."""
        self._patch_evalplus(
            monkeypatch,
            base_result=("failed", [False, False, False]),
            plus_result=("failed", [False] * 5),
        )
        task = self._make_task()
        ok, tp, tt, out = score_task(task, "def f(x): return x")
        assert ok is False, f"Incorrect solution should fail: {out}"
        assert tp < tt

    def test_plus_failure_causes_overall_failure(self, monkeypatch):
        """Base passes but plus fails → ok=False (plus inputs are mandatory)."""
        self._patch_evalplus(
            monkeypatch,
            base_result=("pass", [True, True, True]),
            plus_result=("failed", [False] * 5),
        )
        task = self._make_task()
        ok, tp, tt, out = score_task(task, "def f(x): return x + 1")
        assert ok is False, "Plus failure must cause overall failure"

    def test_evalplus_error_cannot_produce_passing_zero_tests(self, monkeypatch):
        """An exception inside check_correctness must produce (False, 0, 0, ...)."""
        import evalplus.evaluate
        import evaluation.benchmarks.executor as ex

        def crash(*a, **kw):
            raise RuntimeError("simulated evalplus crash")

        monkeypatch.setattr(evalplus.evaluate, "check_correctness", crash)
        monkeypatch.setattr(
            ex, "_get_evalplus_groundtruth",
            lambda d: {"HumanEval/0": {}},
        )
        task = self._make_task()
        ok, tp, tt, out = score_task(task, "def f(x): return x + 1")
        assert ok is False,  "An evaluator crash must never produce a passing result"
        assert tt == 0,      "Zero tests must be recorded on crash"
        assert "ERROR" in out.upper(), f"Output should mention ERROR, got: {out!r}"

    def test_zero_tests_executed_is_failure(self, monkeypatch):
        """Empty detail lists (tests_total=0) must be treated as failure."""
        self._patch_evalplus(
            monkeypatch,
            base_result=("pass", []),   # no base tests run
            plus_result=("pass", []),   # no plus tests run
        )
        task = self._make_task()
        ok, tp, tt, out = score_task(task, "def f(x): return x + 1")
        assert ok is False, "Zero executed tests must not count as a pass"
        assert tt == 0
