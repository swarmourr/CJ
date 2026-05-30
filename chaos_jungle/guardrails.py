"""Guardrails — conflict detection before chaos is injected.

Three validators run at different points:

* :class:`ScenarioValidator` — checks a single scenario for internal
  conflicts before ``ChaosRunner.start()`` is called.
* :class:`RuntimeValidator` — checks live state on the target machine
  (existing tc rules, running crontabs) before injecting.
* :class:`SuiteValidator` — checks an ``ExperimentSuite`` for
  cross-scenario conflicts before any parallel run begins.
"""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chaos_jungle.scenario import Scenario
    from chaos_jungle.targets.base import Target


# ── Exceptions ────────────────────────────────────────────────────

class ConflictError(RuntimeError):
    """Raised when two faults or scenarios conflict with each other.

    The message always includes a human-readable description of the
    conflict and one or more suggested fixes.
    """


class ConflictWarning(UserWarning):
    """Issued instead of raising when ``conflict="warn"`` is set."""


# ── Helpers ───────────────────────────────────────────────────────

def _fault_names(faults) -> str:
    return ", ".join(f.__class__.__name__ for f in faults)


def _is_tc_fault(fault) -> bool:
    from chaos_jungle.faults.network import NetworkDelay, NetworkLoss, NetworkCorrupt, NetworkDuplicate
    return isinstance(fault, (NetworkDelay, NetworkLoss, NetworkCorrupt, NetworkDuplicate))


def _is_bpf_fault(fault) -> bool:
    from chaos_jungle.faults.bpf import SilentNetworkCorrupt
    return isinstance(fault, SilentNetworkCorrupt)


def _is_storage_fault(fault) -> bool:
    from chaos_jungle.faults.storage import StorageCorrupt
    return isinstance(fault, StorageCorrupt)


def _iface_of(fault) -> str:
    """Return the iface of a fault, or '' if not set (auto-detect)."""
    return getattr(fault, "iface", "") or ""


def _dir_of(fault) -> str:
    return getattr(fault, "directory", "") or ""


# ── Level 1: Scenario validator ───────────────────────────────────

