"""Command-line interface for chaos-jungle."""

from __future__ import annotations
import json
import sys

import click

from chaos_jungle.db.session_db import SessionDB
from chaos_jungle.targets.local import LocalTarget
from chaos_jungle.targets.ssh import SSHTarget
from chaos_jungle.targets.http import HTTPTarget


def _make_target(target_str: str | None):
    """Parse a target string into a Target instance.

    Formats:
      (empty)                → LocalTarget
      ssh://user@host        → SSHTarget
      ssh://user@host:port   → SSHTarget with custom port
      http://host:port       → HTTPTarget
      https://host:port      → HTTPTarget (TLS)
    """
    if not target_str:
        return LocalTarget()
    if target_str.startswith("ssh://"):
        rest = target_str[6:]
        user, hostport = rest.split("@", 1)
        if ":" in hostport:
            host, port = hostport.rsplit(":", 1)
            return SSHTarget(host, user=user, port=int(port))
        return SSHTarget(hostport, user=user)
    if target_str.startswith("http://") or target_str.startswith("https://"):
        return HTTPTarget(target_str)
    raise click.BadParameter(f"Unknown target format: {target_str!r}")


@click.group()
@click.version_option(package_name="chaos-jungle")
def main():
    """chaos-jungle — inject and control chaos faults on any machine."""


# ── start ─────────────────────────────────────────────────────────

@main.command()
@click.option("--scenario", "-s", required=True, help="Scenario name (stored in DB)")
@click.option("--target", "-t", default="", help="Target: ssh://user@host  http://host:port  (empty=local)")
@click.option("--delay", default="", help="NetworkDelay, e.g. 100ms")
@click.option("--jitter", default="", help="Jitter for delay, e.g. 10ms")
@click.option("--loss", default="", help="NetworkLoss rate, e.g. 5%")
@click.option("--corrupt", default="", help="NetworkCorrupt rate, e.g. 1%")
@click.option("--duplicate", default="", help="NetworkDuplicate rate, e.g. 0.5%")
@click.option("--storage-pattern", default="", help="StorageCorrupt file pattern, e.g. '*.pdb'")
@click.option("--storage-dir", default="", help="StorageCorrupt directory")
@click.option("--storage-interval", default="10m", help="StorageCorrupt interval. Default: 10m")
@click.option("--auto-install", is_flag=True, default=False, help="Auto-install missing dependencies via apt-get")
@click.option("--conflict", default="raise", type=click.Choice(["raise", "warn", "force"]), help="Conflict handling: raise (default), warn, or force")
@click.option("--duration", "-d", default="", help="Auto-stop after duration: 10m, 1h, 30s, 1h30m (empty = run until 'chaos-jungle stop')")
@click.option("--silent-corrupt", default=0, type=int, help="SilentNetworkCorrupt: mangle 1 in N packets via BPF (e.g. 5000)")
@click.option("--bpf-hook", default="tc", type=click.Choice(["tc", "xdp"]), help="BPF hook for silent corrupt (default: tc)")
def start(scenario, target, delay, jitter, loss, corrupt, duplicate,
          storage_pattern, storage_dir, storage_interval,
          auto_install, duration, silent_corrupt, bpf_hook, conflict):
    """Start chaos faults. Returns immediately unless --duration is given."""
    from chaos_jungle.faults.network import NetworkDelay, NetworkLoss, NetworkCorrupt, NetworkDuplicate
    from chaos_jungle.faults.storage import StorageCorrupt
    from chaos_jungle.faults.bpf import SilentNetworkCorrupt
    from chaos_jungle.faults.base import PreflightError
    from chaos_jungle.runner import ChaosRunner
    from chaos_jungle.scenario import Scenario

    faults = []
    if delay:
        faults.append(NetworkDelay(delay, jitter=jitter))
    if loss:
        faults.append(NetworkLoss(loss))
    if corrupt:
        faults.append(NetworkCorrupt(corrupt))
    if duplicate:
        faults.append(NetworkDuplicate(duplicate))
    if storage_pattern and storage_dir:
        faults.append(StorageCorrupt(storage_pattern, storage_dir, interval=storage_interval))
    if silent_corrupt:
        faults.append(SilentNetworkCorrupt(rate=silent_corrupt, hook=bpf_hook))

    if not faults:
        raise click.UsageError(
            "Specify at least one fault: --delay, --loss, --corrupt, "
            "--silent-corrupt, --storage-pattern + --storage-dir"
        )

    runner = ChaosRunner(
        Scenario(scenario, faults),
        target=_make_target(target),
        auto_install=auto_install,
        conflict=conflict,
    )
    try:
        if duration:
            # blocking — run for fixed duration then stop
            runner.run(duration)
            click.echo(f"[chaos-jungle] Session {runner._session_id} complete.")
        else:
            # fire-and-forget — returns immediately
            runner.start()
            click.echo(f"[chaos-jungle] Session started: {scenario}  (session id: {runner._session_id})")
            click.echo("[chaos-jungle] Chaos is ON. Run 'chaos-jungle stop' when done.")
    except PreflightError as e:
        click.echo(f"[chaos-jungle] ERROR: {e}", err=True)
        sys.exit(1)


