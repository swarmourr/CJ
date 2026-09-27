"""Base exporter interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chaos_jungle.core.runner import MeasurementResult


class Exporter(ABC):
    """Base class for all observability exporters.

    Subclass and implement :meth:`export` to push
    :class:`~chaos_jungle.core.runner.MeasurementResult` data to any external system.
    """

    @abstractmethod
    def export(self, result: "MeasurementResult") -> None:
        """Push *result* metrics to the external system.

        Parameters
        ----------
        result : MeasurementResult
            The result of a ``ChaosRunner.measure()`` call.
        """
