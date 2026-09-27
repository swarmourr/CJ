"""Cross-platform dependency detection and auto-installation for fault preflight checks.

Supports apt (Debian/Ubuntu), dnf/yum (RHEL/Fedora/CentOS), apk (Alpine),
and brew (macOS).  Maps canonical package names to the correct name for
each package manager, detects missing binaries, and can install with or
without user confirmation.
"""

from __future__ import annotations
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chaos_jungle.targets.base import Target

# ---------------------------------------------------------------------------
# Package name map
# canonical_name -> {manager: actual_package_name_on_that_manager}
# ---------------------------------------------------------------------------
PKG_MAP: dict[str, dict[str, str]] = {
    "iproute2": {
        "apt":  "iproute2",
        "dnf":  "iproute",
        "yum":  "iproute",
        "apk":  "iproute2",
        "brew": "iproute2mac",
    },
    "e2fsprogs": {
        "apt":  "e2fsprogs",
        "dnf":  "e2fsprogs",
        "yum":  "e2fsprogs",
        "apk":  "e2fsprogs",
    },
    "inotify-tools": {
        "apt":  "inotify-tools",
        "dnf":  "inotify-tools",
        "yum":  "inotify-tools",
        "apk":  "inotify-tools",
    },
    "coreutils": {
        "apt":  "coreutils",
        "dnf":  "coreutils",
        "yum":  "coreutils",
        "apk":  "coreutils",
        "brew": "coreutils",
    },
    "python3": {
        "apt":  "python3",
        "dnf":  "python3",
        "yum":  "python3",
        "apk":  "python3",
        "brew": "python@3",
    },
    "python3-bpfcc": {
        "apt":  "python3-bpfcc",
        "dnf":  "python3-bcc",
        "yum":  "python3-bcc",
    },
    "sysstat": {
        "apt":  "sysstat",
        "dnf":  "sysstat",
        "yum":  "sysstat",
        "apk":  "sysstat",
        "brew": "sysstat",
    },
    "iputils": {
        "apt":  "iputils-ping",
        "dnf":  "iputils",
        "yum":  "iputils",
        "apk":  "iputils",
        "brew": "inetutils",
    },
    "procps": {
        "apt":  "procps",
        "dnf":  "procps-ng",
        "yum":  "procps-ng",
        "apk":  "procps",
        "brew": "procps",
    },
    "nvidia-utils": {
        "apt":  "nvidia-utils-535",
        "dnf":  "nvidia-utils",
        "yum":  "nvidia-utils",
        "apk":  "nvidia-utils",
    },
    "docker-cli": {
        "apt":  "docker.io",
        "dnf":  "docker-ce-cli",
        "yum":  "docker-ce-cli",
        "apk":  "docker-cli",
    },
    "redis-tools": {
        "apt":  "redis-tools",
        "dnf":  "redis",
        "yum":  "redis",
        "apk":  "redis",
        "brew": "redis",
    },
    "postgresql-client": {
        "apt":  "postgresql-client",
        "dnf":  "postgresql",
        "yum":  "postgresql",
        "apk":  "postgresql-client",
        "brew": "postgresql",
    },
    "stress-ng": {
        "apt":  "stress-ng",
        "dnf":  "stress-ng",
        "yum":  "stress-ng",
        "apk":  "stress-ng",
        "brew": "stress-ng",
    },
}

PKG_TO_BIN: dict[str, str | None] = {
    "iproute2":           "tc",
    "e2fsprogs":          "filefrag",
    "inotify-tools":      "inotifywait",
    "coreutils":          "dd",
    "python3":            "python3",
    "python3-bpfcc":      None,
    "sysstat":            "iostat",
    "iputils":            "ping",
    "procps":             "pgrep",
    "nvidia-utils":       "nvidia-smi",
    "docker-cli":         "docker",
    "redis-tools":        "redis-cli",
    "postgresql-client":  "psql",
    "stress-ng":          "stress-ng",
}

_INSTALL_CMDS: dict[str, str] = {
    "apt":  "DEBIAN_FRONTEND=noninteractive apt-get install -y {pkg}",
    "dnf":  "dnf install -y {pkg}",
    "yum":  "yum install -y {pkg}",
    "apk":  "apk add --no-cache {pkg}",
    "brew": "brew install {pkg}",
}

_PIP_MAP: dict[str, str] = {
    "python-crontab": "python-crontab",
}


