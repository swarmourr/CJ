"""Sandboxed code executor for benchmark evaluation.

Executes generated Python code in an isolated subprocess with:
  - Hard wall-clock timeout (default 10 s)
  - Isolated temporary directory (cleaned up after every run)
  - Captured stdout and stderr (no terminal output leaks)
  - No network: the subprocess inherits the environment but code that
    tries to open network connections will simply fail or time out
    (full network namespace isolation requires root; we rely on the
    benchmark test suite not needing network access)

API
---
    ok, output = sandbox_exec(code, timeout_s=10)

``ok``     — True if the process exited 0 (all assertions passed).
``output`` — captured stdout + stderr (truncated to 8 kB).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile


_DEFAULT_TIMEOUT = 10   # seconds

# Module-level ground-truth cache: dataset_name → per-task expected_output dict.
# Populated lazily by _get_evalplus_groundtruth(); isolated per process so tests
# can patch it without affecting other tests.
_EVALPLUS_GROUNDTRUTH: dict[str, dict] = {}


def sandbox_exec(
    code: str,
    timeout_s: float = _DEFAULT_TIMEOUT,
    extra_env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Execute *code* in a sandboxed subprocess.

    Parameters
    ----------
    code : str
        Complete Python source to execute (solution + test harness).
    timeout_s : float
        Hard wall-clock timeout in seconds.
    extra_env : dict, optional
        Extra environment variables for the subprocess.

    Returns
    -------
    (success, output) : (bool, str)
        *success* is True iff exit code is 0.
        *output* contains up to 8 kB of combined stdout + stderr.
    """
    tmpdir = tempfile.mkdtemp(prefix="cj_eval_")
    try:
        src_path = os.path.join(tmpdir, "solution.py")
        with open(src_path, "w", encoding="utf-8") as f:
            f.write(code)

        env = {
            # Minimal environment — no proxy vars, no API keys
            "PATH":       os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME":       tmpdir,
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            "TMPDIR":     tmpdir,
        }
        if extra_env:
            env.update(extra_env)

        try:
            result = subprocess.run(
                [sys.executable, src_path],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                cwd=tmpdir,
                env=env,
            )
            combined = (result.stdout + result.stderr)[:8192]
            return result.returncode == 0, combined

        except subprocess.TimeoutExpired:
            return False, f"TIMEOUT: execution exceeded {timeout_s}s"

        except Exception as exc:
            return False, f"EXECUTOR ERROR: {exc}"

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _get_evalplus_groundtruth(dataset: str) -> dict:
    """Return (cached) expected-output dict for the given evalplus dataset.

    The first call per dataset runs the canonical solution on every input to
    produce ground-truth outputs; evalplus also persists this to a pickle
    file on disk so subsequent cold-process starts are fast.  Thereafter the
    in-process ``_EVALPLUS_GROUNDTRUTH`` dict makes repeated calls O(1).

    Parameters
    ----------
    dataset : ``"humaneval"`` | ``"mbpp"``

    Returns
    -------
    dict
        Mapping ``task_id → per-task oracle dict`` (the structure that
        ``evalplus.evaluate.check_correctness`` expects as ``expected_output``).
    """
    if dataset in _EVALPLUS_GROUNDTRUTH:
        return _EVALPLUS_GROUNDTRUTH[dataset]

    if dataset == "humaneval":
        from evalplus.data import get_human_eval_plus, get_human_eval_plus_hash  # type: ignore[import]
        from evalplus.evaluate import get_groundtruth  # type: ignore[import]
        problems = get_human_eval_plus()
        hashcode = get_human_eval_plus_hash()
        gt = get_groundtruth(problems, hashcode, [])
    elif dataset == "mbpp":
        from evalplus.data import get_mbpp_plus, get_mbpp_plus_hash  # type: ignore[import]
        from evalplus.eval._special_oracle import MBPP_OUTPUT_NOT_NONE_TASKS  # type: ignore[import]
        from evalplus.evaluate import get_groundtruth  # type: ignore[import]
        problems = get_mbpp_plus()
        hashcode = get_mbpp_plus_hash()
        gt = get_groundtruth(problems, hashcode, MBPP_OUTPUT_NOT_NONE_TASKS)
    else:
        raise ValueError(f"Unknown evalplus dataset: {dataset!r}. Expected 'humaneval' or 'mbpp'.")

    _EVALPLUS_GROUNDTRUTH[dataset] = gt
    return gt


