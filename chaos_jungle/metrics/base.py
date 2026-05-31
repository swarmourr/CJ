"""Abstract base class for chaos-jungle metrics collectors."""

from __future__ import annotations
from abc import ABC, abstractmethod

from chaos_jungle.targets.base import Target


class Metric(ABC):
    """Base class for a metric collector.

    Subclass this to define a custom measurement. Implement
    :meth:`collect` to run whatever measurement you need and return a
    plain ``dict`` of ``{key: value}`` pairs.

    The key names you return become columns in the exported CSV and
    appear in the dashboard under the session's metrics panel, prefixed
    with ``baseline_`` or ``chaos_`` depending on when they were
    collected.

    Parameters
    ----------
    name : str
        Short identifier used as a prefix for the result keys.
        For example, a metric named ``"ping"`` returning
        ``{"avg_ms": 1.2}`` will appear in results as
        ``{"baseline_ping_avg_ms": 0.2, "chaos_ping_avg_ms": 108.6}``.

    Examples
    --------
    Define a custom metric that counts open TCP connections:

    .. code-block:: python

        from chaos_jungle.metrics import Metric
        from chaos_jungle.targets.base import Target

        class OpenConnections(Metric):
            name = "tcp"

            def collect(self, target: Target) -> dict:
                _, out, _ = target.run("ss -tn | grep ESTAB | wc -l")
                try:
                    return {"open_connections": int(out.strip())}
                except ValueError:
                    return {"open_connections": 0}

    Use it with ``@chaos_measure``:

    .. code-block:: python

        from chaos_jungle.decorators import chaos_measure
        from chaos_jungle.faults import NetworkDelay

        @chaos_measure(
            NetworkDelay("100ms"),
            metrics=[OpenConnections()],
        )
        def run():
            run_pipeline()
    """

    #: Short identifier used to prefix result keys. Override in subclass.
    name: str = "metric"

    @abstractmethod
    def collect(self, target: Target) -> dict:
        """Run the measurement and return a result dict.

        Parameters
        ----------
        target : Target
            The target machine to measure. Use ``target.run(cmd)`` to
            execute shell commands on it.

        Returns
        -------
        dict
            Flat dict of ``{key: value}`` pairs. Values should be
            numbers or strings. Keys must not contain spaces.
        """

    def _prefixed(self, phase: str) -> dict:
        """Return keys prefixed with ``{phase}_{name}_``."""
        result = self.collect.__func__(self, None) if False else {}
        return result

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"
