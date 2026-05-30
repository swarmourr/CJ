"""ExperimentSuite — run multiple chaos scenarios in parallel or sequence.

Example (Python API)::

    from chaos_jungle import Scenario
    from chaos_jungle.faults import NetworkDelay, NetworkLoss
    from chaos_jungle.targets import SSHTarget, LocalTarget
    from chaos_jungle.suite import ExperimentSuite

    suite = ExperimentSuite(duration="10m")
    suite.add(Scenario("baseline", []), LocalTarget())
    suite.add(Scenario("net-delay", [NetworkDelay("100ms")]), SSHTarget("node1", user="ubuntu"))
    suite.add(Scenario("net-loss",  [NetworkLoss("5%")]),  SSHTarget("node2", user="ubuntu"))

    results = suite.run(parallel=True)
    for name, result in results.items():
        print(name, "OK" if result["error"] is None else result["error"])

Example (YAML config)::

    from chaos_jungle.suite import ExperimentSuite

    suite = ExperimentSuite.from_yaml("my-suite.yml")
    results = suite.run()

YAML schema::

    duration: 10m          # optional — applies to all experiments
    conflict: raise        # raise | warn | force
    auto_install: false
    experiments:
      - name: baseline
        target: local
        faults: []
      - name: net-delay
        target: ssh://ubuntu@node1
        duration: 5m       # per-experiment override
        faults:
          - kind: NetworkDelay
            delay: 100ms
            jitter: 10ms
      - name: net-loss
        target: ssh://ubuntu@node2
        faults:
          - kind: NetworkLoss
            rate: 5%
"""

from __future__ import annotations
import concurrent.futures
import time
from typing import Any

from chaos_jungle._duration import parse_duration
from chaos_jungle.guardrails import SuiteValidator, apply_guardrails, ConflictError, ConflictWarning
from chaos_jungle.runner import ChaosRunner
from chaos_jungle.scenario import Scenario
from chaos_jungle.targets.base import Target
from chaos_jungle.targets.local import LocalTarget


class ExperimentResult:
    """Result of a single experiment within a suite.

    Attributes
    ----------
    name :
        Scenario name.
    status :
        ``"ok"``, ``"error"``, or ``"skipped"``.
    error :
        Exception if the experiment failed, else ``None``.
    duration_s :
        Wall-clock seconds the experiment actually ran (excludes queue time).
    session_id :
        Database session id, or ``None`` if the experiment never started.
    """

    def __init__(
        self,
        name: str,
        status: str = "ok",
        error: Exception | None = None,
        duration_s: float = 0.0,
        session_id: int | None = None,
    ) -> None:
        self.name = name
        self.status = status
        self.error = error
        self.duration_s = duration_s
        self.session_id = session_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "error": str(self.error) if self.error else None,
            "duration_s": round(self.duration_s, 3),
            "session_id": self.session_id,
        }


