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


# ── export helpers ─────────────────────────────────────────────────

def _parse_session_ids(session: int | None, sessions: str | None, db: "SessionDB") -> list[int]:
    """Resolve --session / --sessions into an ordered list of session IDs.

    ``sessions`` accepts comma-separated IDs and inclusive ranges::

        "3"          → [3]
        "1,3,5"      → [1, 3, 5]
        "1-5"        → [1, 2, 3, 4, 5]
        "1,3,5-7"    → [1, 3, 5, 6, 7]
        ""  / None   → all sessions in DB
    """
    if session is not None and sessions:
        raise click.UsageError("Use either --session or --sessions, not both.")

    if session is not None:
        return [session]

    if sessions:
        ids: list[int] = []
        for part in sessions.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                lo, hi = part.split("-", 1)
                ids.extend(range(int(lo), int(hi) + 1))
            else:
                ids.append(int(part))
        if not ids:
            raise click.BadParameter("--sessions produced an empty list.", param_hint="--sessions")
        return ids

    # default: all sessions
    rows = db.list_sessions()
    if not rows:
        return []
    return [r["id"] for r in rows]


def _session_filename(db: "SessionDB", sid: int, fmt: str) -> str:
    """Return auto-generated filename for a single session."""
    row = db.get_session(sid)
    safe = row["name"].replace("/", "_").replace(" ", "_") if row else str(sid)
    return f"session_{sid}_{safe}.{fmt}"


