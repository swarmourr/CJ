"""BPF-based fault: silent network corruption via eBPF/XDP.

Unlike tc netem corrupt (which breaks the TCP checksum and causes
packet drops), this fault modifies payload bytes while keeping the
TCP checksum valid — the packet arrives intact at the receiver but
carries corrupted data. This is silent corruption.

The underlying mechanism is the flow_modify.c BPF program from the
original chaos-jungle project, loaded via the BCC toolkit.
"""

from __future__ import annotations
import os
import signal
import subprocess
from typing import TYPE_CHECKING

from chaos_jungle.faults.base import Fault

if TYPE_CHECKING:
    from chaos_jungle.targets.base import Target

# Default path to the original chaos-jungle BPF driver on the target
_XDP_FLOW_MODIFY = "~/chaos-jungle/bpf/bcc/xdp_flow_modify.py"


def iface_for_ip(ip: str, target: "Target") -> str:
    """Return the network interface name that owns the given IP address.

    Useful when you know the virtual IP of a network link (e.g. from a
    topology file) but need the interface name to pass to
    :class:`SilentNetworkCorrupt`.

    Parameters
    ----------
    ip :
        IPv4 address to look up, e.g. ``"10.100.1.2"``.
    target :
        Target to run the lookup on.

    Returns
    -------
    str
        Interface name, e.g. ``"eth1"``.

    Raises
    ------
    RuntimeError
        If no interface on the target has that IP.

    Examples
    --------
    ::

        from chaos_jungle.targets import SSHTarget
        from chaos_jungle.faults.bpf import iface_for_ip, SilentNetworkCorrupt

        target = SSHTarget("node1", user="ubuntu")
        iface = iface_for_ip("10.100.1.2", target)
        fault = SilentNetworkCorrupt(rate=5000, iface=iface)
    """
    _, stdout, _ = target.run(
        f"ip -o addr show | awk '$4 ~ /^{ip}(\\/|$)/ {{print $2; exit}}'"
    )
    iface = stdout.strip()
    if not iface:
        raise RuntimeError(
            f"iface_for_ip: no interface found with IP {ip!r} on target.\n"
            f"  Run 'ip addr show' on the target to list available interfaces."
        )
    return iface