class ScenarioValidator:
    """Validate a single scenario for internal fault conflicts.

    Checks
    ------
    * Multiple tc netem faults on the same interface
    * BPF fault + tc netem fault on the same interface
    * Multiple storage faults on the same directory
    * SilentNetworkCorrupt + NetworkCorrupt on the same interface
    """

    def check(self, scenario: "Scenario") -> None:
        """Run all checks. Raises :exc:`ConflictError` on first conflict.

        Parameters
        ----------
        scenario :
            The scenario to validate.
        """
        self._check_tc_conflicts(scenario)
        self._check_bpf_tc_conflict(scenario)
        self._check_storage_conflicts(scenario)
        self._check_silent_vs_corrupt(scenario)

    # ── tc conflicts ──────────────────────────────────────────────

    def _check_tc_conflicts(self, scenario: "Scenario") -> None:
        tc_faults = [f for f in scenario.faults if _is_tc_fault(f)]
        if len(tc_faults) < 2:
            return

        # group by interface (empty string = auto-detect = same iface)
        by_iface: dict[str, list] = {}
        for f in tc_faults:
            iface = _iface_of(f)
            by_iface.setdefault(iface, []).append(f)

        for iface, faults in by_iface.items():
            if len(faults) > 1:
                iface_str = iface or "auto-detected default interface"
                names = _fault_names(faults)
                raise ConflictError(
                    f"Scenario '{scenario.name}': {names} all require exclusive "
                    f"control of the root tc qdisc on {iface_str}.\n"
                    f"  Linux allows only one root qdisc per interface — "
                    f"the second rule will fail.\n"
                    f"  Fix A: Use a single NetworkDelay with combined parameters.\n"
                    f"  Fix B: Assign each fault a different iface= parameter.\n"
                    f"  Fix C: Use conflict='warn' or conflict='force' to override."
                )

    # ── BPF + tc conflict ─────────────────────────────────────────

    def _check_bpf_tc_conflict(self, scenario: "Scenario") -> None:
        tc_faults  = [f for f in scenario.faults if _is_tc_fault(f)]
        bpf_faults = [f for f in scenario.faults if _is_bpf_fault(f)]
        if not tc_faults or not bpf_faults:
            return

        # check for shared interface
        for bpf in bpf_faults:
            for tc in tc_faults:
                bi, ti = _iface_of(bpf), _iface_of(tc)
                if bi == ti:  # same iface or both auto-detect
                    iface_str = bi or "auto-detected default interface"
                    raise ConflictError(
                        f"Scenario '{scenario.name}': SilentNetworkCorrupt (BPF) and "
                        f"{tc.__class__.__name__} (tc netem) both target {iface_str}.\n"
                        f"  BPF operates at the XDP/TC hook and tc netem at the qdisc — "
                        f"combining them on the same interface produces undefined behavior.\n"
                        f"  Fix A: Use SilentNetworkCorrupt alone for silent corruption.\n"
                        f"  Fix B: Use tc netem faults alone for detectable corruption.\n"
                        f"  Fix C: Assign them to different interfaces via iface=."
                    )

    # ── Storage conflicts ─────────────────────────────────────────

    def _check_storage_conflicts(self, scenario: "Scenario") -> None:
        storage_faults = [f for f in scenario.faults if _is_storage_fault(f)]
        if len(storage_faults) < 2:
            return

        by_dir: dict[str, list] = {}
        for f in storage_faults:
            d = _dir_of(f)
            by_dir.setdefault(d, []).append(f)

        for d, faults in by_dir.items():
            if len(faults) > 1:
                dir_str = d or "same directory"
                raise ConflictError(
                    f"Scenario '{scenario.name}': {len(faults)} StorageCorrupt faults "
                    f"target {dir_str}.\n"
                    f"  Multiple cj_storage crontabs on the same directory will "
                    f"conflict and may corrupt the corruption database.\n"
                    f"  Fix: Use one StorageCorrupt per directory."
                )

    # ── SilentNetworkCorrupt + NetworkCorrupt ─────────────────────

    def _check_silent_vs_corrupt(self, scenario: "Scenario") -> None:
        from chaos_jungle.faults.network import NetworkCorrupt
        from chaos_jungle.faults.bpf import SilentNetworkCorrupt

        silent = [f for f in scenario.faults if isinstance(f, SilentNetworkCorrupt)]
        noisy  = [f for f in scenario.faults if isinstance(f, NetworkCorrupt)]
        if not silent or not noisy:
            return

        for s in silent:
            for n in noisy:
                if _iface_of(s) == _iface_of(n):
                    raise ConflictError(
                        f"Scenario '{scenario.name}': SilentNetworkCorrupt and "
                        f"NetworkCorrupt both target the same interface.\n"
                        f"  SilentNetworkCorrupt preserves checksums; NetworkCorrupt "
                        f"breaks them. Using both together produces conflicting results.\n"
                        f"  Fix: Pick one — silent (BPF) or noisy (tc netem corrupt)."
                    )


# ── Level 2: Runtime validator ────────────────────────────────────

class RuntimeValidator:
    """Check live state on the target before injecting faults.

    Checks
    ------
    * tc netem rule already active on the target interface
    * cj_storage crontab already running on the target
    * BPF program already loaded (pid file exists)
    """

    def check(self, scenario: "Scenario", target: "Target") -> None:
        """Run live checks on the target machine.

        Parameters
        ----------
        scenario :
            The scenario about to be started.
        target :
            The machine to check.
        """
        for fault in scenario.faults:
            if _is_tc_fault(fault):
                self._check_existing_tc(fault, target, scenario.name)
            if _is_storage_fault(fault):
                self._check_existing_storage(fault, target, scenario.name)
            if _is_bpf_fault(fault):
                self._check_existing_bpf(fault, target, scenario.name)

    def _check_existing_tc(self, fault, target: "Target", scenario_name: str) -> None:
        iface = _iface_of(fault) or self._detect_iface(target)
        code, stdout, _ = target.run(
            f"tc qdisc show dev {iface} 2>/dev/null | grep -q 'netem' && echo FOUND || echo CLEAR"
        )
        if "FOUND" in stdout:
            raise ConflictError(
                f"Scenario '{scenario_name}': tc netem rule already active on {iface}.\n"
                f"  A previous chaos session may not have been cleaned up.\n"
                f"  Fix A: sudo tc qdisc del dev {iface} root\n"
                f"  Fix B: chaos-jungle stop --force\n"
                f"  Fix C: Use conflict='force' to skip this check."
            )

    def _check_existing_storage(self, fault, target: "Target", scenario_name: str) -> None:
        code, stdout, _ = target.run(
            "crontab -l 2>/dev/null | grep -q 'cj_storage' && echo FOUND || echo CLEAR"
        )
        if "FOUND" in stdout:
            raise ConflictError(
                f"Scenario '{scenario_name}': cj_storage crontab already active on target.\n"
                f"  A previous storage chaos session may not have been stopped.\n"
                f"  Fix A: python3 ~/chaos-jungle/storage/cj_storage.py --stop --revert\n"
                f"  Fix B: chaos-jungle stop --force\n"
                f"  Fix C: Use conflict='force' to skip this check."
            )

    def _check_existing_bpf(self, fault, target: "Target", scenario_name: str) -> None:
        pid_file = getattr(fault, "_pid_file", "/tmp/cj_bpf.pid")
        code, stdout, _ = target.run(
            f"test -f {pid_file} && echo FOUND || echo CLEAR"
        )
        if "FOUND" in stdout:
            raise ConflictError(
                f"Scenario '{scenario_name}': BPF chaos process already running "
                f"(pid file: {pid_file}).\n"
                f"  A previous BPF session may not have been stopped.\n"
                f"  Fix A: kill $(cat {pid_file}) && rm {pid_file}\n"
                f"  Fix B: chaos-jungle stop --force\n"
                f"  Fix C: Use conflict='force' to skip this check."
            )

    def _detect_iface(self, target: "Target") -> str:
        _, stdout, _ = target.run(
            "ip route | grep default | awk '{print $5}' | head -1"
        )
        return stdout.strip() or "eth0"