def _build_csv_rows(db: "SessionDB", session_ids: list[int]) -> tuple[list[dict], list[str]]:
    """Return (all_rows, fieldnames) for a CSV export of the given sessions."""
    import csv as _csv, io as _io
    from datetime import datetime as _dt

    all_rows: list[dict] = []
    metric_keys: list[str] = []

    for sid in session_ids:
        data = db.export_session(sid)
        if data is None:
            continue
        sess = data["session"]

        duration_s = ""
        if sess.get("started_at") and sess.get("stopped_at"):
            try:
                t0 = _dt.fromisoformat(sess["started_at"])
                t1 = _dt.fromisoformat(sess["stopped_at"])
                duration_s = round((t1 - t0).total_seconds(), 1)
            except ValueError:
                pass

        results = db.get_results(sid)
        faults = data["faults"] or [{"kind": "", "parameters": {}}]

        for fault in faults:
            base = {
                "session_id": sess["id"],
                "name":        sess["name"],
                "status":      sess["status"],
                "started_at":  sess.get("started_at", ""),
                "stopped_at":  sess.get("stopped_at", ""),
                "duration_s":  duration_s,
                "fault_kind":  fault["kind"],
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
    return all_rows, base_cols + metric_keys


def _write_csv(rows: list[dict], fieldnames: list[str], dest: str) -> None:
    import csv as _csv, io as _io
    buf = _io.StringIO()
    writer = _csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    with open(dest, "w", newline="") as fh:
        fh.write(buf.getvalue())


# ── export ────────────────────────────────────────────────────────

@main.command()
@click.option("--session", "-i", default=None, type=int,
              help="Single session id to export.")
@click.option("--sessions", "-I", default="", metavar="IDS",
              help="Multiple session ids: '1,3,5' or range '1-5' or mixed '1,3-5'.")
@click.option("--format", "-f", "fmt", default="json",
              type=click.Choice(["json", "csv"]),
              help="Output format. Default: json")
@click.option("--output", "-o", default="", metavar="PATH",
              help="Output file path (single session). Ignored when --dir is set.")
@click.option("--dir", "-d", "out_dir", default="", metavar="DIR",
              help="Output directory. Files are auto-named inside it.")
@click.option("--split", is_flag=True, default=False,
              help="Write one file per session instead of a single combined file.")
def export(session, sessions, fmt, output, out_dir, split):
    """Export one or more sessions to JSON or CSV.

    \b
    Session selection
      --session 3                single session
      --sessions 1,3,5           specific sessions
      --sessions 1-5             range (inclusive)
      --sessions 1,3-5,8        mixed
      (no flag)                  all sessions

    \b
    Output location
      --output path.csv          explicit file (single session)
      --dir ./results/           write into a folder (auto-named files)
      --dir ./results/ --split   one file per session in the folder

    \b
    CSV columns: session_id, name, status, started_at, stopped_at,
                 duration_s, fault_kind, fault_parameters, <metrics...>

    \b
    Examples:
      chaos-jungle export --session 3 --format csv
      chaos-jungle export --sessions 1,3,5 --format csv --dir ./results/
      chaos-jungle export --sessions 1-10 --dir ./results/ --split
      chaos-jungle export --format csv --dir ./results/   # all sessions
    """
    import os
    from datetime import datetime

    db = SessionDB()

    # Resolve session list
    try:
        ids = _parse_session_ids(session, sessions or None, db)
    except click.UsageError:
        raise
    if not ids:
        click.echo("No sessions found.")
        return

    # Resolve output directory
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    # ── JSON ──────────────────────────────────────────────────────
    if fmt == "json":
        if split or (len(ids) == 1):
            # One file per session
            written: list[str] = []
            for sid in ids:
                payload = db.export_session(sid)
                if payload is None:
                    click.echo(f"[chaos-jungle] WARNING: session {sid} not found — skipped.", err=True)
                    continue
                content = json.dumps(payload, indent=2)
                if out_dir:
                    dest = os.path.join(out_dir, _session_filename(db, sid, "json"))
                elif output and len(ids) == 1:
                    dest = output
                else:
                    dest = _session_filename(db, sid, "json")
                with open(dest, "w") as fh:
                    fh.write(content)
                click.echo(f"[chaos-jungle] Exported session {sid} → {dest}")
                written.append(dest)
        else:
            # Combined file
            payload = [db.export_session(sid) for sid in ids if db.export_session(sid) is not None]
            content = json.dumps(payload, indent=2)
            if out_dir:
                dest = os.path.join(out_dir, "chaos_sessions.json")
            elif output:
                dest = output
            else:
                dest = "chaos_sessions.json"
            with open(dest, "w") as fh:
                fh.write(content)
            click.echo(f"[chaos-jungle] Exported {len(payload)} session(s) → {dest}")

    # ── CSV ───────────────────────────────────────────────────────
    elif fmt == "csv":
        if split:
            # One CSV per session
            for sid in ids:
                rows, fieldnames = _build_csv_rows(db, [sid])
                if not rows:
                    click.echo(f"[chaos-jungle] WARNING: session {sid} not found — skipped.", err=True)
                    continue
                if out_dir:
                    dest = os.path.join(out_dir, _session_filename(db, sid, "csv"))
                elif output and len(ids) == 1:
                    dest = output
                else:
                    dest = _session_filename(db, sid, "csv")
                _write_csv(rows, fieldnames, dest)
                click.echo(f"[chaos-jungle] Exported session {sid} → {dest}")
        else:
            # Combined file
            rows, fieldnames = _build_csv_rows(db, ids)
            if not rows:
                click.echo("No data to export.")
                return
            if out_dir:
                dest = os.path.join(out_dir, "chaos_sessions.csv")
            elif output:
                dest = output
            elif len(ids) == 1:
                dest = _session_filename(db, ids[0], "csv")
            else:
                dest = "chaos_sessions.csv"
            _write_csv(rows, fieldnames, dest)
            click.echo(f"[chaos-jungle] Exported {len(ids)} session(s) → {dest}")


# ── fetch ──────────────────────────────────────────────────────────

@main.command()
@click.option("--target", "-t", required=True,
              help="SSH target: ssh://user@host  or  ssh://user@host:port")
@click.option("--output-dir", "-o", default="./chaos-results",
              help="Local directory to save files into. Default: ./chaos-results")
@click.option("--remote-dir", default="~/.chaos-jungle",
              help="Remote directory to fetch from. Default: ~/.chaos-jungle")
@click.option("--files", "-f", default="chaos_jungle.db",
              help="Comma-separated filenames to fetch. Default: chaos_jungle.db")
@click.option("--glob", "-g", "glob_patterns", default="", metavar="PATTERNS",
              help="Comma-separated glob patterns expanded on the remote "
                   "(e.g. '*_wget_*.log,*_diff_*.log').")
@click.option("--export-csv/--no-export-csv", default=True,
              help="After fetching the DB, also export all sessions to CSV. Default: yes")
def fetch(target, output_dir, remote_dir, files, glob_patterns, export_csv):
    """Fetch result files from a remote SSH target.

    Downloads files from the remote chaos-jungle directory to a local
    output directory. Useful for collecting experiment results after a
    remote chaos run.

    \b
    Examples:
      # fetch DB only (default)
      chaos-jungle fetch --target ssh://ubuntu@10.0.0.5

      # fetch DB + named files + glob-matched logs
      chaos-jungle fetch --target ssh://ubuntu@10.0.0.5 \\
          --files "chaos_jungle.db,cj.db" \\
          --glob "*_wget_*.log,*_diff_*.log,*_cj.log" \\
          --output-dir ./results/

      # fetch without auto-exporting to CSV
      chaos-jungle fetch --target ssh://ubuntu@10.0.0.5 --no-export-csv
    """
    from chaos_jungle.fetch import fetch as _fetch

    file_list = [f.strip() for f in files.split(",") if f.strip()]
    pattern_list = [p.strip() for p in glob_patterns.split(",") if p.strip()]

    tgt = _make_target(target)
    try:
        result = _fetch(
            tgt,
            output_dir=output_dir,
            remote_dir=remote_dir,
            files=file_list,
            glob_patterns=pattern_list,
            export_csv=export_csv,
        )
    except Exception as e:
        click.echo(f"[chaos-jungle] ERROR: {e}", err=True)
        sys.exit(1)

    for local_path in result.fetched:
        click.echo(f"[chaos-jungle] Fetched  →  {local_path}")
    for remote_path in result.missing:
        click.echo(f"[chaos-jungle] MISSING  {remote_path} (skipped)", err=True)
    for remote_path, err in result.errors.items():
        click.echo(f"[chaos-jungle] ERROR    {remote_path}: {err}", err=True)

    if result.csv_path:
        click.echo(f"[chaos-jungle] CSV      →  {result.csv_path}")

    if result.fetched:
        click.echo(f"[chaos-jungle] Done. Files saved in: {result.output_dir}")


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


# ── scenarios ─────────────────────────────────────────────────────

@main.group("scenarios")
def scenarios_group():
    """Inspect and watch scenario registry entries."""


@scenarios_group.command("list")
@click.option("--status", "-s", default="", help="Filter by status: pending|running|done|failed")
@click.option("--type", "-t", "type_", default="", help="Filter by type: local|ssh|http")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
def scenarios_list(status, type_, as_json):
    """List all scenarios in the local registry."""
    from chaos_jungle.registry import ScenarioRegistry

    entries = ScenarioRegistry().list(
        status=status or None,
        type=type_ or None,
    )
    if as_json:
        click.echo(json.dumps(entries, indent=2))
        return
    if not entries:
        click.echo("No scenarios found.")
        return
    click.echo(f"{'ID':>36}  {'NAME':<25}  {'TYPE':<6}  {'TARGET':<18}  STATUS")
    click.echo("-" * 100)
    for e in entries:
        click.echo(
            f"{e['id']:>36}  {e['name']:<25}  {e['type']:<6}  "
            f"{(e.get('target_ip') or '-'):<18}  {e['status']}"
        )


@scenarios_group.command("status")
@click.argument("scenario_id")
@click.option("--target", "-t", default="", help="Remote target: ssh://user@host  http://host:port")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
def scenarios_status(scenario_id, target, as_json):
    """Check status of a scenario (local or remote)."""
    from chaos_jungle.registry import ScenarioRegistry

    if target:
        tgt = _make_target(target)
        entry = tgt.scenario_status(scenario_id)
    else:
        entry = ScenarioRegistry().get(scenario_id)

    if entry is None:
        click.echo(f"Scenario {scenario_id!r} not found.", err=True)
        sys.exit(1)

    if as_json:
        click.echo(json.dumps(entry))
        return

    click.echo(f"ID      : {entry['id']}")
    click.echo(f"Name    : {entry['name']}")
    click.echo(f"Type    : {entry['type']}")
    click.echo(f"Status  : {entry['status']}")
    if entry.get("target_ip"):
        click.echo(f"Target  : {entry['target_ip']}")
    if entry.get("session_id"):
        click.echo(f"Session : {entry['session_id']}")


@scenarios_group.command("watch")
@click.argument("scenario_ids", nargs=-1, required=True)
@click.option("--target", "-t", default="", help="Remote target: ssh://user@host  http://host:port")
@click.option("--interval", default=5.0, type=float, help="Poll interval in seconds. Default: 5")
@click.option("--timeout", default=600.0, type=float, help="Max wait in seconds. Default: 600")
def scenarios_watch(scenario_ids, target, interval, timeout):
    """Watch one or more scenarios until they finish.

    \b
    Examples:
      cj scenarios watch abc123
      cj scenarios watch abc123 def456
      cj scenarios watch abc123 --target ssh://ubuntu@10.0.0.5
    """
    import time
    from chaos_jungle.registry import ScenarioRegistry

    tgt = _make_target(target) if target else None
    registry = ScenarioRegistry()
    pending = set(scenario_ids)
    deadline = time.monotonic() + timeout

    while pending:
        for sid in list(pending):
            if tgt is not None:
                entry = tgt.scenario_status(sid)
            else:
                entry = registry.get(sid)

            if entry is None:
                click.echo(f"[{sid[:8]}] not found in registry", err=True)
                pending.discard(sid)
                continue

            ts = time.strftime("%H:%M:%S")
            click.echo(f"[{ts}] {sid[:8]}  {entry['name']}  → {entry['status']}")

            if entry["status"] in ("done", "failed"):
                pending.discard(sid)

        if pending:
            if time.monotonic() > deadline:
                click.echo(f"Timed out after {timeout}s. Still pending: {pending}", err=True)
                sys.exit(1)
            time.sleep(interval)

    click.echo("all done")


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
