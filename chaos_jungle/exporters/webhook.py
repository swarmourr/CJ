"""Generic webhook exporter — POST MeasurementResult as JSON to any HTTP endpoint.

No dependencies beyond the standard library.

Example::

    from chaos_jungle.exporters import WebhookExporter

    exporter = WebhookExporter(
        url="https://hooks.example.com/chaos",
        headers={"Authorization": "Bearer my-token"},
    )
    result = runner.measure(workload, n_baseline=5, n_fault=5)
    exporter.export(result)

Payload sent::

    {
        "scenario":   "wan-degraded",
        "session_id": 42,
        "n_baseline": 5,
        "n_fault":    5,
        "baseline":   {"duration_s": 0.12, ...},
        "fault":      {"duration_s": 0.34, ...},
        "delta":      {"duration_s": 0.22, ...},
        "passed":     true          # present only if hypothesis was checked
    }
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from typing import TYPE_CHECKING

from chaos_jungle.exporters.base import Exporter

if TYPE_CHECKING:
    from chaos_jungle.runner import MeasurementResult


class WebhookExporter(Exporter):
    """POST MeasurementResult as JSON to an HTTP endpoint.

    Parameters
    ----------
    url : str
        Destination URL, e.g. ``"https://hooks.example.com/chaos"``.
    headers : dict, optional
        Extra HTTP headers, e.g. ``{"Authorization": "Bearer token"}``.
    timeout_s : float
        Request timeout in seconds. Default ``10``.
    raise_on_error : bool
        Raise an exception if the server returns a non-2xx response.
        Default ``True``.
    """

    def __init__(
        self,
        url: str,
        headers: dict | None = None,
        timeout_s: float = 10.0,
        raise_on_error: bool = True,
    ) -> None:
        self.url = url
        self.headers = headers or {}
        self.timeout_s = timeout_s
        self.raise_on_error = raise_on_error

    def export(self, result: "MeasurementResult") -> None:
        payload: dict = {
            "scenario":   result.scenario,
            "session_id": result.session_id,
            "n_baseline": result.n_baseline,
            "n_fault":    result.n_fault,
            "baseline":   result.baseline,
            "fault":      result.fault,
            "delta":      result.delta,
        }

        # Include hypothesis result if present
        hr = getattr(result, "hypothesis_result", None)
        if hr is not None:
            payload["hypothesis"] = {
                "name":   hr.name,
                "passed": hr.passed,
                "assertions": [
                    {
                        "metric":   a.metric,
                        "passed":   a.passed,
                        "reason":   a.reason,
                        "expected": a.expected,
                        "actual":   a.actual,
                    }
                    for a in hr.assertions
                ],
            }

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        for k, v in self.headers.items():
            req.add_header(k, v)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            if self.raise_on_error:
                raise RuntimeError(
                    f"WebhookExporter: POST to {self.url} returned HTTP {status}"
                ) from exc
        except Exception as exc:
            if self.raise_on_error:
                raise RuntimeError(
                    f"WebhookExporter: failed to POST to {self.url}: {exc}"
                ) from exc
            status = -1

        print(f"[chaos-jungle] WebhookExporter: POST {self.url} -> HTTP {status}")
