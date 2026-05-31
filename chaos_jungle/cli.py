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
@click.option("--session", "-i", default=None, type=int,
              help="Session id to export (default: all sessions)")
@click.option("--format", "-f", "fmt", default="json",
              type=click.Choice(["json", "csv"]),
              help="Output format. Default: json")
@click.option("--output", "-o", default="", metavar="PATH",
              help="Output file path. Default: auto-named in current directory")
def export(session, fmt, output):
    """Export session(s) to a portable JSON or CSV file.

    \b
    CSV columns: session_id, name, status, started_at, stopped_at,
                 duration_s, fault_kind, fault_parameters, <metrics...>

    Examples:
      chaos-jungle export --session 3 --format csv
      chaos-jungle export --session 3 --format json --output run3.json
      chaos-jungle export --format csv          # all sessions
    """
    import csv as csv_mod, io as io_mod
    from datetime import datetime, timezone

    db = SessionDB()

    if session is not None:
        sessions_to_export = [session]
    else:
        rows = db.list_sessions()
        if not rows:
            click.echo("No sessions found.")
            return
        sessions_to_export = [r["id"] for r in rows]

    if fmt == "json":
        if len(sessions_to_export) == 1:
            payload = db.export_session(sessions_to_export[0])
        else:
            payload = [db.export_session(sid) for sid in sessions_to_export]

        content = json.dumps(payload, indent=2)
        if output:
            dest = output
        elif session is not None:
            sess = db.get_session(session)
            safe_name = sess["name"].replace("/", "_").replace(" ", "_") if sess else str(session)
            dest = f"session_{session}_{safe_name}.json"
        else:
            dest = "chaos_sessions.json"

        with open(dest, "w") as fh:
            fh.write(content)
        click.echo(f"[chaos-jungle] Exported {len(sessions_to_export)} session(s) → {dest}")

    elif fmt == "csv":
        # Collect all rows first to determine metric columns
        all_rows = []
        metric_keys: list[str] = []

        for sid in sessions_to_export:
            data = db.export_session(sid)
            sess = data["session"]

            # compute duration
            duration_s = ""
            if sess.get("started_at") and sess.get("stopped_at"):
                try:
                    t0 = datetime.fromisoformat(sess["started_at"])
                    t1 = datetime.fromisoformat(sess["stopped_at"])
                    duration_s = round((t1 - t0).total_seconds(), 1)
                except ValueError:
                    pass

            results = db.get_results(sid)
            faults = data["faults"] or [{"kind": "", "parameters": {}}]

            # Build one base row per fault
            for fault in faults:
                base = {
                    "session_id": sess["id"],
                    "name": sess["name"],
                    "status": sess["status"],
                    "started_at": sess.get("started_at", ""),
                    "stopped_at": sess.get("stopped_at", ""),
                    "duration_s": duration_s,
                    "fault_kind": fault["kind"],
                    "fault_parameters": json.dumps(fault["parameters"]),
                }
                # Flatten the first result record's metrics
                metrics: dict = {}
                if results:
                    metrics = results[0].get("metrics", {}) if isinstance(results[0].get("metrics"), dict) else {}
                base.update(metrics)
                for k in metrics:
                    if k not in metric_keys:
                        metric_keys.append(k)
                all_rows.append(base)

        base_cols = ["session_id", "name", "status", "started_at", "stopped_at",
                     "duration_s", "fault_kind", "fault_parameters"]
        fieldnames = base_cols + metric_keys

        buf = io_mod.StringIO()
        writer = csv_mod.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in all_rows:
            writer.writerow(row)

        content = buf.getvalue()

        if output:
            dest = output
        elif session is not None:
            sess_row = db.get_session(session)
            safe_name = sess_row["name"].replace("/", "_").replace(" ", "_") if sess_row else str(session)
            dest = f"session_{session}_{safe_name}.csv"
        else:
            dest = "chaos_sessions.csv"

        with open(dest, "w") as fh:
            fh.write(content)
        click.echo(f"[chaos-jungle] Exported {len(sessions_to_export)} session(s) → {dest}")


# ── fetch ──────────────────────────────────────────────────────────

@main.command()
@click.option("--target", "-t", required=True,
              help="SSH target: ssh://user@host  or  ssh://user@host:port")
@click.option("--output-dir", "-o", default="./chaos-results",
              help="Local directory to save files into. Default: ./chaos-results")
@click.option("--remote-dir", default="~/.chaos-jungle",
              help="Remote directory to fetch from. Default: ~/.chaos-jungle")
@click.option("--files", "-f", default="chaos_jungle.db",
              help="Comma-separated list of filenames to fetch. Default: chaos_jungle.db")
@click.option("--export-csv/--no-export-csv", default=True,
              help="After fetching the DB, also export all sessions to CSV. Default: yes")
