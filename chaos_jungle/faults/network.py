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


def _default_iface(target: "Target") -> str:
    """Detect the default network interface on the target machine.

    Tries three methods in order, returning the first that works:

    1. ``ip route show default`` — standard Linux routing table
    2. ``ip route get 8.8.8.8`` — interface used to reach the internet
    3. ``ip link show`` — first non-loopback interface that is UP

    Raises ``RuntimeError`` only if all three methods fail.
    """
    # Method 1: default route
    _, out, _ = target.run(
        "ip route show default 2>/dev/null | awk '/default/{print $5}' | head -1"
    )
    iface = out.strip()
    if iface:
        return iface

    # Method 2: route to external IP
    _, out, _ = target.run(
        "ip route get 8.8.8.8 2>/dev/null | awk '/dev/{for(i=1;i<=NF;i++) if($i==\"dev\") print $(i+1)}' | head -1"
    )
    iface = out.strip()
    if iface:
        return iface

    # Method 3: first non-loopback UP interface
    _, out, _ = target.run(
        "ip link show 2>/dev/null | awk -F': ' '/^[0-9]+:/{dev=$2} /UP/{if(dev && dev!=\"lo\"){print dev; exit}}'"
    )
    iface = out.strip().split("@")[0]  # strip alias suffix e.g. eth0@if5 → eth0
    if iface and iface != "lo":
        return iface

    raise RuntimeError(
        "Could not detect a network interface on the target.\n"
        "  Fix A: pass iface= explicitly, e.g. NetworkDelay('100ms', iface='eth0')\n"
        "  Fix B: run 'ip link show' on the target and check interface names."
    )


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
        self._resolved_iface: str = ""

    def _iface(self, target: "Target") -> str:
        if not self._resolved_iface:
            self._resolved_iface = self.iface or _default_iface(target)
        return self._resolved_iface

    def start(self, target: "Target") -> None:
        iface = self._iface(target)
        jitter_part = f" {self.jitter}" if self.jitter else ""
        target.sudo(f"tc qdisc add dev {iface} root netem delay {self.delay}{jitter_part}")

    def stop(self, target: "Target") -> None:
        target.sudo(f"tc qdisc del dev {self._iface(target)} root 2>/dev/null || true")

    def revert(self, target: "Target") -> None:
        pass  # stateless — stop() is sufficient

    def _parameters(self) -> dict:
        return {"delay": self.delay, "jitter": self.jitter, "iface": self._resolved_iface or self.iface}


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
        self._resolved_iface: str = ""

    def _iface(self, target: "Target") -> str:
        if not self._resolved_iface:
            self._resolved_iface = self.iface or _default_iface(target)
        return self._resolved_iface

    def start(self, target: "Target") -> None:
        target.sudo(f"tc qdisc add dev {self._iface(target)} root netem loss {self.rate}")

    def stop(self, target: "Target") -> None:
        target.sudo(f"tc qdisc del dev {self._iface(target)} root 2>/dev/null || true")

    def revert(self, target: "Target") -> None:
        pass

    def _parameters(self) -> dict:
        return {"rate": self.rate, "iface": self._resolved_iface or self.iface}


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
        self._resolved_iface: str = ""

    def _iface(self, target: "Target") -> str:
        if not self._resolved_iface:
            self._resolved_iface = self.iface or _default_iface(target)
        return self._resolved_iface

    def start(self, target: "Target") -> None:
        target.sudo(f"tc qdisc add dev {self._iface(target)} root netem corrupt {self.rate}")

    def stop(self, target: "Target") -> None:
        target.sudo(f"tc qdisc del dev {self._iface(target)} root 2>/dev/null || true")

    def revert(self, target: "Target") -> None:
        pass

    def _parameters(self) -> dict:
        return {"rate": self.rate, "iface": self._resolved_iface or self.iface}


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
        self._resolved_iface: str = ""

    def _iface(self, target: "Target") -> str:
        if not self._resolved_iface:
            self._resolved_iface = self.iface or _default_iface(target)
        return self._resolved_iface

    def start(self, target: "Target") -> None:
        target.sudo(f"tc qdisc add dev {self._iface(target)} root netem duplicate {self.rate}")

    def stop(self, target: "Target") -> None:
        target.sudo(f"tc qdisc del dev {self._iface(target)} root 2>/dev/null || true")

    def revert(self, target: "Target") -> None:
        pass

    def _parameters(self) -> dict:
        return {"rate": self.rate, "iface": self._resolved_iface or self.iface}
