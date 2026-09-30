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
import textwrap


_DEFAULT_TIMEOUT = 10   # seconds


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


def score_task(
    task: "BenchmarkTask",
    generated_code: str,
    timeout_s: float = _DEFAULT_TIMEOUT,
) -> tuple[bool, int, int, str]:
    """Score generated code against a BenchmarkTask's test suite.

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

    full_code = task.full_exec_code(generated_code)
    ok, output = sandbox_exec(full_code, timeout_s=timeout_s)

    # Rough test count from assertion lines in test_code
    tests_total = task.test_code.count("assert ")
    tests_passed = tests_total if ok else 0
    if not ok and "AssertionError" in output:
        # Count how many asserts succeeded before failure (heuristic)
        lines = output.split("\n")
        passed = sum(1 for l in lines if "assert" in l.lower() and "error" not in l.lower())
        tests_passed = max(0, min(passed, tests_total - 1))

    return ok, tests_passed, tests_total, output