# ── stop ──────────────────────────────────────────────────────────

@main.command()
@click.option("--session", "-i", default=None, type=int, help="Session id (default: most recent)")
@click.option("--target", "-t", default="", help="Target (must match where chaos was started)")
def stop(session, target):
    """Stop and revert the active chaos session."""
    from chaos_jungle.runner import ChaosRunner

    db = SessionDB()
    if session:
        row = db.get_session(session)
        if row is None:
            click.echo(f"Session {session} not found", err=True)
            sys.exit(1)
    else:
        row = db.active_session()
        if row is None:
            click.echo("No running session found", err=True)
            sys.exit(1)

    runner = ChaosRunner.attach(db=db, target=_make_target(target))
    runner.stop()
    click.echo(f"[chaos-jungle] Session {row['id']} stopped and reverted.")


# ── status ────────────────────────────────────────────────────────

@main.command()
def status():
    """Show the current active session."""
    db = SessionDB()
    row = db.active_session()
    if row is None:
        click.echo("No active session.")
        return
    click.echo(f"Session {row['id']}: {row['name']}  status={row['status']}  started={row['started_at']}")


# ── list ──────────────────────────────────────────────────────────

@main.command("list")
def list_sessions():
    """List all sessions."""
    db = SessionDB()
    sessions = db.list_sessions()
    if not sessions:
        click.echo("No sessions found.")
        return
    click.echo(f"{'ID':>4}  {'NAME':<30}  {'STATUS':<10}  STARTED")
    click.echo("-" * 70)
    for s in sessions:
        click.echo(f"{s['id']:>4}  {s['name']:<30}  {s['status']:<10}  {s['started_at']}")


# ── export ────────────────────────────────────────────────────────

@main.command()
@click.option("--session", "-i", required=True, type=int, help="Session id to export")
@click.option("--format", "-f", "fmt", default="json", type=click.Choice(["json", "csv"]))
def export(session, fmt):
    """Export a session to JSON or CSV."""
    db = SessionDB()
    data = db.export_session(session)

    if fmt == "json":
        click.echo(json.dumps(data, indent=2))
    elif fmt == "csv":
        import csv, io
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=["id", "session_id", "fault_id", "timestamp", "message"])
        writer.writeheader()
        for e in data["events"]:
            writer.writerow(e)
        click.echo(out.getvalue())


# ── suite ─────────────────────────────────────────────────────────

@main.command()
@click.option("--config", "-c", required=True, help="Path to suite YAML config file")
@click.option("--parallel/--sequential", default=True, help="Run experiments in parallel (default) or one-by-one")
@click.option("--max-workers", default=None, type=int, help="Max parallel threads (default: number of experiments)")
def suite(config, parallel, max_workers):
    """Run an ExperimentSuite from a YAML config file.

    \b
    YAML schema:

        duration: 10m
        conflict: raise
        auto_install: false
        experiments:
          - name: baseline
            target: local
            faults: []
          - name: net-delay
            target: ssh://ubuntu@node1
            faults:
              - kind: NetworkDelay
                delay: 100ms
    """
    from chaos_jungle.config import load_suite
    from chaos_jungle.suite import ExperimentSuite

    try:
        exp_suite = load_suite(config)
    except (FileNotFoundError, ValueError, ImportError) as e:
        click.echo(f"[chaos-jungle] ERROR: {e}", err=True)
        import sys; sys.exit(1)

    n = len(exp_suite)
    mode = "parallel" if parallel else "sequential"
    click.echo(f"[chaos-jungle] Starting suite with {n} experiment(s) ({mode})")

    results = exp_suite.run(parallel=parallel, max_workers=max_workers)
    ExperimentSuite.print_summary(results)

    failed = [r for r in results.values() if r.status != "ok"]
    if failed:
        import sys; sys.exit(1)


# ── daemon subcommand ────────────────────────────────────────────

@main.command()
@click.option("--host", default="0.0.0.0", help="Bind address. Default: 0.0.0.0")
@click.option("--port", default=7777, help="Port. Default: 7777")
@click.option("--token", default="", help="Bearer token for auth (optional)")
def daemon(host, port, token):
    """Start the chaos daemon on this machine (for HTTP target mode)."""
    from chaos_jungle.daemon import run as daemon_run
    click.echo(f"[chaos-jungle] Starting daemon on {host}:{port}")
    daemon_run(host=host, port=port, token=token)
