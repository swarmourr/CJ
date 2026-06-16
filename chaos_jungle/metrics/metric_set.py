"""MetricSet — user-facing API for selecting which metrics to collect."""

from __future__ import annotations


class MetricSet:
    """Control which of a fault's ``default_metrics`` are collected.

    Build instances from the class-level sentinel ``MetricSet.DEFAULT``
    using the fluent API::

        # Use every metric declared in fault.default_metrics (the default)
        MetricSet.DEFAULT

        # Drop a few metrics you don't need
        MetricSet.DEFAULT.exclude("swap_used_mb", "context_switches")

        # Add extra metric names on top of the fault defaults
        MetricSet.DEFAULT.add("inode_used", "open_fds")

        # Ignore fault defaults entirely — collect only these
        MetricSet.only("error_rate", "duration_s")

    Instances are immutable; every method returns a new instance.

    Examples
    --------
    ::

        from chaos_jungle import ChaosRunner, Scenario
        from chaos_jungle.faults import NetworkDelay
        from chaos_jungle.metrics import MetricSet, CollectStrategy

        runner = ChaosRunner(Scenario("test", [NetworkDelay("200ms")]))
        result = runner.measure(
            workload,
            strategy=CollectStrategy.SNAPSHOT,
            metric_set=MetricSet.DEFAULT.exclude("swap_used_mb"),
        )
    """

    # Set after class definition
    DEFAULT: "MetricSet"

    def __init__(
        self,
        _use_defaults: bool = True,
        _extra: frozenset[str] = frozenset(),
        _exclude: frozenset[str] = frozenset(),
        _only: frozenset[str] | None = None,
    ) -> None:
        self._use_defaults = _use_defaults
        self._extra = _extra
        self._exclude = _exclude
        self._only = _only

    # ------------------------------------------------------------------
    # Core resolver
    # ------------------------------------------------------------------

    def resolve(self, fault_defaults: list[str]) -> list[str]:
        """Return the final ordered list of metric names to collect.

        Parameters
        ----------
        fault_defaults : list[str]
            The ``default_metrics`` declared on the fault class (or the
            union of all faults in a scenario).

        Returns
        -------
        list[str]
            Active metric names after applying include/exclude/only rules,
            sorted alphabetically for deterministic output.
        """
        if self._only is not None:
            return sorted(self._only)

        base = set(fault_defaults) if self._use_defaults else set()
        active = (base | self._extra) - self._exclude
        return sorted(active)

    # ------------------------------------------------------------------
    # Fluent builders
    # ------------------------------------------------------------------

    def exclude(self, *names: str) -> "MetricSet":
        """Return a new MetricSet that excludes *names* from the default set.

        Parameters
        ----------
        *names : str
            Metric names to suppress.

        Examples
        --------
        ::

            ms = MetricSet.DEFAULT.exclude("swap_used_mb", "context_switches")
        """
        return MetricSet(
            _use_defaults=self._use_defaults,
            _extra=self._extra,
            _exclude=self._exclude | frozenset(names),
            _only=self._only,
        )

    def add(self, *names: str) -> "MetricSet":
        """Return a new MetricSet that also collects *names* in addition to defaults.

        Parameters
        ----------
        *names : str
            Extra metric names to activate.

        Examples
        --------
        ::

            ms = MetricSet.DEFAULT.add("inode_used", "open_fds")
        """
        return MetricSet(
            _use_defaults=self._use_defaults,
            _extra=self._extra | frozenset(names),
            _exclude=self._exclude,
            _only=self._only,
        )

    @staticmethod
    def only(*names: str) -> "MetricSet":
        """Return a MetricSet that collects **only** *names*, ignoring fault defaults.

        Parameters
        ----------
        *names : str
            Exact metric names to collect.

        Examples
        --------
        ::

            ms = MetricSet.only("error_rate", "duration_s")
        """
        return MetricSet(_only=frozenset(names))

    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        if self._only is not None:
            return f"MetricSet.only({', '.join(repr(n) for n in sorted(self._only))})"
        parts = []
        if self._extra:
            parts.append(f"add={sorted(self._extra)!r}")
        if self._exclude:
            parts.append(f"exclude={sorted(self._exclude)!r}")
        suffix = f".({', '.join(parts)})" if parts else ""
        return f"MetricSet.DEFAULT{suffix}"


MetricSet.DEFAULT = MetricSet()
