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
        "dnf":  "inotify-tools",   # needs EPEL on CentOS/RHEL
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
}

# canonical_name -> binary to probe with `which`
# None means no binary: fall back to package-manager query
PKG_TO_BIN: dict[str, str | None] = {
    "iproute2":      "tc",
    "e2fsprogs":     "filefrag",
    "inotify-tools": "inotifywait",
    "coreutils":     "dd",
    "python3":       "python3",
    "python3-bpfcc": None,
}

# Install command templates per manager
_INSTALL_CMDS: dict[str, str] = {
    "apt":  "DEBIAN_FRONTEND=noninteractive apt-get install -y {pkg}",
    "dnf":  "dnf install -y {pkg}",
    "yum":  "yum install -y {pkg}",
    "apk":  "apk add --no-cache {pkg}",
    "brew": "brew install {pkg}",
}

# Pip install command (for Python packages like python-crontab)
_PIP_MAP: dict[str, str] = {
    "python-crontab": "python-crontab",
}


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def detect_pkg_manager(target: "Target") -> str | None:
    """Detect which system package manager is available on the target.

    Probes for apt-get, dnf, yum, apk, brew in order.

    Parameters
    ----------
    target :
        The machine to probe.

    Returns
    -------
    str or None
        One of ``"apt"``, ``"dnf"``, ``"yum"``, ``"apk"``, ``"brew"``,
        or ``None`` if none were found.
    """
    for cmd, key in [
        ("apt-get", "apt"),
        ("dnf",     "dnf"),
        ("yum",     "yum"),
        ("apk",     "apk"),
        ("brew",    "brew"),
    ]:
        code, _, _ = target.run(f"which {cmd} 2>/dev/null")
        if code == 0:
            return key
    return None


def check_missing(
    target: "Target",
    dependencies: list[str],
) -> list[tuple[str, str | None]]:
    """Return (canonical_name, binary) pairs for every missing dependency.

    Parameters
    ----------
    target :
        The machine to check.
    dependencies :
        List of canonical package names (as declared in ``Fault.dependencies``).

    Returns
    -------
    list of (str, str | None)
        Each entry is ``(canonical_name, binary_checked)`` for packages that
        are not present on the target.
    """
    missing = []
    for pkg in dependencies:
        binary = PKG_TO_BIN.get(pkg, pkg)
        if binary:
            code, _, _ = target.run(f"which {binary} 2>/dev/null")
            is_missing = code != 0
        else:
            # No known binary — query dpkg / rpm / apk
            code, out, _ = target.run(
                f"(dpkg -s {pkg} 2>/dev/null | grep -q 'installed') || "
                f"(rpm -q {pkg} 2>/dev/null | grep -qv 'not installed') || "
                f"(apk info {pkg} 2>/dev/null | grep -q {pkg})"
            )
            is_missing = code != 0
        if is_missing:
            missing.append((pkg, binary))
    return missing


# ---------------------------------------------------------------------------
# Installation helper
# ---------------------------------------------------------------------------

def install_package(
    target: "Target",
    canonical: str,
    mgr: str,
) -> None:
    """Install a single package on the target using the given package manager.

    Parameters
    ----------
    target :
        The machine to install on.
    canonical :
        Canonical package name (key in :data:`PKG_MAP`).
    mgr :
        Package manager key (``"apt"``, ``"dnf"``, etc.).

    Raises
    ------
    RuntimeError
        If the install command returns a non-zero exit code.
    """
    pkg_name = PKG_MAP.get(canonical, {}).get(mgr, canonical)
    cmd = _INSTALL_CMDS[mgr].format(pkg=pkg_name)
    print(f"[preflight] Installing '{pkg_name}' via {mgr} ...", flush=True)
    code, _, stderr = target.sudo(cmd)
    if code != 0:
        raise RuntimeError(
            f"[preflight] Failed to install '{pkg_name}' via {mgr}: {stderr.strip()}"
        )
    print(f"[preflight] OK: {pkg_name}", flush=True)


def install_pip_package(target: "Target", pip_pkg: str) -> None:
    """Install a Python package on the target via pip3.

    Parameters
    ----------
    target :
        The machine to install on.
    pip_pkg :
        PyPI package name.
    """
    pkg_name = _PIP_MAP.get(pip_pkg, pip_pkg)
    print(f"[preflight] Installing Python package '{pkg_name}' via pip3 ...", flush=True)
    # Try with --break-system-packages first (pip >= 22.3); fall back for older pip
    code, _, stderr = target.run(f"pip3 install --quiet --break-system-packages {pkg_name}")
    if code != 0:
        code, _, stderr = target.run(f"pip3 install --quiet {pkg_name}")
    if code != 0:
        raise RuntimeError(
            f"[preflight] pip3 install '{pkg_name}' failed: {stderr.strip()}"
        )
    print(f"[preflight] OK: {pkg_name}", flush=True)


# ---------------------------------------------------------------------------
# Main entry-point used by Fault.preflight()
# ---------------------------------------------------------------------------

def run_preflight(
    target: "Target",
    fault_name: str,
    dependencies: list[str],
    pip_dependencies: list[str],
    auto_install: bool | str,
) -> None:
    """Full preflight check with optional auto-install and user prompt.

    Parameters
    ----------
    target :
        The machine to check.
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
        * ``"prompt"`` — show a table of what will be installed and ask
          for confirmation before proceeding.

    Raises
    ------
    ~chaos_jungle.faults.base.PreflightError
        When ``auto_install=False`` and dependencies are missing, or when
        the user declines installation in prompt mode.
    """
    from chaos_jungle.faults.base import PreflightError

    missing_sys = check_missing(target, dependencies)
    missing_pip = [p for p in pip_dependencies if not _pip_installed(target, p)]

    if not missing_sys and not missing_pip:
        return  # all good

    # Build human-readable summary
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

    # ── raise ──────────────────────────────────────────────────────────
    if auto_install is False:
        mgr = detect_pkg_manager(target) or "apt"
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

    # ── prompt ─────────────────────────────────────────────────────────
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

    # ── auto_install=True  or  user confirmed ──────────────────────────
    if missing_sys:
        mgr = detect_pkg_manager(target)
        if mgr is None:
            raise PreflightError(
                "Cannot detect a package manager on the target "
                "(tried apt-get, dnf, yum, apk, brew).\n"
                f"Install manually:\n{summary}"
            )
        for canonical, _ in missing_sys:
            install_package(target, canonical, mgr)

    for pip_pkg in missing_pip:
        install_pip_package(target, pip_pkg)


def _pip_installed(target: "Target", pkg: str) -> bool:
    """Return True if a Python package is importable on the target."""
    import_name = pkg.replace("-", "_").split("[")[0]
    code, _, _ = target.run(
        f"python3 -c 'import {import_name}' 2>/dev/null"
    )
    return code == 0
