"""Tests for fault-specific verify_active() and verify_recovered() methods."""
from __future__ import annotations
import pytest
from tests.conftest import MockTarget


# ── Network faults ────────────────────────────────────────────────────────────

class TestNetworkDelayVerification:
    def test_verify_active_returns_true_when_netem_present(self):
        from chaos_jungle.faults.network import NetworkDelay
        target = MockTarget(responses={
            "tc qdisc show": (0, "qdisc netem 8001: dev eth0 root refcnt 2 limit 1000 delay 100ms", ""),
        })
        fault = NetworkDelay("100ms", iface="eth0")
        fault._resolved_ifaces = ["eth0"]

        result = fault.verify_active(target)
        assert result.verified is True
        assert "netem" in result.reason.lower() or result.verified

    def test_verify_active_returns_false_when_no_netem(self):
        from chaos_jungle.faults.network import NetworkDelay
        target = MockTarget(responses={
            "tc qdisc show": (0, "qdisc pfifo_fast 0: dev eth0 root refcnt 2 bands 3", ""),
        })
        fault = NetworkDelay("100ms", iface="eth0")
        fault._resolved_ifaces = ["eth0"]

        result = fault.verify_active(target)
        assert result.verified is False

    def test_verify_recovered_returns_true_when_no_netem(self):
        from chaos_jungle.faults.network import NetworkDelay
        target = MockTarget(responses={
            "tc qdisc show": (0, "qdisc pfifo_fast 0: dev eth0", ""),
        })
        fault = NetworkDelay("100ms", iface="eth0")
        fault._resolved_ifaces = ["eth0"]

        result = fault.verify_recovered(target)
        assert result.verified is True

    def test_verify_recovered_returns_false_when_netem_still_present(self):
        from chaos_jungle.faults.network import NetworkDelay
        target = MockTarget(responses={
            "tc qdisc show": (0, "qdisc netem 8001: dev eth0 root refcnt 2", ""),
        })
        fault = NetworkDelay("100ms", iface="eth0")
        fault._resolved_ifaces = ["eth0"]

        result = fault.verify_recovered(target)
        assert result.verified is False

    def test_verification_result_has_timestamp(self):
        from chaos_jungle.faults.network import NetworkDelay
        import time
        target = MockTarget(responses={
            "tc qdisc show": (0, "qdisc pfifo_fast 0: dev eth0", ""),
        })
        fault = NetworkDelay("50ms", iface="eth0")
        fault._resolved_ifaces = ["eth0"]

        before = time.time()
        result = fault.verify_active(target)
        after = time.time()

        assert before <= result.timestamp_s <= after

    def test_verify_active_stores_observed_output(self):
        from chaos_jungle.faults.network import NetworkDelay
        tc_output = "qdisc netem 8001: dev eth0 root delay 100ms"
        target = MockTarget(responses={"tc qdisc show": (0, tc_output, "")})
        fault = NetworkDelay("100ms", iface="eth0")
        fault._resolved_ifaces = ["eth0"]

        result = fault.verify_active(target)
        assert result.observed  # should not be empty


class TestNetworkLossVerification:
    def test_verify_active_returns_true_when_loss_netem_present(self):
        from chaos_jungle.faults.network import NetworkLoss
        target = MockTarget(responses={
            "tc qdisc show": (0, "qdisc netem 8001: dev eth0 root loss 5%", ""),
        })
        fault = NetworkLoss("5%", iface="eth0")
        fault._resolved_ifaces = ["eth0"]

        result = fault.verify_active(target)
        assert result.verified is True


# ── Resource faults ───────────────────────────────────────────────────────────

