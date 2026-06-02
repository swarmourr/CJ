"""Shared helpers used across all scenarios."""

import os
from datetime import datetime
from chaos_jungle import SSHTarget


def make_target(ip: str) -> SSHTarget:
    """Build an SSHTarget from config credentials."""
    from config import USER, PASSWORD, SSH_KEY
    return SSHTarget(
        ip,
        user=USER,
        key=SSH_KEY if SSH_KEY else None,
        password=PASSWORD or None,
        allow_agent=not bool(PASSWORD),
        look_for_keys=not bool(PASSWORD),
    )


def result_dir(scenario_name: str) -> str:
    """Create and return a timestamped result directory."""
    from config import RESULTS_DIR
    ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(RESULTS_DIR, f"{scenario_name}_{ts}")
    os.makedirs(path, exist_ok=True)
    return path


def print_banner(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}\n")
