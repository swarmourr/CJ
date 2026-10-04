"""Deterministic task-complexity proxy metadata for CJ evaluation.

Complexity labels are assigned before model execution from benchmark/task
metadata only.  They are proxies, not outcome-derived difficulty measures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any


_CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("dynamic_programming", ("dynamic", "minimum cost", "tile", "ways", "dp")),
    ("graph", ("graph", "tree", "path", "node", "edge")),
    ("string", ("string", "substring", "palindrome", "parentheses")),
    ("math", ("prime", "number", "integer", "matrix", "deviation")),
    ("collection", ("list", "tuple", "set", "heap", "sort")),
]


@dataclass(frozen=True)
class TaskComplexity:
    benchmark: str
    task_id: str
    prompt_chars: int
    prompt_words: int
    test_chars: int
    test_assertions: int
    reference_solution_chars: int
    reference_solution_lines: int
    required_reasoning_steps: int
    algorithmic_category: str
    complexity_score: int
    complexity_proxy: str
    complexity_proxy_kind: str
    complexity_classification_rule: str

    def to_metadata(self) -> dict[str, Any]:
        return asdict(self)


def classify_task_complexity(
    *,
    benchmark: str,
    task_id: str,
    prompt: str,
    test_code: str,
    canonical_solution: str,
) -> TaskComplexity:
    """Return deterministic complexity-proxy metadata for one task.

    The score intentionally uses static signals available before running the
    model: prompt size, test count, reference-solution size, rough reasoning
    step count, and a keyword-based algorithmic category.
    """
    prompt_text = prompt or ""
    test_text = test_code or ""
    solution_text = canonical_solution or ""
    prompt_words = len(re.findall(r"\w+", prompt_text))
    prompt_chars = len(prompt_text)
    test_chars = len(test_text)
    test_assertions = len(re.findall(r"\bassert\b", test_text))
    solution_lines = [
        line for line in solution_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    reference_solution_lines = len(solution_lines)
    required_reasoning_steps = _reasoning_steps(prompt_text, solution_text)
    category = _algorithmic_category(prompt_text)
    score = (
        prompt_chars
        + 2 * test_chars
        + 80 * test_assertions
        + 120 * reference_solution_lines
        + 180 * required_reasoning_steps
    )
    if category in {"dynamic_programming", "graph"}:
        score += 500
    if score < 2200:
        stratum = "small"
    elif score < 5200:
        stratum = "medium"
    else:
        stratum = "large"
    return TaskComplexity(
        benchmark=benchmark,
        task_id=task_id,
        prompt_chars=prompt_chars,
        prompt_words=prompt_words,
        test_chars=test_chars,
        test_assertions=test_assertions,
        reference_solution_chars=len(solution_text),
        reference_solution_lines=reference_solution_lines,
        required_reasoning_steps=required_reasoning_steps,
        algorithmic_category=category,
        complexity_score=score,
        complexity_proxy=stratum,
        complexity_proxy_kind="static_pre_execution_proxy",
        complexity_classification_rule=(
            "score = prompt_chars + 2*test_chars + 80*test_assertions + "
            "120*reference_solution_lines + 180*required_reasoning_steps + "
            "500 if category in {dynamic_programming, graph}; "
            "small<2200, medium<5200, otherwise large"
        ),
    )


def _reasoning_steps(prompt: str, solution: str) -> int:
    text = f"{prompt}\n{solution}".lower()
    steps = 1
    if any(token in text for token in ("loop", "for ", "while ", "iterate")):
        steps += 1
    if any(token in text for token in ("nested", "matrix", "grid", "path", "dynamic", "heap")):
        steps += 1
    if any(token in text for token in ("edge", "case", "empty", "prime", "balanced")):
        steps += 1
    if any(token in text for token in ("recursion", "recursive", "dp", "memo")):
        steps += 1
    return min(steps, 6)


def _algorithmic_category(prompt: str) -> str:
    text = prompt.lower()
    for category, words in _CATEGORY_KEYWORDS:
        if any(word in text for word in words):
            return category
    return "general"

