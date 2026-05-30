"""Network fault implementations using Linux tc netem."""

from __future__ import annotations
from typing import TYPE_CHECKING

from chaos_jungle.faults.base import Fault

if TYPE_CHECKING:
    from chaos_jungle.targets.base import Target


def _default_iface(target: "Target") -> str:
    """Detect the default network interface on the target machine."""
    _, stdout, _ = target.run("ip route | grep default | awk '{print $5}' | head -1")
    iface = stdout.strip()
    if not iface:
        raise RuntimeError("Could not detect default network interface on target")
    return iface


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
        self.delay = delay
        self.jitter = jitter
        self.iface = iface

    def start(self, target: "Target") -> None:
        iface = self.iface or _default_iface(target)
        jitter_part = f" {self.jitter}" if self.jitter else ""
        target.sudo(f"tc qdisc add dev {iface} root netem delay {self.delay}{jitter_part}")

    def stop(self, target: "Target") -> None:
        iface = self.iface or _default_iface(target)
        target.sudo(f"tc qdisc del dev {iface} root 2>/dev/null || true")

    def revert(self, target: "Target") -> None:
        pass  # stateless — stop() is sufficient

    def _parameters(self) -> dict:
        return {"delay": self.delay, "jitter": self.jitter, "iface": self.iface}


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
        self.rate = rate
        self.iface = iface

    def start(self, target: "Target") -> None:
        iface = self.iface or _default_iface(target)
        target.sudo(f"tc qdisc add dev {iface} root netem loss {self.rate}")

    def stop(self, target: "Target") -> None:
        iface = self.iface or _default_iface(target)
        target.sudo(f"tc qdisc del dev {iface} root 2>/dev/null || true")

    def revert(self, target: "Target") -> None:
        pass

    def _parameters(self) -> dict:
        return {"rate": self.rate, "iface": self.iface}


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
        self.rate = rate
        self.iface = iface

    def start(self, target: "Target") -> None:
        iface = self.iface or _default_iface(target)
        target.sudo(f"tc qdisc add dev {iface} root netem corrupt {self.rate}")

    def stop(self, target: "Target") -> None:
        iface = self.iface or _default_iface(target)
        target.sudo(f"tc qdisc del dev {iface} root 2>/dev/null || true")

    def revert(self, target: "Target") -> None:
        pass

    def _parameters(self) -> dict:
        return {"rate": self.rate, "iface": self.iface}


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
        self.rate = rate
        self.iface = iface

    def start(self, target: "Target") -> None:
        iface = self.iface or _default_iface(target)
        target.sudo(f"tc qdisc add dev {iface} root netem duplicate {self.rate}")

    def stop(self, target: "Target") -> None:
        iface = self.iface or _default_iface(target)
        target.sudo(f"tc qdisc del dev {iface} root 2>/dev/null || true")

    def revert(self, target: "Target") -> None:
        pass

    def _parameters(self) -> dict:
        return {"rate": self.rate, "iface": self.iface}