def fetch(target, output_dir, remote_dir, files, export_csv):
    """Fetch result files from a remote SSH target.

    Downloads files from the remote chaos-jungle directory to a local
    output directory. Useful for collecting experiment results after a
    remote chaos run.

    \b
    Examples:
      # fetch DB only
      chaos-jungle fetch --target ssh://ubuntu@10.0.0.5

      # fetch DB + a custom log file, save to ./results/
      chaos-jungle fetch --target ssh://ubuntu@10.0.0.5 \\
          --files "chaos_jungle.db,cj.log" --output-dir ./results/

      # fetch without auto-exporting to CSV
      chaos-jungle fetch --target ssh://ubuntu@10.0.0.5 --no-export-csv
    """
    import os

    tgt = _make_target(target)
    os.makedirs(output_dir, exist_ok=True)

    try:
        tgt.connect()
    except Exception as e:
        click.echo(f"[chaos-jungle] ERROR connecting to {target}: {e}", err=True)
        sys.exit(1)

    # Resolve remote home dir (handles ~ expansion on remote)
    try:
        _, home_out, _ = tgt.run("echo $HOME")
        remote_home = home_out.strip()
        resolved_dir = remote_dir.replace("~", remote_home)
    except Exception:
        resolved_dir = remote_dir

    fetched = []
    for filename in [f.strip() for f in files.split(",") if f.strip()]:
        remote_path = f"{resolved_dir}/{filename}"
        local_path = os.path.join(output_dir, filename)
        try:
            tgt.get(remote_path, local_path)
            click.echo(f"[chaos-jungle] Fetched  {remote_path}  →  {local_path}")
            fetched.append(local_path)
        except FileNotFoundError:
            click.echo(f"[chaos-jungle] MISSING  {remote_path} (skipped)", err=True)
        except Exception as e:
            click.echo(f"[chaos-jungle] ERROR fetching {remote_path}: {e}", err=True)

    tgt.disconnect()

    # Auto-export CSV from the fetched DB
    if export_csv:
        db_path = os.path.join(output_dir, "chaos_jungle.db")
        if os.path.isfile(db_path):
            try:
                from chaos_jungle.db.session_db import SessionDB as _SDB
                remote_db = _SDB(path=db_path)
                rows = remote_db.list_sessions()
                if not rows:
                    click.echo("[chaos-jungle] DB is empty — no sessions to export.")
                else:
                    import csv as csv_mod, io as io_mod
                    from datetime import datetime as _dt

                    all_rows: list[dict] = []
                    metric_keys: list[str] = []

                    for sess_row in rows:
                        sid = sess_row["id"]
                        data = remote_db.export_session(sid)
                        sess = data["session"]
                        duration_s = ""
                        if sess.get("started_at") and sess.get("stopped_at"):
                            try:
                                t0 = _dt.fromisoformat(sess["started_at"])
                                t1 = _dt.fromisoformat(sess["stopped_at"])
                                duration_s = round((t1 - t0).total_seconds(), 1)
                            except ValueError:
                                pass

                        results = remote_db.get_results(sid)
                        faults = data["faults"] or [{"kind": "", "parameters": {}}]

                        for fault in faults:
                            base = {
                                "session_id": sess["id"],
                                "name": sess["name"],
                                "status": sess["status"],
                                "started_at": sess.get("started_at", ""),
                                "stopped_at": sess.get("stopped_at", ""),
                                "duration_s": duration_s,
                                "fault_kind": fault["kind"],
                                "fault_parameters": json.dumps(fault["parameters"]),
                            }
                            metrics: dict = {}
                            if results:
                                metrics = results[0].get("metrics", {}) if isinstance(results[0].get("metrics"), dict) else {}
                            base.update(metrics)
                            for k in metrics:
                                if k not in metric_keys:
                                    metric_keys.append(k)
                            all_rows.append(base)

                    base_cols = ["session_id", "name", "status", "started_at", "stopped_at",
                                 "duration_s", "fault_kind", "fault_parameters"]
                    fieldnames = base_cols + metric_keys
                    csv_path = os.path.join(output_dir, "chaos_sessions.csv")
                    with open(csv_path, "w", newline="") as fh:
                        writer = csv_mod.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
                        writer.writeheader()
                        for row in all_rows:
                            writer.writerow(row)
                    click.echo(f"[chaos-jungle] Exported {len(all_rows)} row(s) → {csv_path}")
            except Exception as e:
                click.echo(f"[chaos-jungle] WARNING: could not auto-export CSV: {e}", err=True)

    if fetched:
        click.echo(f"[chaos-jungle] Done. Files saved in: {os.path.abspath(output_dir)}")


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


# ── dashboard ─────────────────────────────────────────────────────

@main.command()
@click.option("--host", default="127.0.0.1", help="Bind address. Default: 127.0.0.1")
@click.option("--port", default=8050, help="Port. Default: 8050")
def dashboard(host, port):
    """Open the experiment tracking dashboard in your browser."""
    from chaos_jungle.dashboard import run as dash_run
    import webbrowser, threading
    url = f"http://{host}:{port}"
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    click.echo(f"[chaos-jungle] Dashboard → {url}  (Ctrl+C to stop)")
    dash_run(host=host, port=port)


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
