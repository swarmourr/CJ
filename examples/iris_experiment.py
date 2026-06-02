#!/usr/bin/env python3
"""IRIS Experiment — Python rewrite using the chaos-jungle package.

Replicates experiment/v3/run_experiement.sh using the chaos-jungle Python
package (SSHTarget, StorageCorrupt, SilentNetworkCorrupt, ChaosRunner,
collect_logs).

Usage
-----
    python iris_experiment.py \\
        --output-dir /root/results/v3 \\
        --user root \\
        --ssh-key ~/.ssh/id_geni_ssh_rsa \\
        --site-dir /var/www/iris \\
        --iris-dir /root/iris \\
        --template-dir /root/iris/testdata/20190425T121649-0700 \\
        --parse-logs /path/to/experiment/v3/parse_logs.py

Topology files (read from --output-dir):
    CORRUPT_NODES   : IP HOSTNAME [prob]
    CORRUPT_EDGES   : export LINKNAME=IP [prob]
    nodes_all       : IP HOSTNAME
    nodes_end       : IP HOSTNAME
    NODES_SRC       : IP [...]
    NODES_DEST      : IP [...]
    edges_all.sh    : export LINKNAME=IP
    allfiles        : relative paths of template files (generated here if missing)

Notes on StorageCorrupt vs original:
    The original run_experiement.sh calls cj_storage.py --onetime, which
    performs a single one-shot corruption before file transfers begin.
    The CJ package's StorageCorrupt uses a crontab-based periodic schedule
    (default every 10m). Files are corrupted on a recurring interval while
    the chaos runner is active (during transfers). This is a behaviorally
    different but equivalent chaos pattern.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime

from chaos_jungle import SSHTarget, ChaosRunner, Scenario
from chaos_jungle.faults import StorageCorrupt, SilentNetworkCorrupt
from chaos_jungle.fetch import collect_logs


# ─────────────────────────────────────────────── data types ──────────────────

@dataclass
class Node:
    ip: str
    hostname: str


@dataclass
class CorruptNode:
    ip: str
    hostname: str
    prob: float = 1.0


@dataclass
class CorruptEdge:
    link_name: str
    link_ip: str
    prob: float = 0.002

    @property
    def node_hostname(self) -> str:
        """First component of LINKNAME, e.g. 'ESNET' from 'ESNET_Link1'."""
        return self.link_name.split("_")[0]


# ─────────────────────────────────────────────── topology parsing ─────────────

def _iter_lines(path: str):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                yield line


def parse_nodes_file(path: str) -> list[Node]:
    """Parse nodes_all / nodes_end / NODES_SRC / NODES_DEST.
    Format: IP HOSTNAME [extra...]
    """
    nodes: list[Node] = []
    if not os.path.isfile(path):
        return nodes
    for line in _iter_lines(path):
        parts = line.split()
        ip = parts[0]
        hostname = parts[1] if len(parts) > 1 else ip
        nodes.append(Node(ip, hostname))
    return nodes


def parse_corrupt_nodes(path: str) -> list[CorruptNode]:
    """Parse CORRUPT_NODES. Format: IP HOSTNAME [prob]"""
    result: list[CorruptNode] = []
    for line in _iter_lines(path):
        parts = line.split()
        ip = parts[0]
        hostname = parts[1] if len(parts) > 1 else ip
        prob = float(parts[2]) if len(parts) > 2 else 1.0
        result.append(CorruptNode(ip, hostname, prob))
    return result


def parse_corrupt_edges(path: str) -> list[CorruptEdge]:
    """Parse CORRUPT_EDGES. Format: export LINKNAME=IP [prob]"""
    result: list[CorruptEdge] = []
    for line in _iter_lines(path):
        line = line.removeprefix("export ").strip()
        parts = line.split()
        m = re.match(r"(\w+)=([\d.]+)", parts[0])
        if not m:
            continue
        link_name, link_ip = m.group(1), m.group(2)
        prob = float(parts[1]) if len(parts) > 1 else 0.002
        result.append(CorruptEdge(link_name, link_ip, prob))
    return result


def parse_edges_sh(path: str) -> dict[str, str]:
    """Parse edges_all.sh (bash source). Returns LINKNAME → virtual_IP."""
    edges: dict[str, str] = {}
    for line in _iter_lines(path):
        line = line.removeprefix("export ").strip()
        m = re.match(r"(\w+)=([\d.]+)", line)
        if m:
            edges[m.group(1)] = m.group(2)
    return edges


def get_virtual_ip(hostname: str, all_edges: dict[str, str]) -> str:
    """Return first virtual IP for a node (link names are NodeX_NodeY)."""
    for link_name, ip in all_edges.items():
        if link_name.split("_")[0] == hostname:
            return ip
    return ""


def get_node_ip(hostname: str, all_nodes: list[Node]) -> str:
    for n in all_nodes:
        if n.hostname == hostname:
            return n.ip
    return ""


# ─────────────────────────────────────────────── SSH helpers ─────────────────

def make_target(ip: str, user: str, key: str) -> SSHTarget:
    return SSHTarget(ip, user=user, key=key)


def wait_for_marker(ip: str, user: str, key: str, pattern: str,
                    poll: float = 1.0) -> None:
    """Poll ls /root/ on the node until a file matching pattern appears."""
    t = make_target(ip, user, key)
    t.connect()
    try:
        while True:
            _, out, _ = t.run(f"ls /root/ 2>/dev/null | grep -i {pattern!r}")
            if out.strip():
                return
            time.sleep(poll)
    finally:
        t.disconnect()


# ─────────────────────────────────────────────── workload ────────────────────

def generate_allfiles(
    any_node_ip: str,
    template_dir: str,
    output_dir: str,
    user: str,
    key: str,
) -> str:
    """Generate the 'allfiles' listing from any node. Returns local path."""
    allfiles_path = os.path.join(output_dir, "allfiles")
    t = make_target(any_node_ip, user, key)
    t.connect()
    try:
        parent = os.path.dirname(template_dir.rstrip("/"))
        base = os.path.basename(template_dir.rstrip("/"))
        _, out, _ = t.run(f"cd {parent} && find {base} -type f")
        with open(allfiles_path, "w") as f:
            f.write(out)
    finally:
        t.disconnect()
    return allfiles_path


def create_run_data(
    run: int,
    src_nodes: list[Node],
    dest_nodes: list[Node],
    site_dir: str,
    iris_dir: str,
    user: str,
    key: str,
) -> None:
    """Clean dest nodes and launch src_create_data.sh on all src nodes."""
    run_dir = f"{site_dir}/run{run}"

    def _clean_dest(node: Node) -> None:
        t = make_target(node.ip, user, key)
        t.connect()
        try:
            t.run(f"rm -rf {iris_dir}/*.*.*/* 2>/dev/null || true")
        finally:
            t.disconnect()

    def _create_src(node: Node) -> None:
        t = make_target(node.ip, user, key)
        t.connect()
        try:
            t.run(f"nohup /root/src_create_data.sh {run_dir} > /dev/null 2>&1 &")
        finally:
            t.disconnect()

    with concurrent.futures.ThreadPoolExecutor() as pool:
        list(pool.map(_clean_dest, dest_nodes))
    with concurrent.futures.ThreadPoolExecutor() as pool:
        list(pool.map(_create_src, src_nodes))


def run_transfers(
    run: int,
    end_nodes: list[Node],
    src_ips: set[str],
    dest_ips: set[str],
    all_edges: dict[str, str],
    iris_dir: str,
    template_dir: str,
    user: str,
    key: str,
) -> None:
    """Drive all src→dest wget+diff transfers for this run in parallel.

    Replicates _transfer_files() from include.sh: for each pair (dest_i, src_j)
    where dest_i is in DEST_NODES and src_j is in SRC_NODES, wget the run
    directory from src and diff against the template on dest.
    """
    n = len(end_nodes)
    tasks = []

    for x in range(1, n):
        for i in range(n):
            j = (i + x) % n
            d_node = end_nodes[i]
            s_node = end_nodes[j]

            if d_node.ip not in dest_ips:
                continue
            if s_node.ip not in src_ips:
                continue

            s_vip = get_virtual_ip(s_node.hostname, all_edges)
            if not s_vip:
                print(f"  [warn] no virtual IP for {s_node.hostname}, skipping pair")
                continue

            logfile = f"{d_node.hostname}_run{run}_wget_{s_node.hostname}.log"
            diff_log = f"{d_node.hostname}_run{run}_diff_{s_node.hostname}.log"
            tasks.append((d_node, s_node, s_vip, logfile, diff_log, x))

    def _do_transfer(task: tuple) -> None:
        d_node, s_node, s_vip, logfile, diff_log, x = task
        # Wait for src data to be ready
        wait_for_marker(s_node.ip, user, key, "src_create_data_done")

        t = make_target(d_node.ip, user, key)
        t.connect()
        try:
            tmpl_base = os.path.basename(template_dir.rstrip("/"))
            # wget transfer
            t.run(
                f"rm -rf {iris_dir}/{s_vip}/run* 2>/dev/null || true; "
                f"wget -q -P {iris_dir} -r -m --no-parent -R 'index.html*' "
                f"http://{s_vip}/run{run}/ > /root/{logfile} 2>&1"
            )
            # integrity check
            t.run(
                f"diff -qr {template_dir} "
                f"{iris_dir}/{s_vip}/run{run}/{tmpl_base} "
                f"> /root/{diff_log} 2>&1; "
                f"rm -rf {iris_dir}/{s_vip}/run{run}"
            )
            # completion marker (mirrors dest_transfer_diff.sh)
            t.run(f"echo done > /root/{run}_{x}_transfer_diff_done")
        finally:
            t.disconnect()

    with concurrent.futures.ThreadPoolExecutor() as pool:
        list(pool.map(_do_transfer, tasks))

    print(f"  [run{run}] all transfers done")


# ─────────────────────────────────────────────── storage experiments ──────────

def run_storage_experiments(
    corrupt_nodes: list[CorruptNode],
    end_nodes: list[Node],
    src_nodes: list[Node],
    dest_nodes: list[Node],
    all_edges: dict[str, str],
    result_dir: str,
    run_label_path: str,
    start_run: int,
    site_dir: str,
    iris_dir: str,
    template_dir: str,
    user: str,
    key: str,
) -> int:
    """Run one ChaosRunner per corrupt node. Returns the last run number used."""
    src_ips = {n.ip for n in src_nodes}
    dest_ips = {n.ip for n in dest_nodes}
    run = start_run - 1

    for cn in corrupt_nodes:
        run += 1
        run_dir = f"{site_dir}/run{run}"
        print(f"\n### Run {run}: StorageCorrupt — {cn.hostname} ({cn.ip})")

        # Create fresh run data on all src nodes
        create_run_data(run, src_nodes, dest_nodes, site_dir, iris_dir, user, key)

        # StorageCorrupt: crontab-based periodic corruption while transfers run.
        # Original used --onetime (single-shot). This continuously corrupts
        # during the transfer window, which exercises the same integrity path.
        fault = StorageCorrupt(
            pattern="*",
            directory=run_dir,
            interval="1m",  # short interval so corruption fires during transfers
            recursive=True,
        )
        scenario = Scenario(f"storage-run{run}", faults=[fault])
        runner = ChaosRunner(scenario, make_target(cn.ip, user, key))

        runner.start()
        print(f"  [run{run}] storage corruption active on {cn.hostname}")

        run_transfers(run, end_nodes, src_ips, dest_ips, all_edges,
                      iris_dir, template_dir, user, key)

        runner.stop()
        print(f"  [run{run}] corruption stopped and reverted")

        # Collect cj.log from the corrupt node
        t = make_target(cn.ip, user, key)
        t.connect()
        try:
            dest_log = os.path.join(result_dir, f"{cn.hostname}_run{run}_cj.log")
            t.get("/var/log/cj.log", dest_log)
            t.run("sudo rm -f /var/log/cj.log")
        except Exception as exc:
            print(f"  [warn] cj.log not collected from {cn.hostname}: {exc}")
        finally:
            t.disconnect()

        # Record label for parse_logs.py
        with open(run_label_path, "a") as f:
            f.write(f"run{run} {cn.hostname}\n")

        print(f"  [run{run}] done")

    return run


# ─────────────────────────────────────────────── network experiments ──────────

def run_network_experiments(
    corrupt_edges: list[CorruptEdge],
    all_nodes: list[Node],
    end_nodes: list[Node],
    src_nodes: list[Node],
    dest_nodes: list[Node],
    all_edges: dict[str, str],
    result_dir: str,
    run_label_path: str,
    start_run: int,
    site_dir: str,
    iris_dir: str,
    template_dir: str,
    user: str,
    key: str,
) -> int:
    """Run one ChaosRunner per corrupt edge. Returns the last run number used."""
    src_ips = {n.ip for n in src_nodes}
    dest_ips = {n.ip for n in dest_nodes}
    run = start_run - 1

    for edge in corrupt_edges:
        run += 1
        print(f"\n### Run {run}: NetworkCorrupt — {edge.link_name} ({edge.link_ip})")

        node_ip = get_node_ip(edge.node_hostname, all_nodes)
        if not node_ip:
            print(f"  [warn] could not resolve IP for {edge.node_hostname!r}, skipping")
            continue

        # Convert probability to 1-in-N packet rate (mirrors _get_network_probablity)
        if edge.prob <= 0:
            print(f"  [run{run}] prob=0, running baseline (no fault)")
            create_run_data(run, src_nodes, dest_nodes, site_dir, iris_dir, user, key)
            run_transfers(run, end_nodes, src_ips, dest_ips, all_edges,
                          iris_dir, template_dir, user, key)
            continue

        rate = max(1, int(round(1.0 / edge.prob)))

        # Create fresh run data
        create_run_data(run, src_nodes, dest_nodes, site_dir, iris_dir, user, key)

        # SilentNetworkCorrupt: BPF/XDP payload corruption via virtual link IP.
        # link_ip auto-resolves the interface at start() time via iface_for_ip().
        fault = SilentNetworkCorrupt(rate=rate, hook="tc", link_ip=edge.link_ip)
        scenario = Scenario(f"network-run{run}", faults=[fault])
        runner = ChaosRunner(scenario, make_target(node_ip, user, key))

        runner.start()
        print(f"  [run{run}] BPF corruption active — {edge.link_name} "
              f"rate=1/{rate} ({edge.link_ip})")

        run_transfers(run, end_nodes, src_ips, dest_ips, all_edges,
                      iris_dir, template_dir, user, key)

        runner.stop()
        print(f"  [run{run}] corruption stopped")

        # Record link label for parse_logs.py
        with open(run_label_path, "a") as f:
            f.write(f"run{run} {edge.link_name}\n")

        print(f"  [run{run}] done")

    return run


# ─────────────────────────────────────────────── log collection ───────────────

def collect_all_logs(
    dest_nodes: list[Node],
    result_dir: str,
    user: str,
    key: str,
) -> None:
    """Fetch wget+diff logs from all dest nodes, then clean remote copies."""
    print("\nCollecting logs from dest nodes...")

    def _fetch_node(node: Node) -> list[str]:
        logs = collect_logs(
            make_target(node.ip, user, key),
            output_dir=result_dir,
            remote_dir="~",
            patterns=["*_wget_*.log", "*_diff_*.log"],
        )
        # Clean up remote
        t = make_target(node.ip, user, key)
        t.connect()
        try:
            t.run(
                "rm -f ~/*_transfer_diff_done ~/*_diff_*.log "
                "~/*_wget_*.log ~/*diff_done 2>/dev/null || true"
            )
        finally:
            t.disconnect()
        return logs

    with concurrent.futures.ThreadPoolExecutor() as pool:
        results = list(pool.map(_fetch_node, dest_nodes))

    total = sum(len(r) for r in results)
    print(f"Collected {total} log files")


# ─────────────────────────────────────────────── CLI entry point ──────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="IRIS chaos experiment — Python rewrite using chaos-jungle",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--output-dir", default="/root/results/v3",
                        help="Local directory containing topology files and results")
    parser.add_argument("--user", default="root",
                        help="SSH username for all nodes")
    parser.add_argument("--ssh-key", default="~/.ssh/id_geni_ssh_rsa",
                        help="Path to SSH private key")
    parser.add_argument("--site-dir", default="/var/www/iris",
                        help="Apache2 document root on nodes (where run dirs live)")
    parser.add_argument("--iris-dir", default="/root/iris",
                        help="IRIS working directory on dest nodes (wget output)")
    parser.add_argument("--template-dir",
                        default="/root/iris/testdata/20190425T121649-0700",
                        help="Reference template directory for diff checks")
    parser.add_argument("--any-node-ip", default="",
                        help="IP of any node (used to generate allfiles if missing)")
    parser.add_argument("--start-run", type=int, default=1,
                        help="Run number to start from")
    parser.add_argument("--parse-logs",
                        default=os.path.join(
                            os.path.dirname(__file__),
                            "../../../chaos-jungle/experiment/v3/parse_logs.py"
                        ),
                        help="Path to parse_logs.py")
    parser.add_argument("--csv-name", default="",
                        help="Rename output CSV to this name (default: <result_dir>.csv)")
    args = parser.parse_args()

    output_dir = os.path.abspath(os.path.expanduser(args.output_dir))
    key = os.path.expanduser(args.ssh_key)

    # ── Load topology ─────────────────────────────────────────────────────
    def topo(name: str) -> str:
        return os.path.join(output_dir, name)

    all_nodes     = parse_nodes_file(topo("nodes_all"))
    end_nodes     = parse_nodes_file(topo("nodes_end"))
    src_nodes     = parse_nodes_file(topo("NODES_SRC"))
    dest_nodes    = parse_nodes_file(topo("NODES_DEST"))
    corrupt_nodes = parse_corrupt_nodes(topo("CORRUPT_NODES"))
    corrupt_edges = parse_corrupt_edges(topo("CORRUPT_EDGES"))
    all_edges     = parse_edges_sh(topo("edges_all.sh"))

    print(f"Topology loaded from {output_dir}")
    print(f"  end nodes : {[n.hostname for n in end_nodes]}")
    print(f"  src nodes : {[n.hostname for n in src_nodes]}")
    print(f"  dest nodes: {[n.hostname for n in dest_nodes]}")
    print(f"  corrupt nodes: {[cn.hostname for cn in corrupt_nodes]}")
    print(f"  corrupt edges: {[ce.link_name for ce in corrupt_edges]}")

    # ── Create timestamped result directory ───────────────────────────────
    ts = datetime.now().strftime("%Y%m%d_%H%M%p")
    result_dir = os.path.join(output_dir, f"output_{ts}")
    os.makedirs(result_dir, exist_ok=True)
    print(f"\nResult dir: {result_dir}")

    # Copy static topology files into result dir
    for fname in ("node_router", "allfiles"):
        src_path = topo(fname)
        if os.path.isfile(src_path):
            shutil.copy2(src_path, result_dir)

    # Generate allfiles if missing
    allfiles_dest = os.path.join(result_dir, "allfiles")
    if not os.path.isfile(allfiles_dest):
        if args.any_node_ip:
            print("Generating allfiles from any_node_ip...")
            generate_allfiles(args.any_node_ip, args.template_dir,
                              result_dir, args.user, key)
        else:
            print("[warn] allfiles not found and --any-node-ip not set; "
                  "parse_logs.py missing-file detection will be skipped")

    # Initialise run_label_autogen
    run_label_path = os.path.join(result_dir, "run_label_autogen")
    open(run_label_path, "w").close()

    # ── Storage experiments ───────────────────────────────────────────────
    last_run = run_storage_experiments(
        corrupt_nodes=corrupt_nodes,
        end_nodes=end_nodes,
        src_nodes=src_nodes,
        dest_nodes=dest_nodes,
        all_edges=all_edges,
        result_dir=result_dir,
        run_label_path=run_label_path,
        start_run=args.start_run,
        site_dir=args.site_dir,
        iris_dir=args.iris_dir,
        template_dir=args.template_dir,
        user=args.user,
        key=key,
    )

    # ── Network experiments ───────────────────────────────────────────────
    last_run = run_network_experiments(
        corrupt_edges=corrupt_edges,
        all_nodes=all_nodes,
        end_nodes=end_nodes,
        src_nodes=src_nodes,
        dest_nodes=dest_nodes,
        all_edges=all_edges,
        result_dir=result_dir,
        run_label_path=run_label_path,
        start_run=last_run + 1,
        site_dir=args.site_dir,
        iris_dir=args.iris_dir,
        template_dir=args.template_dir,
        user=args.user,
        key=key,
    )

    # ── Collect logs from dest nodes ──────────────────────────────────────
    collect_all_logs(dest_nodes, result_dir, args.user, key)

    # ── Parse logs → CSV ──────────────────────────────────────────────────
    parse_logs_path = os.path.abspath(args.parse_logs)
    if os.path.isfile(parse_logs_path):
        env = os.environ.copy()
        env["RUN_LINKLABEL_FILE"] = "run_label_autogen"
        print(f"\nParsing logs → CSV ...")
        subprocess.run(
            [sys.executable, parse_logs_path, result_dir],
            env=env,
            check=True,
        )
        # Optionally rename CSV
        if args.csv_name:
            default_csv = os.path.join(
                result_dir, os.path.basename(result_dir) + ".csv"
            )
            target_csv = os.path.join(result_dir, args.csv_name)
            if os.path.isfile(default_csv):
                os.rename(default_csv, target_csv)
                print(f"CSV saved as: {target_csv}")
    else:
        print(f"\n[warn] parse_logs.py not found at {parse_logs_path}")
        print(f"  Run manually:  python parse_logs.py {result_dir}")

    # Save combined topology snapshot in result dir
    combined = os.path.join(result_dir, f"{os.path.basename(result_dir)}.txt")
    with open(combined, "w") as f:
        for src_name in ("CORRUPT_NODES", "CORRUPT_EDGES"):
            src_path = topo(src_name)
            if os.path.isfile(src_path):
                f.write(open(src_path).read())

    print(f"\nExperiment complete. Results: {result_dir}")


if __name__ == "__main__":
    main()
