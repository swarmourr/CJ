"""Network fault implementations using Linux tc netem."""

from __future__ import annotations
import re
from typing import TYPE_CHECKING

from chaos_jungle.faults.base import Fault

if TYPE_CHECKING:
    from chaos_jungle.targets.base import Target

# e.g. "100ms", "1s", "500us", "1.5ms"
_TIME_RE  = re.compile(r"^\d+(\.\d+)?(ms|us|s)$")
# e.g. "5%", "0.5%", "100%"
_RATE_RE  = re.compile(r"^\d+(\.\d+)?%$")


def _require_time(value: str, name: str) -> None:
    if not value or not _TIME_RE.match(value.strip()):
        raise ValueError(
            f"{name!r} must be a time value like '100ms', '1s', '500us' — got {value!r}"
        )


def _require_rate(value: str, name: str) -> None:
    if not value or not _RATE_RE.match(value.strip()):
        raise ValueError(
            f"{name!r} must be a percentage like '5%', '0.5%', '100%' — got {value!r}"
        )


def _all_ifaces(target: "Target") -> list[str]:
    """Return all non-loopback UP interfaces on the target.

    Used when no ``iface`` is specified — the fault is applied to every
    active interface so no traffic path is missed.

    Returns a list of interface names, e.g. ``["eth0", "eth1", "ens3"]``.
    Raises ``RuntimeError`` if no interfaces are found.
    """
    _, out, _ = target.run(
        "ip -o link show up 2>/dev/null | awk -F': ' '{print $2}' | cut -d@ -f1"
    )
    ifaces = [
        line.strip() for line in out.splitlines()
        if line.strip() and line.strip() != "lo"
    ]
    if not ifaces:
        raise RuntimeError(
            "Could not detect any network interfaces on the target.\n"
            "  Fix A: pass iface= explicitly, e.g. NetworkDelay('100ms', iface='eth0')\n"
            "  Fix B: run 'ip -o link show up' on the target to list available interfaces."
        )
    return ifaces


class NetworkDelay(Fault):
    """Inject artificial network delay using tc netem.

    Parameters
    ----------
    delay : str
        Base delay, e.g. ``"100ms"``.
    jitter : str, optional
        Variation around the base delay, e.g. ``"10ms"``.
    iface : str, optional
        Network interface to apply the rule on. Auto-detected if not given.

    Examples
    --------
    >>> fault = NetworkDelay("100ms", jitter="10ms")
    >>> runner = ChaosRunner(Scenario("test", [fault]), LocalTarget())
    >>> runner.start()
    """

    dependencies = ["iproute2"]

    def __init__(self, delay: str, jitter: str = "", iface: str = "") -> None:
        _require_time(delay, "delay")
        if jitter:
            _require_time(jitter, "jitter")
        self.delay = delay
        self.jitter = jitter
        self.iface = iface
        self._resolved_ifaces: list[str] = []

    def _ifaces(self, target: "Target") -> list[str]:
        if not self._resolved_ifaces:
            self._resolved_ifaces = [self.iface] if self.iface else _all_ifaces(target)
        return self._resolved_ifaces

    def start(self, target: "Target") -> None:
        jitter_part = f" {self.jitter}" if self.jitter else ""
        for iface in self._ifaces(target):
            target.sudo(f"tc qdisc add dev {iface} root netem delay {self.delay}{jitter_part}")

    def stop(self, target: "Target") -> None:
        for iface in self._ifaces(target):
            target.sudo(f"tc qdisc del dev {iface} root 2>/dev/null || true")

    def revert(self, target: "Target") -> None:
        pass  # stateless — stop() is sufficient

    def _parameters(self) -> dict:
        return {"delay": self.delay, "jitter": self.jitter, "iface": ",".join(self._resolved_ifaces) or self.iface}