def _score_evalplus(
    task: "BenchmarkTask",
    generated_code: str,
    timeout_s: float,
) -> tuple[bool, int, int, str] | None:
    """Score using the official evalplus 0.3.1 evaluator (base + plus inputs).

    API (evalplus 0.3.1):
        check_correctness(
            dataset,        # "humaneval" | "mbpp"
            completion_id,  # int (arbitrary; used for logging)
            problem,        # full task dict from get_human_eval_plus()
            solution,       # generated code string
            expected_output,# oracle dict for this task_id from get_groundtruth()
            base_only=False,
            fast_check=False,
        ) -> {"base": (status, [bool, ...]), "plus": (status, [bool, ...])}

    status is the string constant ``evalplus.eval.PASS`` ("pass") when all
    inputs in that split pass; any other value means failure.

    Returns
    -------
    (ok, tests_passed, tests_total, summary) if the task has evalplus metadata.
    None if the task has no evalplus metadata (caller should use sandbox_exec).

    Raises
    ------
    ImportError  if evalplus is not installed (propagates to caller).
    """
    problem = task.metadata.get("evalplus_problem")
    dataset = task.metadata.get("evalplus_dataset")
    if not problem or not dataset:
        return None

    # ImportError propagates — evalplus-sourced tasks must not silently degrade.
    from evalplus.evaluate import check_correctness  # type: ignore[import]
    from evalplus.eval import PASS  # type: ignore[import]

    gt       = _get_evalplus_groundtruth(dataset)
    task_id  = task.task_id
    expected = gt.get(task_id)
    if expected is None:
        return False, 0, 0, f"EVALPLUS: no ground truth for {task_id!r}"

    result = check_correctness(
        dataset,
        0,              # completion_id
        problem,
        generated_code,
        expected,
        base_only=False,
        fast_check=False,
    )
    base_status, base_details = result["base"]
    plus_status, plus_details = result["plus"]

    all_results  = list(base_details) + list(plus_details)
    tests_total  = len(all_results)
    tests_passed = sum(1 for r in all_results if r)
    ok = base_status == PASS and plus_status == PASS and tests_total > 0

    summary = (
        f"evalplus({dataset}): "
        f"base={base_status} ({sum(base_details)}/{len(base_details)}) "
        f"plus={plus_status} ({sum(plus_details)}/{len(plus_details)})"
    )
    return ok, tests_passed, tests_total, summary


def score_task(
    task: "BenchmarkTask",
    generated_code: str,
    timeout_s: float = _DEFAULT_TIMEOUT,
) -> tuple[bool, int, int, str]:
    """Score generated code against a BenchmarkTask's test suite.

    Dispatch logic
    --------------
    * Tasks with ``metadata["source"] == "evalplus"`` **must** be evaluated
      through ``evalplus.evaluate.check_correctness`` which runs both the base
      and augmented plus inputs.  There is **no silent fallback** to
      sandbox_exec for these tasks; any failure (missing evalplus, exception,
      zero tests executed) is returned as ``(False, 0, 0, message)``.

    * Bundled/smoke tasks (``source == "bundled"``) use sandbox_exec with the
      runnable ``test_code`` assertions stored in the task.

    Parameters
    ----------
    task : BenchmarkTask
    generated_code : str
        Code produced by the agent (function body or full module).
    timeout_s : float

    Returns
    -------
    (success, tests_passed, tests_total, output) : tuple
    """
    if not generated_code.strip():
        return False, 0, 0, "EMPTY: agent produced no code"

    if task.metadata.get("source") == "evalplus":
        # Strict path: no silent fallback to sandbox_exec.
        try:
            ep = _score_evalplus(task, generated_code, timeout_s)
        except Exception as exc:
            return False, 0, 0, f"EVALPLUS ERROR: {exc}"
        if ep is None:
            return False, 0, 0, "EVALPLUS: no evalplus metadata on task"
        ok, tests_passed, tests_total, output = ep
        if tests_total == 0:
            return False, 0, 0, f"EVALPLUS: zero tests executed — {output}"
        return ok, tests_passed, tests_total, output

    # Bundled/smoke tasks: sandbox_exec with deterministic test_code assertions.
    full_code = task.full_exec_code(generated_code)
    ok, output = sandbox_exec(full_code, timeout_s=timeout_s)

    # Rough test count from assertion lines in test_code
    tests_total  = task.test_code.count("assert ")
    tests_passed = tests_total if ok else 0
    if not ok and "AssertionError" in output:
        # Count how many asserts succeeded before failure (heuristic)
        lines  = output.split("\n")
        passed = sum(1 for l in lines if "assert" in l.lower() and "error" not in l.lower())
        tests_passed = max(0, min(passed, tests_total - 1))

    return ok, tests_passed, tests_total, output
