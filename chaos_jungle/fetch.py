"""Programmatic fetch API — download result files from remote targets.

Use this module to collect experiment data (DB, logs, custom files) from
remote machines after a chaos run, without leaving Python.

Examples
--------
Basic fetch (DB only, auto-exports CSV)::

    from chaos_jungle import SSHTarget
    from chaos_jungle.fetch import fetch

    result = fetch(
        SSHTarget("node1", user="ubuntu"),
        output_dir="./results/node1/",
    )
    print(result.fetched)   # ['.../chaos_jungle.db']
    print(result.csv_path)  # '.../chaos_sessions.csv'

Fetch DB + application logs with glob patterns (IRIS-style)::

    result = fetch(
        SSHTarget("node1", user="ubuntu", key="~/.ssh/id_geni_ssh_rsa"),
        output_dir="./results/node1/",
        files=["chaos_jungle.db", "cj.db"],
        glob_patterns=["*_wget_*.log", "*_diff_*.log", "*_cj.log"],
    )

Collect logs from multiple nodes in parallel::

    import concurrent.futures
    from chaos_jungle import SSHTarget
    from chaos_jungle.fetch import collect_logs

    nodes = [
        SSHTarget("10.0.0.1", user="ubuntu"),
        SSHTarget("10.0.0.2", user="ubuntu"),
        SSHTarget("10.0.0.3", user="ubuntu"),
    ]

    def _fetch_node(target):
        return collect_logs(
            target,
            output_dir=f"./results/{target.host}/",
            patterns=["*_wget_*.log", "*_diff_*.log", "*_cj.log"],
        )

    with concurrent.futures.ThreadPoolExecutor() as pool:
        all_logs = list(pool.map(_fetch_node, nodes))
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from chaos_jungle.targets.base import Target


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class FetchResult:
    """Result of a :func:`fetch` call.

    Attributes
    ----------
    output_dir :
        Absolute path to the local directory files were saved into.
    fetched :
        Local paths of successfully downloaded files.
    missing :
        Remote paths that did not exist on the target (skipped).
    errors :
        Mapping of ``remote_path → error message`` for files that failed
        for reasons other than being missing.
    csv_path :
        Path to the auto-generated CSV file, or ``None`` if CSV export was
        skipped or the DB was not fetched.
    """

    output_dir: str
    fetched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    csv_path: str | None = None

    def __repr__(self) -> str:
        return (
            f"FetchResult(fetched={len(self.fetched)}, "
            f"missing={len(self.missing)}, "
            f"errors={len(self.errors)}, "
            f"csv={self.csv_path!r})"
        )


# ---------------------------------------------------------------------------
# Main API
# ---------------------------------------------------------------------------

def fetch(
    target: "Target",
    output_dir: str = "./chaos-results",
    remote_dir: str = "~/.chaos-jungle",
    files: Sequence[str] = ("chaos_jungle.db",),
    glob_patterns: Sequence[str] = (),
    export_csv: bool = True,
) -> FetchResult:
    """Download result files from a remote target.

    Connects to ``target`` via SSH, downloads the requested files to
    ``output_dir``, and optionally auto-exports all sessions in the fetched
    DB to a CSV file.

    Parameters
    ----------
    target :
        A connected or unconnected :class:`~chaos_jungle.targets.ssh.SSHTarget`.
        The connection is managed internally — do not pre-connect.
    output_dir :
        Local directory to write files into. Created if it does not exist.
    remote_dir :
        Base directory on the remote machine. Default ``~/.chaos-jungle``.
    files :
        Specific filenames to download from ``remote_dir``.
        Default ``("chaos_jungle.db",)``.
    glob_patterns :
        Shell glob patterns expanded on the remote machine.
        Files matching any pattern inside ``remote_dir`` are downloaded.
        Example: ``["*_wget_*.log", "*_diff_*.log", "*_cj.log"]``.
    export_csv :
        If ``True`` (default) and ``chaos_jungle.db`` was fetched,
        automatically export all sessions to ``chaos_sessions.csv`` in
        ``output_dir``.

    Returns
    -------
    FetchResult
        Contains lists of fetched paths, missing paths, errors, and the
        CSV path if generated.

    Raises
    ------
    RuntimeError
        If the SSH connection fails.

    Examples
    --------
    ::

        from chaos_jungle import SSHTarget
        from chaos_jungle.fetch import fetch

        # Basic — fetch DB and auto-export CSV
        result = fetch(SSHTarget("node1", user="ubuntu"))

        # Fetch DB + IRIS-style wget/diff/cj logs
        result = fetch(
            SSHTarget("node1", user="ubuntu", key="~/.ssh/id_geni_ssh_rsa"),
            output_dir="./results/node1/",
            files=["chaos_jungle.db", "cj.db"],
            glob_patterns=["*_wget_*.log", "*_diff_*.log", "*_cj.log"],
        )
        print(f"Downloaded {len(result.fetched)} files")
        print(f"CSV: {result.csv_path}")
    """
    os.makedirs(output_dir, exist_ok=True)
    result = FetchResult(output_dir=os.path.abspath(output_dir))

    target.connect()
    try:
        # Resolve ~ on remote
        _, home_out, _ = target.run("echo $HOME")
        remote_home = home_out.strip() or "/root"
        base = remote_dir.replace("~", remote_home)

        # Build list of filenames to download
        to_fetch: list[str] = list(files)

        # Expand glob patterns on the remote
        for pattern in glob_patterns:
            _, ls_out, _ = target.run(f"ls {base}/{pattern} 2>/dev/null")
            for line in ls_out.splitlines():
                fname = os.path.basename(line.strip())
                if fname:
                    to_fetch.append(fname)

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique_files: list[str] = []
        for f in to_fetch:
            if f not in seen:
                seen.add(f)
                unique_files.append(f)

        # Download each file
        for filename in unique_files:
            remote_path = f"{base}/{filename}"
            local_path = os.path.join(output_dir, filename)
            try:
                target.get(remote_path, local_path)
                result.fetched.append(local_path)
            except FileNotFoundError:
                result.missing.append(remote_path)
            except Exception as exc:
                result.errors[remote_path] = str(exc)

    finally:
        target.disconnect()

    # Auto-export CSV from fetched DB
    if export_csv:
        db_path = os.path.join(output_dir, "chaos_jungle.db")
        if os.path.isfile(db_path):
            csv_path = export_db_to_csv(db_path, output_dir)
            if csv_path:
                result.csv_path = csv_path

    return result


def collect_logs(
    target: "Target",
    output_dir: str,
    remote_dir: str = "~/.chaos-jungle",
    patterns: Sequence[str] = ("*.log",),
) -> list[str]:
    """Download log files matching glob patterns from a remote target.

    A convenience wrapper around :func:`fetch` for collecting application
    and chaos log files without fetching the DB.

    Parameters
    ----------
    target :
        An SSH target.
    output_dir :
        Local directory to write logs into.
    remote_dir :
        Remote directory to search in.  Default ``~/.chaos-jungle``.
    patterns :
        Glob patterns to match against filenames in ``remote_dir``.
        Default ``("*.log",)``.

    Returns
    -------
    list[str]
        Local paths of successfully downloaded files.

    Examples
    --------
    Collect IRIS-style transfer and corruption logs from a node::

        from chaos_jungle import SSHTarget
        from chaos_jungle.fetch import collect_logs

        logs = collect_logs(
            SSHTarget("node1", user="ubuntu", key="~/.ssh/id_geni_ssh_rsa"),
            output_dir="./results/node1/",
            patterns=["*_wget_*.log", "*_diff_*.log", "*_cj.log"],
        )
        print(f"Collected {len(logs)} log files")

    Collect from multiple nodes in parallel::

        import concurrent.futures
        from chaos_jungle import SSHTarget
        from chaos_jungle.fetch import collect_logs

        nodes = {
            "node1": SSHTarget("10.0.0.1", user="ubuntu"),
            "node2": SSHTarget("10.0.0.2", user="ubuntu"),
        }
        log_patterns = ["*_wget_*.log", "*_diff_*.log", "*_cj.log"]

        def _do_fetch(item):
            name, tgt = item
            return name, collect_logs(
                tgt,
                output_dir=f"./results/{name}/",
                patterns=log_patterns,
            )

        with concurrent.futures.ThreadPoolExecutor() as pool:
            all_results = dict(pool.map(_do_fetch, nodes.items()))
    """
    result = fetch(
        target,
        output_dir=output_dir,
        remote_dir=remote_dir,
        files=[],
        glob_patterns=list(patterns),
        export_csv=False,
    )
    return result.fetched


# ---------------------------------------------------------------------------
# DB → CSV helper (also used by CLI)
# ---------------------------------------------------------------------------

def export_db_to_csv(db_path: str, output_dir: str) -> str:
    """Export all sessions from a local SQLite DB file to a CSV file.

    Parameters
    ----------
    db_path :
        Path to a ``chaos_jungle.db`` file (can be a remotely fetched copy).
    output_dir :
        Directory to write ``chaos_sessions.csv`` into.

    Returns
    -------
    str
        Absolute path to the written CSV, or ``""`` if the DB has no sessions.

    Examples
    --------
    ::

        from chaos_jungle.fetch import export_db_to_csv

        csv_path = export_db_to_csv(
            "./results/chaos_jungle.db",
            "./results/",
        )
        import pandas as pd
        df = pd.read_csv(csv_path)
        print(df[["name", "fault_kind", "duration_s", "chaos_ping_avg_ms"]])
    """
    from chaos_jungle.db.session_db import SessionDB

    db = SessionDB(path=db_path)
    session_rows = db.list_sessions()
    if not session_rows:
        return ""

    all_rows: list[dict] = []
    metric_keys: list[str] = []

    for sess_row in session_rows:
        sid = sess_row["id"]
        data = db.export_session(sid)
        sess = data["session"]

        duration_s: float | str = ""
        if sess.get("started_at") and sess.get("stopped_at"):
            try:
                from datetime import datetime as _dt
                t0 = _dt.fromisoformat(sess["started_at"])
                t1 = _dt.fromisoformat(sess["stopped_at"])
                duration_s = round((t1 - t0).total_seconds(), 1)
            except ValueError:
                pass

        results = db.get_results(sid)
        faults = data["faults"] or [{"kind": "", "parameters": {}}]

        for fault in faults:
            base: dict = {
                "session_id":        sess["id"],
                "name":              sess["name"],
                "status":            sess["status"],
                "started_at":        sess.get("started_at", ""),
                "stopped_at":        sess.get("stopped_at", ""),
                "duration_s":        duration_s,
                "fault_kind":        fault["kind"],
                "fault_parameters":  json.dumps(fault["parameters"]),
            }
            metrics: dict = {}
            if results:
                m = results[0].get("metrics", {})
                if isinstance(m, dict):
                    metrics = m
            base.update(metrics)
            for k in metrics:
                if k not in metric_keys:
                    metric_keys.append(k)
            all_rows.append(base)

    import csv as csv_mod
    base_cols = [
        "session_id", "name", "status", "started_at", "stopped_at",
        "duration_s", "fault_kind", "fault_parameters",
    ]
    fieldnames = base_cols + metric_keys
    csv_path = os.path.join(output_dir, "chaos_sessions.csv")

    with open(csv_path, "w", newline="") as fh:
        writer = csv_mod.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in all_rows:
            writer.writerow(row)

    return os.path.abspath(csv_path)