class ExperimentSuite:
    """Manage and execute a collection of chaos experiments.

    Parameters
    ----------
    duration : str or int or float, optional
        Default chaos duration for every experiment. Accepts the same
        formats as :meth:`ChaosRunner.run` (``"10m"``, ``"1h30m"``,
        ``30``, …). If ``None`` the experiments run until stopped.
    conflict : str
        Conflict handling mode: ``"raise"`` (default), ``"warn"``, or
        ``"force"``.
    auto_install : bool
        Whether to auto-install missing dependencies via apt-get.
    """

    def __init__(
        self,
        duration: str | int | float | None = None,
        conflict: str = "raise",
        auto_install: bool = False,
    ) -> None:
        self.duration = duration
        self.conflict = conflict
        self.auto_install = auto_install
        self._experiments: list[tuple[Scenario, Target, str | int | float | None]] = []

    # ── Building the suite ────────────────────────────────────────

    def add(
        self,
        scenario: Scenario,
        target: Target | None = None,
        duration: str | int | float | None = None,
    ) -> "ExperimentSuite":
        """Add an experiment to the suite.

        Parameters
        ----------
        scenario :
            The scenario to run.
        target :
            Where to run it. Defaults to :class:`~chaos_jungle.targets.local.LocalTarget`.
        duration :
            Per-experiment duration override. Falls back to the suite-level
            ``duration`` if not given.

        Returns
        -------
        ExperimentSuite
            Self, for chaining.
        """
        self._experiments.append((scenario, target or LocalTarget(), duration))
        return self

    def __len__(self) -> int:
        return len(self._experiments)

    # ── Running ───────────────────────────────────────────────────

    def run(
        self,
        parallel: bool = True,
        max_workers: int | None = None,
    ) -> dict[str, ExperimentResult]:
        """Execute all experiments.

        Parameters
        ----------
        parallel : bool
            If ``True`` (default) all experiments start simultaneously in
            separate threads — useful for injecting faults on multiple nodes
            at the same time. Each experiment targets a *different* machine
            (enforced by :class:`~chaos_jungle.guardrails.SuiteValidator`).

            If ``False`` experiments run one after the other.
        max_workers : int, optional
            Maximum thread-pool size when ``parallel=True``. Defaults to the
            number of experiments.

        Returns
        -------
        dict[str, ExperimentResult]
            Mapping of scenario name → :class:`ExperimentResult`.

        Raises
        ------
        ConflictError
            If the suite violates guardrail rules and ``conflict="raise"``.
        ValueError
            If no experiments were added.
        """
        if not self._experiments:
            raise ValueError("No experiments added to the suite.")

        # suite-level guardrails
        if self.conflict != "force":
            import warnings
            validator = SuiteValidator()
            pairs = [(s, t) for s, t, _ in self._experiments]
            # scenario-level checks always run (catches internal fault conflicts)
            # duplicate-target check only applies to parallel runs
            try:
                if parallel:
                    validator.check(pairs)
                else:
                    validator._check_scenario_conflicts(pairs)
            except ConflictError as e:
                if self.conflict == "warn":
                    warnings.warn(str(e), ConflictWarning, stacklevel=2)
                else:
                    raise

        if parallel:
            return self._run_parallel(max_workers)
        return self._run_sequential()

    def _run_one(
        self,
        scenario: Scenario,
        target: Target,
        duration: str | int | float | None,
    ) -> ExperimentResult:
        effective_duration = duration if duration is not None else self.duration
        result = ExperimentResult(name=scenario.name)
        t0 = time.monotonic()

        runner = ChaosRunner(
            scenario,
            target=target,
            auto_install=self.auto_install,
            conflict=self.conflict,
        )
        try:
            if effective_duration is not None:
                runner.run(effective_duration)
            else:
                runner.start()
                # no duration — run until manually stopped; we start and return
            result.session_id = runner._session_id
        except Exception as exc:
            result.status = "error"
            result.error = exc
            result.session_id = runner._session_id
        finally:
            result.duration_s = time.monotonic() - t0

        return result

    def _run_parallel(self, max_workers: int | None) -> dict[str, ExperimentResult]:
        workers = max_workers or len(self._experiments)
        results: dict[str, ExperimentResult] = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_name = {
                pool.submit(self._run_one, scenario, target, dur): scenario.name
                for scenario, target, dur in self._experiments
            }
            for future in concurrent.futures.as_completed(future_to_name):
                name = future_to_name[future]
                try:
                    results[name] = future.result()
                except Exception as exc:
                    results[name] = ExperimentResult(name=name, status="error", error=exc)

        return results

    def _run_sequential(self) -> dict[str, ExperimentResult]:
        results: dict[str, ExperimentResult] = {}
        for scenario, target, dur in self._experiments:
            results[scenario.name] = self._run_one(scenario, target, dur)
        return results

    # ── YAML loading ──────────────────────────────────────────────

    @classmethod
    def from_yaml(cls, path: str) -> "ExperimentSuite":
        """Build an :class:`ExperimentSuite` from a YAML config file.

        Parameters
        ----------
        path :
            Path to the YAML file.

        Returns
        -------
        ExperimentSuite

        Raises
        ------
        FileNotFoundError
            If the YAML file does not exist.
        ValueError
            If the YAML is missing required fields.

        Examples
        --------
        ::

            suite = ExperimentSuite.from_yaml("my-suite.yml")
            results = suite.run()
        """
        from chaos_jungle.config import load_suite
        return load_suite(path)

    # ── Results summary ───────────────────────────────────────────

    @staticmethod
    def print_summary(results: dict[str, ExperimentResult]) -> None:
        """Print a formatted summary table of suite results.

        Parameters
        ----------
        results :
            Return value of :meth:`run`.
        """
        print(f"\n{'NAME':<30}  {'STATUS':<8}  {'DURATION':>10}  ERROR")
        print("-" * 72)
        for name, r in results.items():
            err_str = str(r.error)[:30] if r.error else ""
            print(f"{name:<30}  {r.status:<8}  {r.duration_s:>9.1f}s  {err_str}")
        ok = sum(1 for r in results.values() if r.status == "ok")
        total = len(results)
        print(f"\n{ok}/{total} experiments passed.")
