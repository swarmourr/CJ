"""ChaosScheduler — run chaos experiments on a recurring schedule.

Run an experiment every N seconds/minutes/hours, or once per day at a fixed
time, continuously in the background.

Example::

    from chaos_jungle import ChaosScheduler, Scenario, NetworkDelay
    from chaos_jungle.targets import SSHTarget

    scenario = Scenario("nightly-chaos", [NetworkDelay("200ms")])
    target   = SSHTarget("worker1", user="ubuntu")

    def workload():
        import time
        t0 = time.time()
        call_my_service()
        return {"duration_s": round(time.time() - t0, 2)}

    scheduler = (
        ChaosScheduler(scenario, target, workload=workload)
        .every("1h")                  # run every hour
        .on_result(print)             # called with each MeasurementResult
    )
    scheduler.start()

    # later ...
    scheduler.stop()

Cron-style (once per day at 02:00)::

    scheduler = (
        ChaosScheduler(scenario, target)
        .daily_at("02:00")
    )
    scheduler.start()
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Callable, TYPE_CHECKING

from chaos_jungle._duration import parse_duration

if TYPE_CHECKING:
    from chaos_jungle.runner import MeasurementResult
    from chaos_jungle.scenario import Scenario
    from chaos_jungle.targets.base import Target


class ChaosScheduler:
    """Run a chaos experiment on a recurring schedule.

    Parameters
    ----------
    scenario : Scenario
        The scenario to run on each tick.
    target : Target, optional
        Where to run the faults. Defaults to :class:`~chaos_jungle.targets.local.LocalTarget`.
    workload : callable, optional
        Zero-argument callable returning a metrics dict. Passed to
        ``ChaosRunner.measure()`` on each run. If ``None``, the experiment
        runs ``start()`` / ``stop()`` only without measurement.
    n_baseline : int
        Baseline trials per scheduled run. Default ``1``.
    n_fault : int
        Fault trials per scheduled run. Default ``1``.

    Examples
    --------
    ::

        scheduler = (
            ChaosScheduler(scenario, target, workload=my_workload)
            .every("30m")
            .on_result(lambda r: print(r.summary()))
        )
        scheduler.start()
        time.sleep(3600)
        scheduler.stop()
    """

    def __init__(
        self,
        scenario: "Scenario",
        target: "Target | None" = None,
        workload: "Callable[[], dict] | None" = None,
        n_baseline: int = 1,
        n_fault: int = 1,
    ) -> None:
        self.scenario = scenario
        self.target = target
        self.workload = workload
        self.n_baseline = n_baseline
        self.n_fault = n_fault

        self._interval_s: float | None = None
        self._daily_time: str | None = None
        self._result_callbacks: list[Callable] = []
        self._error_callbacks: list[Callable] = []

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._results: list["MeasurementResult"] = []

    # ── Schedule builders ─────────────────────────────────────────────────────

    def every(self, interval: "str | int | float") -> "ChaosScheduler":
        """Run the experiment every *interval*.

        Accepts human-readable strings like ``"30m"``, ``"1h"``, ``"90s"``,
        or a plain number of seconds.

        Parameters
        ----------
        interval : str or int or float
            How often to run the experiment.
        """
        self._interval_s = parse_duration(interval)
        self._daily_time = None
        return self

    def daily_at(self, time_str: str) -> "ChaosScheduler":
        """Run the experiment once per day at *time_str* (``"HH:MM"`` format).

        Parameters
        ----------
        time_str : str
            Time of day in 24-hour format, e.g. ``"02:00"`` or ``"14:30"``.
        """
        if len(time_str) != 5 or time_str[2] != ":":
            raise ValueError(f"daily_at() expects 'HH:MM' format, got {time_str!r}")
        self._daily_time = time_str
        self._interval_s = None
        return self

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def on_result(self, callback: Callable) -> "ChaosScheduler":
        """Register a callback called with each :class:`~chaos_jungle.runner.MeasurementResult`.

        Parameters
        ----------
        callback : callable
            Called as ``callback(result)`` after each scheduled run.
        """
        self._result_callbacks.append(callback)
        return self

    def on_error(self, callback: Callable) -> "ChaosScheduler":
        """Register a callback called when a scheduled run raises an exception.

        Parameters
        ----------
        callback : callable
            Called as ``callback(exception)`` if a run fails.
        """
        self._error_callbacks.append(callback)
        return self

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> "ChaosScheduler":
        """Start the background scheduler thread.

        Returns
        -------
        ChaosScheduler
            Self, for chaining.

        Raises
        ------
        RuntimeError
            If neither :meth:`every` nor :meth:`daily_at` was configured.
        """
        if self._interval_s is None and self._daily_time is None:
            raise RuntimeError(
                "No schedule configured. Call .every() or .daily_at() first."
            )
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("Scheduler is already running. Call .stop() first.")

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name=f"cj-scheduler-{self.scenario.name}",
        )
        self._thread.start()
        mode = f"every {self._interval_s}s" if self._interval_s else f"daily at {self._daily_time}"
        print(f"[chaos-jungle] Scheduler started — {self.scenario.name!r} ({mode})")
        return self

    def stop(self) -> None:
        """Stop the background scheduler thread gracefully."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        print(f"[chaos-jungle] Scheduler stopped — {self.scenario.name!r}")

    @property
    def results(self) -> list["MeasurementResult"]:
        """All :class:`~chaos_jungle.runner.MeasurementResult` collected so far."""
        return list(self._results)

    # ── Internal loop ─────────────────────────────────────────────────────────

    def _loop(self) -> None:
        if self._interval_s is not None:
            self._interval_loop()
        else:
            self._daily_loop()

    def _interval_loop(self) -> None:
        assert self._interval_s is not None
        while not self._stop_event.is_set():
            self._run_once()
            self._stop_event.wait(timeout=self._interval_s)

    def _daily_loop(self) -> None:
        assert self._daily_time is not None
        target_h, target_m = map(int, self._daily_time.split(":"))
        last_run_date: str | None = None

        while not self._stop_event.is_set():
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            if (
                now.hour == target_h
                and now.minute == target_m
                and last_run_date != today
            ):
                self._run_once()
                last_run_date = today
            self._stop_event.wait(timeout=30)

    def _run_once(self) -> None:
        from chaos_jungle.runner import ChaosRunner
        from chaos_jungle.targets.local import LocalTarget

        target = self.target or LocalTarget()
        runner = ChaosRunner(self.scenario, target)

        print(
            f"[chaos-jungle] Scheduler tick — running {self.scenario.name!r} "
            f"at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        try:
            if self.workload is not None:
                result = runner.measure(
                    self.workload,
                    n_baseline=self.n_baseline,
                    n_fault=self.n_fault,
                )
                self._results.append(result)
                for cb in self._result_callbacks:
                    try:
                        cb(result)
                    except Exception as exc:
                        print(f"[chaos-jungle] Scheduler on_result callback error: {exc}")
            else:
                runner.start()
                runner.stop()
        except Exception as exc:
            print(f"[chaos-jungle] Scheduler run failed: {exc}")
            for cb in self._error_callbacks:
                try:
                    cb(exc)
                except Exception:
                    pass

    def __repr__(self) -> str:
        mode = (
            f"every={self._interval_s}s"
            if self._interval_s
            else f"daily_at={self._daily_time}"
        )
        return f"ChaosScheduler({self.scenario.name!r}, {mode})"
