from evaluation.benchmarks.base import BenchmarkTask, BenchmarkLoader
from evaluation.benchmarks.humanevalplus import HumanEvalPlusLoader
from evaluation.benchmarks.mbppplus import MBPPPlusLoader

__all__ = [
    "BenchmarkTask",
    "BenchmarkLoader",
    "HumanEvalPlusLoader",
    "MBPPPlusLoader",
]

REGISTRY: dict[str, type[BenchmarkLoader]] = {
    "humanevalplus": HumanEvalPlusLoader,
    "mbppplus":      MBPPPlusLoader,
}