class Preflight:
    """Dependency detection and auto-installation for a specific target.

    Instantiate with a target, then call :meth:`run` to check and optionally
    install missing system and pip packages.  The detected package manager is
    cached so subsequent calls within the same preflight session avoid an
    extra ``which`` probe.

    Parameters
    ----------
    target :
        The machine to probe and install on.

    Examples
    --------
    ::

        pf = Preflight(target)
        pf.run("NetworkDelay", ["iproute2"], [], auto_install=True)
    """

    def __init__(self, target: "Target") -> None:
        self.target = target
        self._mgr: str | None = None  # cached after first detection

    # ── Detection ─────────────────────────────────────────────────

    def detect_pkg_manager(self) -> str | None:
        """Return the available system package manager, or ``None``."""
        if self._mgr is not None:
            return self._mgr
        for cmd, key in [
            ("apt-get", "apt"),
            ("dnf",     "dnf"),
            ("yum",     "yum"),
            ("apk",     "apk"),
            ("brew",    "brew"),
        ]:
            code, _, _ = self.target.run(f"which {cmd} 2>/dev/null")
            if code == 0:
                self._mgr = key
                return key
        return None

    def check_missing(self, dependencies: list[str]) -> list[tuple[str, str | None]]:
        """Return ``(canonical_name, binary)`` pairs for every missing dep."""
        missing = []
        for pkg in dependencies:
            binary = PKG_TO_BIN.get(pkg, pkg)
            if binary:
                code, _, _ = self.target.run(f"which {binary} 2>/dev/null")
                is_missing = code != 0
            else:
                code, _, _ = self.target.run(
                    f"(dpkg -s {pkg} 2>/dev/null | grep -q 'installed') || "
                    f"(rpm -q {pkg} 2>/dev/null | grep -qv 'not installed') || "
                    f"(apk info {pkg} 2>/dev/null | grep -q {pkg})"
                )
                is_missing = code != 0
            if is_missing:
                missing.append((pkg, binary))
        return missing

    # ── Installation ──────────────────────────────────────────────

    def install_package(self, canonical: str, mgr: str) -> None:
        """Install a single system package on the target."""
        pkg_name = PKG_MAP.get(canonical, {}).get(mgr, canonical)
        cmd = _INSTALL_CMDS[mgr].format(pkg=pkg_name)
        print(f"[preflight] Installing '{pkg_name}' via {mgr} ...", flush=True)
        code, _, stderr = self.target.sudo(cmd)
        if code != 0:
            raise RuntimeError(
                f"[preflight] Failed to install '{pkg_name}' via {mgr}: {stderr.strip()}"
            )
        print(f"[preflight] OK: {pkg_name}", flush=True)

    def install_pip_package(self, pip_pkg: str) -> None:
        """Install a Python package on the target via pip3."""
        pkg_name = _PIP_MAP.get(pip_pkg, pip_pkg)
        print(f"[preflight] Installing Python package '{pkg_name}' via pip3 ...", flush=True)
        code, _, stderr = self.target.run(
            f"pip3 install --quiet --break-system-packages {pkg_name}"
        )
        if code != 0:
            code, _, stderr = self.target.run(f"pip3 install --quiet {pkg_name}")
        if code != 0:
            raise RuntimeError(
                f"[preflight] pip3 install '{pkg_name}' failed: {stderr.strip()}"
            )
        print(f"[preflight] OK: {pkg_name}", flush=True)

    def _pip_installed(self, pkg: str) -> bool:
        import_name = pkg.replace("-", "_").split("[")[0]
        code, _, _ = self.target.run(
            f"python3 -c 'import {import_name}' 2>/dev/null"
        )
        return code == 0

    # ── Main entry-point ──────────────────────────────────────────

    def run(
        self,
        fault_name: str,
        dependencies: list[str],
        pip_dependencies: list[str],
        auto_install: bool | str,
    ) -> None:
        """Check and optionally install all missing dependencies.

        Parameters
        ----------
        fault_name :
            Fault class name (used in error messages).
        dependencies :
            System package names to check.
        pip_dependencies :
            Python (pip) packages to check.
        auto_install : bool or ``"prompt"``
            * ``False``  — raise :exc:`~chaos_jungle.faults.base.PreflightError`
              if anything is missing.
            * ``True``   — detect package manager and install silently.
            * ``"prompt"`` — show missing deps and ask for confirmation.
        """
        from chaos_jungle.faults.base import PreflightError

        missing_sys = self.check_missing(dependencies)
        missing_pip = [p for p in pip_dependencies if not self._pip_installed(p)]

        if not missing_sys and not missing_pip:
            return

        lines = []
        if missing_sys:
            lines.append("  System packages:")
            for canonical, binary in missing_sys:
                lines.append(f"    - {canonical!r}  (binary: {binary or 'n/a'})")
        if missing_pip:
            lines.append("  Python (pip) packages:")
            for p in missing_pip:
                lines.append(f"    - {p!r}")
        summary = "\n".join(lines)

        if auto_install is False:
            mgr = self.detect_pkg_manager() or "apt"
            sys_fix = " ".join(
                PKG_MAP.get(c, {}).get(mgr, c) for c, _ in missing_sys
            )
            pip_fix = " ".join(missing_pip)
            fix_hint = ""
            if sys_fix:
                fix_hint += f"  sudo {mgr} install {sys_fix}\n"
            if pip_fix:
                fix_hint += f"  pip3 install {pip_fix}\n"
            raise PreflightError(
                f"{fault_name} preflight failed — missing on target:\n{summary}\n\n"
                f"Fix:\n{fix_hint}"
                f"Or pass auto_install=True to install automatically."
            )

        if auto_install == "prompt":
            print(f"\n[preflight] {fault_name} — missing dependencies:\n{summary}")
            try:
                answer = input("\nInstall now? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"
            if answer not in ("y", "yes"):
                raise PreflightError(
                    f"{fault_name} preflight cancelled by user — dependencies not installed."
                )

        if missing_sys:
            mgr = self.detect_pkg_manager()
            if mgr is None:
                raise PreflightError(
                    "Cannot detect a package manager on the target "
                    "(tried apt-get, dnf, yum, apk, brew).\n"
                    f"Install manually:\n{summary}"
                )
            for canonical, _ in missing_sys:
                self.install_package(canonical, mgr)

        for pip_pkg in missing_pip:
            self.install_pip_package(pip_pkg)


# ── Module-level aliases (backwards compatibility) ────────────────

def detect_pkg_manager(target: "Target") -> str | None:
    return Preflight(target).detect_pkg_manager()


def run_preflight(
    target: "Target",
    fault_name: str,
    dependencies: list[str],
    pip_dependencies: list[str],
    auto_install: bool | str,
) -> None:
    Preflight(target).run(fault_name, dependencies, pip_dependencies, auto_install)
