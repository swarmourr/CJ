"""Network fault implementations using Linux tc netem and iptables."""

from __future__ import annotations
import re
import uuid
from typing import TYPE_CHECKING

from chaos_jungle.faults.base import Fault

if TYPE_CHECKING:
    from chaos_jungle.targets.base import Target

# e.g. "100ms", "1s", "500us", "1.5ms"
_TIME_RE = re.compile(r"^\d+(\.\d+)?(ms|us|s)$")
# e.g. "5%", "0.5%", "100%"
_RATE_RE = re.compile(r"^\d+(\.\d+)?%$")
# e.g. "1mbit", "512kbit", "10mbps"
_BW_RE   = re.compile(r"^\d+(\.\d+)?(kbit|mbit|gbit|kbps|mbps|gbps|bps)$", re.IGNORECASE)


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


def _require_bandwidth(value: str, name: str) -> None:
    if not value or not _BW_RE.match(value.strip()):
        raise ValueError(
            f"{name!r} must be a bandwidth like '1mbit', '512kbit', '10mbps' — got {value!r}"
        )


def _all_ifaces(target: "Target") -> list[str]:
    """Return all non-loopback UP interfaces on the target."""
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
    """

    dependencies    = ["iproute2"]
    default_metrics = ["duration_s", "error_rate", "timeout_rate",
                       "rtt_ms", "p50_latency_ms", "p99_latency_ms"]

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
            # Use 'replace' so an existing qdisc is overwritten instead of failing silently
            target.sudo(f"tc qdisc replace dev {iface} root netem delay {self.delay}{jitter_part}")

    def stop(self, target: "Target") -> None:
        for iface in self._ifaces(target):
            target.sudo(f"tc qdisc del dev {iface} root 2>/dev/null || true")

    def revert(self, target: "Target") -> None:
        pass

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

    dependencies    = ["iproute2"]
    default_metrics = ["duration_s", "error_rate", "packet_loss_rate", "retransmissions"]

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
            target.sudo(f"tc qdisc replace dev {iface} root netem loss {self.rate}")

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

    dependencies    = ["iproute2"]
    default_metrics = ["duration_s", "error_rate", "parse_errors", "checksum_errors"]

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
            target.sudo(f"tc qdisc replace dev {iface} root netem corrupt {self.rate}")

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

    dependencies    = ["iproute2"]
    default_metrics = ["duration_s", "error_rate", "throughput_bps", "bandwidth_wasted_bytes"]

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
            target.sudo(f"tc qdisc replace dev {iface} root netem duplicate {self.rate}")

    def stop(self, target: "Target") -> None:
        for iface in self._ifaces(target):
            target.sudo(f"tc qdisc del dev {iface} root 2>/dev/null || true")

    def revert(self, target: "Target") -> None:
        pass

    def _parameters(self) -> dict:
        return {"rate": self.rate, "iface": ",".join(self._resolved_ifaces) or self.iface}


class NetworkBandwidthLimit(Fault):
    """Throttle interface bandwidth using tc netem rate.

    Limits outgoing traffic on the interface to ``rate``, simulating a
    congested or low-bandwidth link (e.g. a saturated uplink or a WAN
    with constrained capacity).

    Parameters
    ----------
    rate : str
        Maximum bandwidth, e.g. ``"1mbit"``, ``"512kbit"``, ``"10mbps"``.
    iface : str, optional
        Network interface. Auto-detected if not given.

    Examples
    --------
    >>> fault = NetworkBandwidthLimit("1mbit")
    >>> fault = NetworkBandwidthLimit("512kbit", iface="eth0")
    """

    dependencies    = ["iproute2"]
    default_metrics = ["duration_s", "throughput_bps", "p99_latency_ms", "error_rate"]

    def __init__(self, rate: str, iface: str = "") -> None:
        _require_bandwidth(rate, "rate")
        self.rate = rate
        self.iface = iface
        self._resolved_ifaces: list[str] = []

    def _ifaces(self, target: "Target") -> list[str]:
        if not self._resolved_ifaces:
            self._resolved_ifaces = [self.iface] if self.iface else _all_ifaces(target)
        return self._resolved_ifaces

    def start(self, target: "Target") -> None:
        for iface in self._ifaces(target):
            target.sudo(f"tc qdisc replace dev {iface} root netem rate {self.rate}")

    def stop(self, target: "Target") -> None:
        for iface in self._ifaces(target):
            target.sudo(f"tc qdisc del dev {iface} root 2>/dev/null || true")

    def revert(self, target: "Target") -> None:
        pass

    def _parameters(self) -> dict:
        return {"rate": self.rate, "iface": ",".join(self._resolved_ifaces) or self.iface}


class NetworkReorder(Fault):
    """Deliver a percentage of packets out of order using tc netem reorder.

    A base ``delay`` is required because netem reorder works by delaying
    most packets and letting the reordered fraction through immediately.
    The ``rate`` controls how many packets are sent out of order.

    Parameters
    ----------
    rate : str
        Fraction of packets to reorder, e.g. ``"25%"``.
    delay : str, optional
        Base queuing delay applied to the remaining packets so that
        reordering is visible. Default ``"50ms"``.
    iface : str, optional
        Network interface. Auto-detected if not given.

    Examples
    --------
    >>> fault = NetworkReorder("25%")
    >>> fault = NetworkReorder("50%", delay="100ms", iface="eth0")
    """

    dependencies    = ["iproute2"]
    default_metrics = ["duration_s", "error_rate", "retransmissions", "p99_latency_ms"]

    def __init__(self, rate: str, delay: str = "50ms", iface: str = "") -> None:
        _require_rate(rate, "rate")
        _require_time(delay, "delay")
        self.rate = rate
        self.delay = delay
        self.iface = iface
        self._resolved_ifaces: list[str] = []

    def _ifaces(self, target: "Target") -> list[str]:
        if not self._resolved_ifaces:
            self._resolved_ifaces = [self.iface] if self.iface else _all_ifaces(target)
        return self._resolved_ifaces

    def start(self, target: "Target") -> None:
        for iface in self._ifaces(target):
            target.sudo(
                f"tc qdisc replace dev {iface} root netem "
                f"delay {self.delay} reorder {self.rate}"
            )

    def stop(self, target: "Target") -> None:
        for iface in self._ifaces(target):
            target.sudo(f"tc qdisc del dev {iface} root 2>/dev/null || true")

    def revert(self, target: "Target") -> None:
        pass

    def _parameters(self) -> dict:
        return {
            "rate": self.rate,
            "delay": self.delay,
            "iface": ",".join(self._resolved_ifaces) or self.iface,
        }


class NetworkReset(Fault):
    """Inject TCP RST packets to simulate abrupt connection termination.

    Uses ``iptables REJECT --reject-with tcp-reset`` to drop connections
    on the specified port(s).  The rule is tracked by a unique iptables
    comment so ``stop()`` removes exactly the rule this instance added,
    even when other iptables rules are present.

    Requires: ``iptables`` (Linux only).

    Parameters
    ----------
    dport : int, optional
        Destination TCP port to target, e.g. ``443`` for HTTPS.
        ``0`` matches all ports. Default ``0``.
    sport : int, optional
        Source TCP port to target. ``0`` matches all ports. Default ``0``.
    direction : ``"OUTPUT"`` | ``"INPUT"`` | ``"both"``
        Which iptables chain(s) to insert the rule into. Default ``"OUTPUT"``.

    Examples
    --------
    >>> # Reset all outgoing HTTPS connections
    >>> fault = NetworkReset(dport=443)

    >>> # Reset connections on a custom LLM gateway port
    >>> fault = NetworkReset(dport=8080)
    """

    dependencies    = ["iptables"]
    danger_level    = 2
    default_metrics = ["duration_s", "error_rate", "connection_resets", "timeout_rate"]

    def __init__(
        self,
        dport: int = 0,
        sport: int = 0,
        direction: str = "OUTPUT",
    ) -> None:
        if direction not in ("OUTPUT", "INPUT", "both"):
            raise ValueError(
                f"NetworkReset 'direction' must be 'OUTPUT', 'INPUT', or 'both', got {direction!r}."
            )
        self.dport = dport
        self.sport = sport
        self.direction = direction
        self._comment = f"cj-reset-{uuid.uuid4().hex[:8]}"

    def _port_flags(self) -> str:
        parts = []
        if self.dport:
            parts.append(f"--dport {self.dport}")
        if self.sport:
            parts.append(f"--sport {self.sport}")
        return " ".join(parts)

    def _rule(self, chain: str) -> str:
        port_flags = self._port_flags()
        port_part  = f" -p tcp {port_flags}" if port_flags else " -p tcp"
        return (
            f"iptables -I {chain}{port_part} "
            f"-j REJECT --reject-with tcp-reset "
            f"-m comment --comment {self._comment!r}"
        )

    def _del_rule(self, chain: str) -> str:
        port_flags = self._port_flags()
        port_part  = f" -p tcp {port_flags}" if port_flags else " -p tcp"
        return (
            f"iptables -D {chain}{port_part} "
            f"-j REJECT --reject-with tcp-reset "
            f"-m comment --comment {self._comment!r} 2>/dev/null || true"
        )

    def _chains(self) -> list[str]:
        if self.direction == "both":
            return ["OUTPUT", "INPUT"]
        return [self.direction]

    def start(self, target: "Target") -> None:
        for chain in self._chains():
            target.sudo(self._rule(chain))

    def stop(self, target: "Target") -> None:
        for chain in self._chains():
            target.sudo(self._del_rule(chain))

    def revert(self, target: "Target") -> None:
        self.stop(target)

    def _parameters(self) -> dict:
        return {
            "dport": self.dport,
            "sport": self.sport,
            "direction": self.direction,
            "comment": self._comment,
        }


class NetworkPartition(Fault):
    """Block all traffic to/from a specific IP to simulate a network partition.

    Inserts ``iptables DROP`` rules for both outgoing and incoming traffic
    to/from ``dest_ip``.  Rules are tagged with a unique comment so
    ``stop()`` removes exactly the rules this instance added.

    Requires: ``iptables`` (Linux only).

    Parameters
    ----------
    dest_ip : str
        IP address (or CIDR) to partition from, e.g. ``"10.0.1.5"`` or
        ``"10.0.0.0/24"``.
    block_input : bool, optional
        Also block incoming traffic from ``dest_ip``. Default ``True``.

    Examples
    --------
    >>> # Partition from a specific service node
    >>> fault = NetworkPartition("10.0.1.5")

    >>> # Block only outgoing traffic (one-way partition)
    >>> fault = NetworkPartition("10.0.1.5", block_input=False)
    """

    dependencies    = ["iptables"]
    danger_level    = 2
    default_metrics = ["duration_s", "error_rate", "connection_refused", "timeout_rate"]

    def __init__(self, dest_ip: str, block_input: bool = True) -> None:
        if not dest_ip or not dest_ip.strip():
            raise ValueError("NetworkPartition requires a non-empty 'dest_ip'.")
        self.dest_ip     = dest_ip.strip()
        self.block_input = block_input
        self._comment    = f"cj-partition-{uuid.uuid4().hex[:8]}"

    def start(self, target: "Target") -> None:
        target.sudo(
            f"iptables -I OUTPUT -d {self.dest_ip} -j DROP "
            f"-m comment --comment {self._comment!r}"
        )
        if self.block_input:
            target.sudo(
                f"iptables -I INPUT -s {self.dest_ip} -j DROP "
                f"-m comment --comment {self._comment!r}"
            )

    def stop(self, target: "Target") -> None:
        target.sudo(
            f"iptables -D OUTPUT -d {self.dest_ip} -j DROP "
            f"-m comment --comment {self._comment!r} 2>/dev/null || true"
        )
        if self.block_input:
            target.sudo(
                f"iptables -D INPUT -s {self.dest_ip} -j DROP "
                f"-m comment --comment {self._comment!r} 2>/dev/null || true"
            )

    def revert(self, target: "Target") -> None:
        self.stop(target)

    def _parameters(self) -> dict:
        return {
            "dest_ip": self.dest_ip,
            "block_input": self.block_input,
            "comment": self._comment,
        }