class NetworkLoss(Fault):
    """Drop a percentage of packets using tc netem.

    Parameters
    ----------
    rate : str
        Packet loss rate, e.g. ``"5%"``.
    iface : str, optional
        Network interface. Auto-detected if not given.

    Examples
    --------
    >>> fault = NetworkLoss("5%")
    """

    dependencies = ["iproute2"]

    def __init__(self, rate: str, iface: str = "") -> None:
        _require_rate(rate, "rate")
        self.rate = rate
        self.iface = iface
        self._resolved_ifaces: list[str] = []

    def _ifaces(self, target: "Target") -> list[str]:
        if not self._resolved_ifaces:
            self._resolved_ifaces = [self.iface] if self.iface else _all_ifaces(target)
        return self._resolved_ifaces

    def start(self, target: "Target") -> None:
        for iface in self._ifaces(target):
            target.sudo(f"tc qdisc add dev {iface} root netem loss {self.rate}")

    def stop(self, target: "Target") -> None:
        for iface in self._ifaces(target):
            target.sudo(f"tc qdisc del dev {iface} root 2>/dev/null || true")

    def revert(self, target: "Target") -> None:
        pass

    def _parameters(self) -> dict:
        return {"rate": self.rate, "iface": ",".join(self._resolved_ifaces) or self.iface}


class NetworkCorrupt(Fault):
    """Corrupt a percentage of packets in transit using tc netem.

    Parameters
    ----------
    rate : str
        Corruption rate, e.g. ``"1%"``.
    iface : str, optional
        Network interface. Auto-detected if not given.

    Examples
    --------
    >>> fault = NetworkCorrupt("1%")
    """

    dependencies = ["iproute2"]

    def __init__(self, rate: str, iface: str = "") -> None:
        _require_rate(rate, "rate")
        self.rate = rate
        self.iface = iface
        self._resolved_ifaces: list[str] = []

    def _ifaces(self, target: "Target") -> list[str]:
        if not self._resolved_ifaces:
            self._resolved_ifaces = [self.iface] if self.iface else _all_ifaces(target)
        return self._resolved_ifaces

    def start(self, target: "Target") -> None:
        for iface in self._ifaces(target):
            target.sudo(f"tc qdisc add dev {iface} root netem corrupt {self.rate}")

    def stop(self, target: "Target") -> None:
        for iface in self._ifaces(target):
            target.sudo(f"tc qdisc del dev {iface} root 2>/dev/null || true")

    def revert(self, target: "Target") -> None:
        pass

    def _parameters(self) -> dict:
        return {"rate": self.rate, "iface": ",".join(self._resolved_ifaces) or self.iface}


class NetworkDuplicate(Fault):
    """Duplicate a percentage of packets using tc netem.

    Parameters
    ----------
    rate : str
        Duplication rate, e.g. ``"0.5%"``.
    iface : str, optional
        Network interface. Auto-detected if not given.

    Examples
    --------
    >>> fault = NetworkDuplicate("0.5%")
    """

    dependencies = ["iproute2"]

    def __init__(self, rate: str, iface: str = "") -> None:
        _require_rate(rate, "rate")
        self.rate = rate
        self.iface = iface
        self._resolved_ifaces: list[str] = []

    def _ifaces(self, target: "Target") -> list[str]:
        if not self._resolved_ifaces:
            self._resolved_ifaces = [self.iface] if self.iface else _all_ifaces(target)
        return self._resolved_ifaces

    def start(self, target: "Target") -> None:
        for iface in self._ifaces(target):
            target.sudo(f"tc qdisc add dev {iface} root netem duplicate {self.rate}")

    def stop(self, target: "Target") -> None:
        for iface in self._ifaces(target):
            target.sudo(f"tc qdisc del dev {iface} root 2>/dev/null || true")

    def revert(self, target: "Target") -> None:
        pass

    def _parameters(self) -> dict:
        return {"rate": self.rate, "iface": ",".join(self._resolved_ifaces) or self.iface}
