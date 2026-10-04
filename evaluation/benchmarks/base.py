"""Base classes for benchmark task loaders."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BenchmarkTask:
    """A single benchmark task (problem + tests).

    Attributes
    ----------
    task_id : str
        Unique identifier, e.g. ``"HumanEval/0"`` or ``"mbpp/1"``.
    benchmark : str
        Benchmark name: ``"humanevalplus"`` or ``"mbppplus"``.
    prompt : str
        Full problem statement shown to the agent.
    entry_point : str
        Name of the function the agent must implement.
    test_code : str
        Deterministic test code that imports and calls the solution.
        Executed with the generated code prepended in the sandbox.
    canonical_solution : str
        Ground-truth solution (not shown to the agent; used for debugging).
    metadata : dict
        Extra benchmark-specific fields.
    """

    task_id:            str
    benchmark:          str
    prompt:             str
    entry_point:        str
    test_code:          str
    canonical_solution: str = ""
    metadata:           dict = field(default_factory=dict)

    def agent_prompt(self) -> str:
        """Return the prompt shown to the agent.

        EvalPlus MBPP tasks often describe the desired function in prose rather
        than embedding a Python signature.  Publication runs must score the
        exact benchmark entry point, so the prompt always carries the function
        name requirement explicitly.  Tests remain hidden.
        """
        entry = self.entry_point.strip()
        if not entry:
            return self.prompt
        return (
            f"{self.prompt.rstrip()}\n\n"
            "Implementation requirement:\n"
            f"- Define a Python function named `{entry}` exactly.\n"
            "- Return only the final Python source code, preferably in one "
            "```python ... ``` block.\n"
            "- Do not rename the function or wrap it in a class unless the "
            "benchmark prompt explicitly asks for that."
        )

    def full_exec_code(self, generated_code: str) -> str:
        """Concatenate generated code + test code for sandbox execution."""
        return f"{generated_code}\n\n{self.test_code}"


class BenchmarkLoader(ABC):
    """Abstract loader for a deterministic coding benchmark.

    Parameters
    ----------
    subset : ``"smoke"`` | ``"development"`` | ``"full"``
        Task count: smoke=5, development=30, full=all.
    cache_dir : str, optional
        Directory for downloaded benchmark files. Defaults to
        ``~/.cache/cj-eval/<benchmark_name>``.
    """

    name: str = "base"
    smoke_n:       int = 5
    development_n: int = 30

    def __init__(self, subset: str = "smoke", cache_dir: str | None = None) -> None:
        if subset not in ("smoke", "development", "full"):
            raise ValueError(f"subset must be smoke/development/full, got {subset!r}")
        self.subset    = subset
        self.cache_dir = cache_dir or self._default_cache()

    def _default_cache(self) -> str:
        import os
        return os.path.join(os.path.expanduser("~"), ".cache", "cj-eval", self.name)

    @abstractmethod
    def _load_all(self) -> list[BenchmarkTask]:
        """Load and return ALL tasks from the benchmark."""

    def load(self, seed: int = 0) -> list[BenchmarkTask]:
        """Load tasks according to the configured subset.

        Tasks are sorted by task_id for reproducibility.  When subset is
        ``"smoke"`` or ``"development"`` a seeded shuffle selects the subset.
        """
        import random
        tasks = sorted(self._load_all(), key=lambda t: t.task_id)
        if self.subset == "full":
            return tasks
        n = self.smoke_n if self.subset == "smoke" else self.development_n
        rng = random.Random(seed)
        sample = rng.sample(tasks, min(n, len(tasks)))
        return sorted(sample, key=lambda t: t.task_id)