class TestCPUStressVerification:
    def test_verify_active_returns_true_when_process_running(self):
        from chaos_jungle.faults.resources import CPUStress
        target = MockTarget(responses={
            "test -f": (0, "", ""),           # pid file exists
            "cat ": (0, "12345", ""),         # pid file contains pid
            "kill -0": (0, "", ""),           # process is alive
        })
        fault = CPUStress(cores=1)
        fault._pid_file = "/tmp/cj_cpu.pid"

        result = fault.verify_active(target)
        assert result.verified is True

    def test_verify_active_returns_false_when_no_pid_file(self):
        from chaos_jungle.faults.resources import CPUStress
        target = MockTarget(responses={
            "test -f": (1, "", ""),  # pid file does not exist
        })
        fault = CPUStress(cores=1)
        fault._pid_file = "/tmp/cj_cpu.pid"

        result = fault.verify_active(target)
        assert result.verified is False

    def test_verify_recovered_returns_true_when_no_pid_file(self):
        from chaos_jungle.faults.resources import CPUStress
        target = MockTarget(responses={
            "test -f": (1, "", ""),  # pid file gone
        })
        fault = CPUStress(cores=1)
        fault._pid_file = "/tmp/cj_cpu.pid"

        result = fault.verify_recovered(target)
        assert result.verified is True

    def test_verify_recovered_returns_false_when_process_still_running(self):
        from chaos_jungle.faults.resources import CPUStress
        target = MockTarget(responses={
            "test -f": (0, "", ""),    # pid file still exists
            "cat ": (0, "12345", ""), # pid in file
            "kill -0": (0, "", ""),   # process still alive
        })
        fault = CPUStress(cores=1)
        fault._pid_file = "/tmp/cj_cpu.pid"

        result = fault.verify_recovered(target)
        assert result.verified is False


class TestDiskFullVerification:
    def test_verify_active_returns_true_when_fill_file_exists(self):
        from chaos_jungle.faults.resources import DiskFull
        target = MockTarget(responses={
            "test -f": (0, "", ""),  # fill file exists
        })
        fault = DiskFull(path="/tmp", size_mb=10)

        result = fault.verify_active(target)
        assert result.verified is True

    def test_verify_active_returns_false_when_no_fill_file(self):
        from chaos_jungle.faults.resources import DiskFull
        target = MockTarget(responses={
            "test -f": (1, "", ""),  # fill file missing
        })
        fault = DiskFull(path="/tmp", size_mb=10)

        result = fault.verify_active(target)
        assert result.verified is False

    def test_verify_recovered_returns_true_when_fill_file_gone(self):
        from chaos_jungle.faults.resources import DiskFull
        target = MockTarget(responses={
            "test -f": (1, "", ""),  # fill file removed
        })
        fault = DiskFull(path="/tmp", size_mb=10)

        result = fault.verify_recovered(target)
        assert result.verified is True


# ── Process faults ────────────────────────────────────────────────────────────

class TestServiceFaultVerification:
    def test_verify_active_returns_true_when_service_stopped(self):
        from chaos_jungle.faults.process import ServiceFault
        target = MockTarget(responses={
            "systemctl is-active": (3, "inactive", ""),
        })
        fault = ServiceFault("nginx", action="stop")

        result = fault.verify_active(target)
        assert result.verified is True

    def test_verify_active_returns_false_when_service_still_running(self):
        from chaos_jungle.faults.process import ServiceFault
        target = MockTarget(responses={
            "systemctl is-active": (0, "active", ""),
        })
        fault = ServiceFault("nginx", action="stop")

        result = fault.verify_active(target)
        assert result.verified is False

    def test_verify_recovered_returns_true_when_service_active(self):
        from chaos_jungle.faults.process import ServiceFault
        target = MockTarget(responses={
            "systemctl is-active": (0, "active", ""),
        })
        fault = ServiceFault("nginx", action="stop")
        fault._was_active = True

        result = fault.verify_recovered(target)
        assert result.verified is True

    def test_verify_recovered_returns_false_when_service_still_down(self):
        from chaos_jungle.faults.process import ServiceFault
        target = MockTarget(responses={
            "systemctl is-active": (3, "inactive", ""),
        })
        fault = ServiceFault("nginx", action="stop")
        fault._was_active = True

        result = fault.verify_recovered(target)
        assert result.verified is False


class TestProcessKillVerification:
    def test_verify_active_returns_true_when_process_gone(self):
        """After a kill, the process should not be running."""
        from chaos_jungle.faults.process import ProcessKill
        target = MockTarget(responses={
            "pgrep -f": (1, "", ""),  # no matching process
        })
        fault = ProcessKill("gunicorn")

        result = fault.verify_active(target)
        # Process is gone = injection succeeded
        assert result.verified is True

    def test_verify_active_returns_false_when_process_still_running(self):
        """If the process is still running, the kill failed."""
        from chaos_jungle.faults.process import ProcessKill
        target = MockTarget(responses={
            "pgrep -f": (0, "12345\n67890", ""),
        })
        fault = ProcessKill("gunicorn")

        result = fault.verify_active(target)
        assert result.verified is False
