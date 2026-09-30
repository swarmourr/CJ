"""MBPP+ benchmark loader.

Uses the EvalPlus library when available (``pip install evalplus``).
Falls back to bundled smoke tasks for offline dry-runs.

MBPP+ extends the sanitised MBPP dataset with additional test cases
per problem, using the same task format as HumanEval+.

EvalPlus version pinned at: 0.3.1 (June 2024)
Source: https://github.com/evalplus/evalplus  (Apache-2.0)
"""

from __future__ import annotations

from evaluation.benchmarks.base import BenchmarkLoader, BenchmarkTask


# Five bundled MBPP tasks for offline smoke runs (from MBPP sanitised set,
# originally released by Google under CC-BY-4.0).
_BUNDLED: list[dict] = [
    {
        "task_id": "mbpp/1",
        "prompt": (
            "Write a function to find the minimum cost path to reach (m, n) from (0, 0) "
            "for the given cost matrix cost[][] and a position (m, n) in cost[][]."
        ),
        "entry_point": "min_cost",
        "test": (
            "def check(candidate):\n"
            "    cost = [[1, 2, 3], [4, 8, 2], [1, 5, 3]]\n"
            "    assert candidate(cost, 2, 2) == 8\n"
            "    cost2 = [[2, 3, 4], [5, 4, 3], [3, 2, 1]]\n"
            "    assert candidate(cost2, 2, 2) == 11\n"
            "check(min_cost)\n"
        ),
        "canonical_solution": (
            "def min_cost(cost, m, n):\n"
            "    import sys\n"
            "    R, C = len(cost), len(cost[0])\n"
            "    tc = [[0]*C for _ in range(R)]\n"
            "    tc[0][0] = cost[0][0]\n"
            "    for i in range(1, R): tc[i][0] = tc[i-1][0] + cost[i][0]\n"
            "    for j in range(1, C): tc[0][j] = tc[0][j-1] + cost[0][j]\n"
            "    for i in range(1, R):\n"
            "        for j in range(1, C):\n"
            "            tc[i][j] = min(tc[i-1][j-1], tc[i-1][j], tc[i][j-1]) + cost[i][j]\n"
            "    return tc[m][n]\n"
        ),
    },
    {
        "task_id": "mbpp/2",
        "prompt": (
            "Write a function to find the similar elements from the given two tuples."
        ),
        "entry_point": "similar_elements",
        "test": (
            "def check(candidate):\n"
            "    assert set(candidate((3, 4, 5, 6), (5, 7, 4, 10))) == {4, 5}\n"
            "    assert set(candidate((1, 2, 3, 4), (5, 4, 3, 7))) == {3, 4}\n"
            "    assert set(candidate((11, 12, 14, 13), (17, 15, 14, 13))) == {13, 14}\n"
            "check(similar_elements)\n"
        ),
        "canonical_solution": (
            "def similar_elements(test_tup1, test_tup2):\n"
            "    return tuple(set(test_tup1) & set(test_tup2))\n"
        ),
    },
    {
        "task_id": "mbpp/3",
        "prompt": (
            "Write a Python function to identify non-prime numbers."
        ),
        "entry_point": "is_not_prime",
        "test": (
            "def check(candidate):\n"
            "    assert candidate(2) == False\n"
            "    assert candidate(10) == True\n"
            "    assert candidate(35) == True\n"
            "    assert candidate(37) == False\n"
            "check(is_not_prime)\n"
        ),
        "canonical_solution": (
            "def is_not_prime(n):\n"
            "    if n < 2: return True\n"
            "    for i in range(2, int(n**0.5)+1):\n"
            "        if n % i == 0: return True\n"
            "    return False\n"
        ),
    },
    {
        "task_id": "mbpp/4",
        "prompt": (
            "Write a function to find the largest integers from a given list of numbers "
            "using heap queue algorithm."
        ),
        "entry_point": "heap_queue_largest",
        "test": (
            "def check(candidate):\n"
            "    assert candidate([25, 35, 22, 85, 14, 65, 75, 22, 58], 3) == [85, 75, 65]\n"
            "    assert candidate([25, 35, 22, 85, 14, 65, 75, 22, 58], 2) == [85, 75]\n"
            "check(heap_queue_largest)\n"
        ),
        "canonical_solution": (
            "import heapq\n"
            "def heap_queue_largest(nums, n):\n"
            "    return heapq.nlargest(n, nums)\n"
        ),
    },
    {
        "task_id": "mbpp/5",
        "prompt": (
            "Write a function to count the number of ways to tile a 3×n board with 2×1 dominoes."
        ),
        "entry_point": "count_ways",
        "test": (
            "def check(candidate):\n"
            "    assert candidate(2) == 3\n"
            "    assert candidate(4) == 11\n"
            "    assert candidate(0) == 1\n"
            "check(count_ways)\n"
        ),
        "canonical_solution": (
            "def count_ways(n):\n"
            "    if n == 0: return 1\n"
            "    if n % 2 != 0: return 0\n"
            "    dp = [0] * (n + 1)\n"
            "    dp[0] = 1\n"
            "    dp[2] = 3\n"
            "    for i in range(4, n + 1, 2):\n"
            "        dp[i] = 4 * dp[i - 2] - dp[i - 4]\n"
            "    return dp[n]\n"
        ),
    },
]


def _bundled_tasks() -> list[BenchmarkTask]:
    return [
        BenchmarkTask(
            task_id=d["task_id"],
            benchmark="mbppplus",
            prompt=d["prompt"],
            entry_point=d["entry_point"],
            test_code=d["test"],
            canonical_solution=d.get("canonical_solution", ""),
            metadata={"source": "bundled"},
        )
        for d in _BUNDLED
    ]


def _evalplus_tasks() -> list[BenchmarkTask]:
    try:
        from evalplus.data import get_mbpp_plus  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "evalplus is not installed. Run: pip install evalplus\n"
            "Or use subset='smoke' to use bundled tasks."
        ) from exc

    dataset = get_mbpp_plus()
    tasks = []
    for task_id, d in dataset.items():
        test_code = d.get("test", "") or ""
        tasks.append(BenchmarkTask(
            task_id=task_id,
            benchmark="mbppplus",
            prompt=d.get("prompt", d.get("text", "")),
            entry_point=d.get("entry_point", "solution"),
            test_code=test_code,
            canonical_solution=d.get("canonical_solution", ""),
            metadata={"source": "evalplus"},
        ))
    return tasks


class MBPPPlusLoader(BenchmarkLoader):
    """Load MBPP+ tasks.

    Uses bundled tasks for ``subset="smoke"`` (no network required).
    Uses evalplus for ``"development"`` and ``"full"`` subsets.
    """

    name          = "mbppplus"
    smoke_n       = 5
    development_n = 30

    def _load_all(self) -> list[BenchmarkTask]:
        if self.subset == "smoke":
            return _bundled_tasks()
        return _evalplus_tasks()
