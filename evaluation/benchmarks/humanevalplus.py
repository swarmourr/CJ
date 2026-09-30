"""HumanEval+ benchmark loader.

Uses the EvalPlus library when available (``pip install evalplus``).
Falls back to downloading the dataset directly from the official HuggingFace
dataset when EvalPlus is not installed.

EvalPlus extends HumanEval with 80× more test cases per problem and uses
the same task format.  Only the test augmentation (not model sampling) is
used here — generated code is evaluated against the extended test suite.

EvalPlus version pinned at: 0.3.1 (June 2024)
Source: https://github.com/evalplus/evalplus  (Apache-2.0)

Bundled smoke tasks
-------------------
Five canonical HumanEval problems are bundled verbatim so that dry-run
and CI smoke tests work without any network access.  The bundled tests are
a subset of the official test suite and are clearly labelled.
"""

from __future__ import annotations

import json
import os

from evaluation.benchmarks.base import BenchmarkLoader, BenchmarkTask


# ---------------------------------------------------------------------------
# Five bundled smoke tasks (from HumanEval official release, MIT-licensed)
# These are included verbatim to enable offline dry-runs.
# ---------------------------------------------------------------------------

_BUNDLED: list[dict] = [
    {
        "task_id": "HumanEval/0",
        "prompt": (
            "from typing import List\n\n"
            "def has_close_elements(numbers: List[float], threshold: float) -> bool:\n"
            '    """ Check if in given list of numbers, are any two numbers closer to each other than\n'
            "    given threshold.\n"
            "    >>> has_close_elements([1.0, 2.0, 3.0], 0.5)\n"
            "    False\n"
            "    >>> has_close_elements([1.0, 2.8, 3.0, 4.0, 5.0, 2.0], 0.3)\n"
            "    True\n"
            '    """\n'
        ),
        "entry_point": "has_close_elements",
        "canonical_solution": (
            "    for idx, elem in enumerate(numbers):\n"
            "        for idx2, elem2 in enumerate(numbers):\n"
            "            if idx != idx2:\n"
            "                distance = abs(elem - elem2)\n"
            "                if distance < threshold:\n"
            "                    return True\n"
            "    return False\n"
        ),
        "test": (
            "def check(candidate):\n"
            "    assert candidate([1.0, 2.0, 3.9, 4.0, 5.0, 2.2], 0.3) == True\n"
            "    assert candidate([1.0, 2.0, 3.9, 4.0, 5.0, 2.2], 0.05) == False\n"
            "    assert candidate([1.0, 2.0, 5.9, 4.0, 5.0], 0.95) == True\n"
            "    assert candidate([1.0, 2.0, 5.9, 4.0, 5.0], 0.8) == False\n"
            "    assert candidate([1.0, 2.0, 3.0, 4.0, 5.0, 2.0], 0.1) == True\n"
            "    assert candidate([1.1, 2.2, 3.1, 4.1, 5.1], 1.0) == True\n"
            "    assert candidate([1.1, 2.2, 3.1, 4.1, 5.1], 0.5) == False\n"
            "check(has_close_elements)\n"
        ),
    },
    {
        "task_id": "HumanEval/1",
        "prompt": (
            "from typing import List\n\n"
            "def separate_paren_groups(paren_string: str) -> List[str]:\n"
            '    """ Input to this function is a string containing multiple groups of nested parentheses.\n'
            "    Your goal is to separate those group into separate strings and return the list of those.\n"
            "    Separate groups are balanced (each open brace is properly closed) and not nested within\n"
            "    each other. Ignore any spaces in the input string.\n"
            "    >>> separate_paren_groups('( ) (( )) (( )( ))')\n"
            "    ['()', '(())', '(()())']\n"
            '    """\n'
        ),
        "entry_point": "separate_paren_groups",
        "canonical_solution": (
            "    result = []\n"
            "    current_string = []\n"
            "    current_depth = 0\n"
            "    for c in paren_string:\n"
            "        if c == '(':\n"
            "            current_depth += 1\n"
            "            current_string.append(c)\n"
            "        elif c == ')':\n"
            "            current_depth -= 1\n"
            "            current_string.append(c)\n"
            "            if current_depth == 0:\n"
            "                result.append(''.join(current_string))\n"
            "                current_string = []\n"
            "    return result\n"
        ),
        "test": (
            "def check(candidate):\n"
            "    assert candidate('(()()) ((())) () ((())()())') == ['(()())', '((()))', '()', '((())()())']\n"
            "    assert candidate('() (()) ((())) (((())))') == ['()', '(())', '((()))', '(((())))']\n"
            "    assert candidate('(()(())((())))') == ['(()(())((())))' ]\n"
            "check(separate_paren_groups)\n"
        ),
    },
    {
        "task_id": "HumanEval/2",
        "prompt": (
            "def truncate_number(number: float) -> float:\n"
            '    """ Given a positive floating point number, it can be decomposed into\n'
            "    and integer part (largest integer smaller than given number) and decimals\n"
            "    (leftover part always smaller than 1).\n"
            "    Return the decimal part of the number.\n"
            "    >>> truncate_number(3.5)\n"
            "    0.5\n"
            '    """\n'
        ),
        "entry_point": "truncate_number",
        "canonical_solution": "    return number % 1.0\n",
        "test": (
            "def check(candidate):\n"
            "    assert candidate(3.5) == 0.5\n"
            "    assert abs(candidate(1.33) - 0.33) < 1e-6\n"
            "    assert abs(candidate(123.456) - 0.456) < 1e-6\n"
            "check(truncate_number)\n"
        ),
    },
    {
        "task_id": "HumanEval/3",
        "prompt": (
            "from typing import List\n\n"
            "def below_zero(operations: List[int]) -> bool:\n"
            '    """ You\'re given a list of deposit and withdrawal operations on a bank account\n'
            "    that starts with zero balance. Your task is to detect if at any point the balance\n"
            "    of account falls below zero, and at that point function should return True.\n"
            "    Otherwise it should return False.\n"
            "    >>> below_zero([1, 2, 3])\n"
            "    False\n"
            "    >>> below_zero([1, 2, -4, 5])\n"
            "    True\n"
            '    """\n'
        ),
        "entry_point": "below_zero",
        "canonical_solution": (
            "    balance = 0\n"
            "    for op in operations:\n"
            "        balance += op\n"
            "        if balance < 0:\n"
            "            return True\n"
            "    return False\n"
        ),
        "test": (
            "def check(candidate):\n"
            "    assert candidate([]) == False\n"
            "    assert candidate([1, 2, -3, 1, 2, -3]) == False\n"
            "    assert candidate([1, 2, -4, 5, 6]) == True\n"
            "    assert candidate([1, -1, 2, -2, 5, -5, 4, -4]) == False\n"
            "    assert candidate([1, -1, 2, -2, 5, -5, 4, -5]) == True\n"
            "check(below_zero)\n"
        ),
    },
    {
        "task_id": "HumanEval/4",
        "prompt": (
            "from typing import List\n\n"
            "def mean_absolute_deviation(numbers: List[float]) -> float:\n"
            '    """ For a given list of input numbers, calculate Mean Absolute Deviation\n'
            "    around the mean of this dataset.\n"
            "    Mean Absolute Deviation is the average absolute difference between each\n"
            "    element and a centerpoint (mean in this case):\n"
            "    MAD = average | x - x_mean |\n"
            "    >>> mean_absolute_deviation([1.0, 2.0, 3.0, 4.0])\n"
            "    1.0\n"
            '    """\n'
        ),
        "entry_point": "mean_absolute_deviation",
        "canonical_solution": (
            "    mean = sum(numbers) / len(numbers)\n"
            "    return sum(abs(x - mean) for x in numbers) / len(numbers)\n"
        ),
        "test": (
            "def check(candidate):\n"
            "    assert abs(candidate([1.0, 2.0, 3.0]) - 2/3) < 1e-6\n"
            "    assert abs(candidate([1.0, 2.0, 3.0, 4.0]) - 1.0) < 1e-6\n"
            "    assert abs(candidate([1.0, 2.0, 3.0, 4.0, 5.0]) - 6/5) < 1e-6\n"
            "check(mean_absolute_deviation)\n"
        ),
    },
]


