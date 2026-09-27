"""Tests for SafetyPolicy path/target validation and allowlist bypass prevention."""
from __future__ import annotations
import os
import tempfile

import pytest

from chaos_jungle.core.guardrails import SafetyPolicy, DangerError, _is_path_allowed


# ── _is_path_allowed unit tests ───────────────────────────────────────────────

class TestIsPathAllowed:
    def test_exact_match(self):
        assert _is_path_allowed("/tmp/foo", ["/tmp"]) is True

    def test_nested_path(self):
        assert _is_path_allowed("/tmp/a/b/c", ["/tmp"]) is True

    def test_prefix_trick_rejected(self):
        """/tmp-evil must NOT match /tmp allowlist."""
        assert _is_path_allowed("/tmp-evil", ["/tmp"]) is False

    def test_prefix_trick_with_dash(self):
        assert _is_path_allowed("/tmp-evil/x", ["/tmp"]) is False

    def test_dot_dot_traversal_blocked(self):
        """Path that resolves outside the allowed dir must be rejected."""
        # /tmp/../etc resolves to /etc which is not /tmp
        assert _is_path_allowed("/tmp/../etc/passwd", ["/tmp"]) is False

    def test_empty_allowlist_allows_all(self):
        """Empty allowlist means no restriction — all paths accepted."""
        assert _is_path_allowed("/etc/passwd", []) is True
        assert _is_path_allowed("/tmp", []) is True

    def test_multiple_allowed_paths(self):
        assert _is_path_allowed("/var/tmp/file", ["/tmp", "/var/tmp"]) is True
        assert _is_path_allowed("/home/user/file", ["/tmp", "/var/tmp"]) is False

    def test_symlink_in_path(self, tmp_path):
        """Symlink that resolves outside the allowed dir must be blocked."""
        # Create a symlink that points outside /tmp
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real_dir)

        allowed = str(tmp_path / "other")
        # The symlink resolves to real_dir which is NOT inside "other"
        assert _is_path_allowed(str(link / "file"), [allowed]) is False

    def test_root_slash_not_matched_by_tmp(self):
        assert _is_path_allowed("/", ["/tmp"]) is False

    def test_absolute_path_required_for_sensible_check(self):
        """Relative paths should not accidentally be allowed."""
        # relative path "tmp/foo" resolves relative to CWD — likely not in /tmp
        assert _is_path_allowed("tmp/foo", ["/tmp"]) is False


# ── SafetyPolicy.check_fault ─────────────────────────────────────────────────

class TestSafetyPolicyFault:
    def _make_fault(self, danger=0, path=None):
        class Fake:
            danger_level = danger
        f = Fake()
        if path:
            f.path = path
        return f

    def test_danger_level_too_high_raises(self):
        policy = SafetyPolicy(max_danger=0)
        fault = self._make_fault(danger=2)
        with pytest.raises(DangerError, match="danger_level"):
            policy.check_fault(fault)

    def test_danger_level_at_limit_allowed(self):
        policy = SafetyPolicy(max_danger=1)
        fault = self._make_fault(danger=1)
        policy.check_fault(fault)   # must not raise

    def test_path_outside_allowlist_raises(self):
        policy = SafetyPolicy(max_danger=2, allowed_paths=["/tmp"])
        fault = self._make_fault(danger=2, path="/etc/danger")
        with pytest.raises(DangerError, match="allowed_paths"):
            policy.check_fault(fault)

    def test_path_inside_allowlist_allowed(self):
        policy = SafetyPolicy(max_danger=2, allowed_paths=["/tmp"])
        fault = self._make_fault(danger=2, path="/tmp/safe")
        policy.check_fault(fault)  # must not raise

    def test_tmp_evil_rejected_when_tmp_in_allowlist(self):
        policy = SafetyPolicy(max_danger=2, allowed_paths=["/tmp"])
        fault = self._make_fault(danger=2, path="/tmp-evil/x")
        with pytest.raises(DangerError):
            policy.check_fault(fault)

    def test_no_path_attribute_passes_path_check(self):
        """Faults without a path attribute should not be blocked by path policy."""
        policy = SafetyPolicy(max_danger=2, allowed_paths=["/tmp"])
        fault = self._make_fault(danger=2, path=None)
        policy.check_fault(fault)  # no path = no restriction


# ── SafetyPolicy.check_target ─────────────────────────────────────────────────

class TestSafetyPolicyTarget:
    def _make_http_target(self, url):
        class Fake:
            pass
        t = Fake()
        t.__class__.__name__ = "HTTPTarget"
        t.url = url
        return t

    def _make_ssh_target(self, host):
        class Fake:
            pass
        t = Fake()
        t.__class__.__name__ = "SSHTarget"
        t.host = host
        return t

    def _make_local_target(self):
        class Fake:
            pass
        t = Fake()
        t.__class__.__name__ = "LocalTarget"
        return t

    def test_empty_allowlist_permits_all(self):
        policy = SafetyPolicy(allowed_targets=[])
        policy.check_target(self._make_ssh_target("worker1"))  # no raise

    def test_allowed_ssh_host_passes(self):
        policy = SafetyPolicy(allowed_targets=["worker1"])
        policy.check_target(self._make_ssh_target("worker1"))  # no raise

    def test_disallowed_ssh_host_raises(self):
        policy = SafetyPolicy(allowed_targets=["worker1"])
        with pytest.raises(DangerError, match="allowed_targets"):
            policy.check_target(self._make_ssh_target("evil-node"))

    def test_local_target_requires_local_in_allowlist(self):
        policy = SafetyPolicy(allowed_targets=["worker1"])
        with pytest.raises(DangerError, match="LocalTarget"):
            policy.check_target(self._make_local_target())

    def test_local_target_allowed_when_local_in_list(self):
        policy = SafetyPolicy(allowed_targets=["local"])
        policy.check_target(self._make_local_target())  # no raise

    def test_empty_ssh_host_fails_closed(self):
        policy = SafetyPolicy(allowed_targets=["worker1"])
        with pytest.raises(DangerError, match="(?i)cannot determine"):
            policy.check_target(self._make_ssh_target(""))

    def test_http_target_matched_by_hostname(self):
        policy = SafetyPolicy(allowed_targets=["worker1:7777"])
        policy.check_target(self._make_http_target("http://worker1:7777"))  # no raise

    def test_http_target_wrong_host_blocked(self):
        policy = SafetyPolicy(allowed_targets=["worker1:7777"])
        with pytest.raises(DangerError):
            policy.check_target(self._make_http_target("http://evil-node:7777"))