class SilentNetworkCorrupt(Fault):
    """Silently corrupt network packet payloads using eBPF/XDP.

    Modifies TCP/UDP payload bytes while preserving the TCP checksum,
    so corrupted packets pass TCP validation but carry wrong data.
    This is the most realistic network fault — it mimics what real
    hardware faults (bad NICs, buggy switches) produce.

    The BPF program is loaded into the kernel on the target machine
    using the BCC toolkit and runs as a background process.

    Parameters
    ----------
    rate : int
        Mangle 1 out of every ``rate`` packets. E.g. ``rate=5000``
        means 1/5000 packets corrupted (~0.02%).
    hook : str
        Kernel hook to attach to. Either ``"tc"`` (default) or
        ``"xdp"``. XDP is faster but requires compatible NIC drivers.
    iface : str, optional
        Network interface name, e.g. ``"eth1"``. Auto-detected via
        ``ip route`` if neither ``iface`` nor ``link_ip`` is given.
    link_ip : str, optional
        Virtual IP address of the link to corrupt, e.g. ``"10.100.1.2"``.
        When given, the interface is resolved automatically via
        :func:`iface_for_ip` at start time — no need to know the
        interface name in advance. Mutually exclusive with ``iface``.
    flow_modify_path : str, optional
        Path to ``xdp_flow_modify.py`` on the target machine.

    Notes
    -----
    Requires on the target machine:

    * Linux kernel 4.15+ with BPF support
    * BCC toolkit: ``apt-get install bpfcc-tools python3-bpfcc``
    * The original chaos-jungle BPF code at ``flow_modify_path``

    This fault cannot be used with ``LocalTarget`` unless the current
    machine is Linux with BCC installed.

    Examples
    --------
    >>> fault = SilentNetworkCorrupt(rate=5000)
    >>> fault = SilentNetworkCorrupt(rate=1000, hook="xdp")
    >>> # Resolve interface from virtual link IP (IRIS-style topology)
    >>> fault = SilentNetworkCorrupt(rate=5000, link_ip="10.100.1.2")
    """

    dependencies = ["python3-bpfcc"]

    def __init__(
        self,
        rate: int = 5000,
        hook: str = "tc",
        iface: str = "",
        link_ip: str = "",
        flow_modify_path: str = _XDP_FLOW_MODIFY,
    ) -> None:
        if hook not in ("tc", "xdp"):
            raise ValueError(f"hook must be 'tc' or 'xdp', got {hook!r}")
        if iface and link_ip:
            raise ValueError(
                "SilentNetworkCorrupt: provide either 'iface' or 'link_ip', not both."
            )
        self.rate = rate
        self.hook = hook
        self.iface = iface
        self.link_ip = link_ip
        self.flow_modify_path = flow_modify_path
        self._pid_file = "/tmp/cj_bpf.pid"

    def start(self, target: "Target") -> None:
        if self.link_ip:
            iface = iface_for_ip(self.link_ip, target)
        else:
            iface = self.iface or self._detect_iface(target)
        hook_flag = "-t" if self.hook == "tc" else ""

        # Launch BPF program in background, write PID to file
        cmd = (
            f"sudo python3 {self.flow_modify_path} "
            f"-i {iface} {hook_flag} -r {self.rate} "
            f"& echo $! > {self._pid_file}"
        )
        target.run(cmd)

    def stop(self, target: "Target") -> None:
        # Kill the background BPF process
        target.sudo(
            f"if [ -f {self._pid_file} ]; then "
            f"  kill $(cat {self._pid_file}) 2>/dev/null || true; "
            f"  rm -f {self._pid_file}; "
            f"fi"
        )

    def revert(self, target: "Target") -> None:
        # BPF program unloads from kernel when the process exits
        pass

    def preflight(self, target: "Target", auto_install: bool = False) -> None:
        """Check BCC is installed; optionally install via apt-get."""
        from chaos_jungle.faults.base import PreflightError

        code, _, _ = target.run("dpkg -l python3-bpfcc 2>/dev/null | grep -q '^ii'")
        if code != 0:
            if auto_install:
                print("[preflight] Installing python3-bpfcc (BCC for BPF programs)")
                c, _, err = target.sudo("apt-get install -y bpfcc-tools python3-bpfcc")
                if c != 0:
                    raise PreflightError(f"Failed to install python3-bpfcc: {err.strip()}")
                print("[preflight] Installed: python3-bpfcc")
            else:
                raise PreflightError(
                    "SilentNetworkCorrupt preflight failed — missing on target:\n"
                    "  - 'python3-bpfcc'  (BCC toolkit for BPF programs)\n"
                    "Fix: run with auto_install=True  or  "
                    "apt-get install bpfcc-tools python3-bpfcc"
                )

        # Also check the BPF source file exists on target
        code, _, _ = target.run(f"test -f {self.flow_modify_path}")
        if code != 0:
            raise PreflightError(
                f"SilentNetworkCorrupt: BPF source not found at {self.flow_modify_path}\n"
                f"Fix: git clone https://github.com/RENCI-NRIG/chaos-jungle ~/chaos-jungle"
            )

    def _detect_iface(self, target: "Target") -> str:
        _, stdout, _ = target.run("ip route | grep default | awk '{print $5}' | head -1")
        iface = stdout.strip()
        if not iface:
            raise RuntimeError("Could not detect default network interface")
        return iface

    def _parameters(self) -> dict:
        return {"rate": self.rate, "hook": self.hook, "iface": self.iface, "link_ip": self.link_ip}