# ── Level 3: Suite validator ──────────────────────────────────────

class SuiteValidator:
    """Validate an ExperimentSuite before parallel execution.

    Checks
    ------
    * Same target used by more than one scenario in parallel mode
    * Same interface targeted by multiple scenarios on the same host
    """

    def check(self, experiments: list[tuple]) -> None:
        """Validate a list of (scenario, target) pairs.

        Parameters
        ----------
        experiments :
            List of ``(Scenario, Target)`` pairs as passed to
            ``ExperimentSuite``.
        """
        self._check_duplicate_targets(experiments)
        self._check_scenario_conflicts(experiments)

    def _check_duplicate_targets(self, experiments: list[tuple]) -> None:
        seen: dict[str, str] = {}   # target_key → scenario name
        for scenario, target in experiments:
            key = self._target_key(target)
            if key in seen:
                raise ConflictError(
                    f"Suite conflict: target '{key}' is used by both "
                    f"'{seen[key]}' and '{scenario.name}'.\n"
                    f"  Parallel execution on the same target is not safe — "
                    f"faults would conflict on the same network interface or filesystem.\n"
                    f"  Fix A: Assign each scenario a different target machine.\n"
                    f"  Fix B: Run sequentially → suite.run(parallel=False).\n"
                    f"  Fix C: Use conflict='force' to skip this check."
                )
            seen[key] = scenario.name

    def _check_scenario_conflicts(self, experiments: list[tuple]) -> None:
        validator = ScenarioValidator()
        for scenario, _ in experiments:
            validator.check(scenario)

    def _target_key(self, target) -> str:
        """Return a string key that uniquely identifies a target machine."""
        if hasattr(target, "host"):
            return f"{getattr(target, 'user', '')}@{target.host}:{getattr(target, 'port', 22)}"
        return target.__class__.__name__


# ── Convenience: apply guardrails with conflict mode ──────────────

def apply_guardrails(
    scenario: "Scenario",
    target: "Target",
    conflict: str = "raise",
    runtime: bool = True,
) -> None:
    """Run scenario + runtime validators with the given conflict mode.

    Parameters
    ----------
    scenario :
        Scenario to validate.
    target :
        Target to check live state on.
    conflict : str
        One of:

        * ``"raise"`` (default) — raise :exc:`ConflictError` on conflicts
        * ``"warn"``  — emit :class:`ConflictWarning` but continue
        * ``"force"`` — skip all guardrails entirely
    runtime : bool
        Whether to run the live :class:`RuntimeValidator` check.
        Default ``True``.
    """
    import warnings

    if conflict == "force":
        return

    errors = []

    try:
        ScenarioValidator().check(scenario)
    except ConflictError as e:
        errors.append(str(e))

    if runtime:
        try:
            RuntimeValidator().check(scenario, target)
        except ConflictError as e:
            errors.append(str(e))

    if not errors:
        return

    message = "\n\n".join(errors)

    if conflict == "warn":
        warnings.warn(message, ConflictWarning, stacklevel=3)
    else:
        raise ConflictError(message)
