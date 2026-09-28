"""Clock offset estimation between the coordinator and remote targets.

CJ uses NTP-style one-way probes to estimate clock skew between the
controller and each injection target. For most agent experiments, a
50–200 ms tolerance is sufficient; sub-millisecond accuracy requires
PTP or Chrony on the target hosts.

The probe records:

* ``rtt_ms``              — TCP round-trip time.
* ``estimated_offset_ms`` — positive = target clock is ahead of controller.
* ``scheduling_error_ms`` — expected error in a scheduled activation
                            (approximately rtt_ms / 2).
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class ClockProbe:
    """NTP-style clock offset estimate for one target."""

    target_host: str
    rtt_ms: float
    estimated_offset_ms: float
    scheduling_error_ms: float

    def to_dict(self) -> dict:
        return {
            "target_host": self.target_host,
            "rtt_ms": round(self.rtt_ms, 3),
            "estimated_offset_ms": round(self.estimated_offset_ms, 3),
            "scheduling_error_ms": round(self.scheduling_error_ms, 3),
        }

    @classmethod
    def local(cls) -> "ClockProbe":
        """Trivial probe for LocalTarget (zero offset, zero RTT)."""
        return cls(
            target_host="localhost",
            rtt_ms=0.0,
            estimated_offset_ms=0.0,
            scheduling_error_ms=0.0,
        )

    @classmethod
    def from_ssh(cls, target: object) -> "ClockProbe":
        """Probe clock offset via Paramiko SSH (date +%s%3N command).

        Uses the T1/T4 timestamps on the controller and the remote T2
        timestamp to estimate one-way offset:

            offset ≈ T2 − (T1 + rtt/2)

        Parameters
        ----------
        target :
            An SSHTarget instance (type-checked at runtime).
        """
        try:
            t1 = time.time()
            rc, stdout, _ = target.run("date +%s%3N")
            t4 = time.time()
            rtt_ms = (t4 - t1) * 1000.0

            remote_ms = int(stdout.strip())
            controller_mid_ms = (t1 + (t4 - t1) / 2) * 1000.0
            offset_ms = remote_ms - controller_mid_ms
            scheduling_error_ms = rtt_ms / 2.0

            return cls(
                target_host=getattr(target, "host", str(target)),
                rtt_ms=rtt_ms,
                estimated_offset_ms=offset_ms,
                scheduling_error_ms=scheduling_error_ms,
            )
        except Exception:
            host = getattr(target, "host", str(target))
            return cls(
                target_host=host,
                rtt_ms=-1.0,
                estimated_offset_ms=0.0,
                scheduling_error_ms=0.0,
            )

    @classmethod
    def from_http(cls, target: object) -> "ClockProbe":
        """Probe clock offset via HTTP health-check latency measurement."""
        import urllib.request

        url = getattr(target, "url", None) or getattr(target, "base_url", "")
        health_url = url.rstrip("/") + "/health"

        try:
            t1 = time.time()
            urllib.request.urlopen(health_url, timeout=5)
            t4 = time.time()
            rtt_ms = (t4 - t1) * 1000.0
            return cls(
                target_host=url,
                rtt_ms=rtt_ms,
                estimated_offset_ms=0.0,
                scheduling_error_ms=rtt_ms / 2.0,
            )
        except Exception:
            return cls(
                target_host=url,
                rtt_ms=-1.0,
                estimated_offset_ms=0.0,
                scheduling_error_ms=0.0,
            )

    @classmethod
    def for_target(cls, target: object) -> "ClockProbe":
        """Dispatch to the appropriate probe method for a target instance."""
        try:
            from chaos_jungle.targets.local import LocalTarget
            if isinstance(target, LocalTarget):
                return cls.local()
        except ImportError:
            pass
        try:
            from chaos_jungle.targets.ssh import SSHTarget
            if isinstance(target, SSHTarget):
                return cls.from_ssh(target)
        except ImportError:
            pass
        try:
            from chaos_jungle.targets.http import HTTPTarget
            if isinstance(target, HTTPTarget):
                return cls.from_http(target)
        except ImportError:
            pass
        return cls(
            target_host=str(target),
            rtt_ms=-1.0,
            estimated_offset_ms=0.0,
            scheduling_error_ms=0.0,
        )