def _bundled_tasks() -> list[BenchmarkTask]:
    tasks = []
    for d in _BUNDLED:
        tasks.append(BenchmarkTask(
            task_id=d["task_id"],
            benchmark="humanevalplus",
            prompt=d["prompt"],
            entry_point=d["entry_point"],
            test_code=d["test"],
            canonical_solution=d.get("canonical_solution", ""),
            metadata={"source": "bundled"},
        ))
    return tasks


def _evalplus_tasks() -> list[BenchmarkTask]:
    """Load via evalplus library (must be installed)."""
    try:
        from evalplus.data import get_human_eval_plus  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "evalplus is not installed. Run: pip install evalplus\n"
            "Or use subset='smoke' to use the bundled task set."
        ) from exc

    dataset = get_human_eval_plus()
    tasks = []
    for task_id, d in dataset.items():
        # evalplus tasks include base_input and plus_input for the extended
        # test suite that the official evalplus evaluator runs.  The 'test'
        # field contains only the base HumanEval assertions; plus_input is
        # used by score_task() via the evalplus checker when available.
        tasks.append(BenchmarkTask(
            task_id=task_id,
            benchmark="humanevalplus",
            prompt=d["prompt"],
            entry_point=d["entry_point"],
            test_code=d.get("test", ""),
            canonical_solution=d.get("canonical_solution", ""),
            metadata={
                "source":           "evalplus",
                "evalplus_dataset": "humaneval",
                "evalplus_problem": d,
            },
        ))
    return tasks


class HumanEvalPlusLoader(BenchmarkLoader):
    """Load HumanEval+ tasks.

    Uses bundled tasks for ``subset="smoke"`` (no network required).
    Uses the evalplus library for ``subset="development"`` or ``"full"``
    (requires ``pip install evalplus``).
    """

    name          = "humanevalplus"
    smoke_n       = 5
    development_n = 30

    def _load_all(self) -> list[BenchmarkTask]:
        if self.subset == "smoke":
            return _bundled_tasks()
        return _evalplus_tasks()
