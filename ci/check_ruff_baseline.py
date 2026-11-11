"""Reject new Ruff findings while allowing the checked-in legacy findings to shrink."""

import argparse
from collections import Counter
import json
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "ci" / "ruff-baseline.json"


def fingerprint(diagnostic):
    path = Path(diagnostic["filename"])
    source = path.read_text(encoding="utf-8").splitlines()[diagnostic["location"]["row"] - 1]
    return (
        path.relative_to(ROOT).as_posix(),
        diagnostic["code"],
        diagnostic["message"],
        source.strip(),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args()

    ruff = shutil.which("ruff")
    if ruff is None:
        parser.error("ruff is not installed")
    result = subprocess.run(
        [ruff, "check", "chaos_jungle/", "tests/", "--output-format", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        print(result.stderr or result.stdout, file=sys.stderr)
        return result.returncode

    diagnostics = json.loads(result.stdout)
    current = Counter(fingerprint(item) for item in diagnostics)
    if args.write_baseline:
        entries = [
            {"path": path, "code": code, "message": message, "source": source, "count": count}
            for (path, code, message, source), count in sorted(current.items())
        ]
        BASELINE.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {sum(current.values())} existing findings to {BASELINE.relative_to(ROOT)}")
        return 0

    entries = json.loads(BASELINE.read_text(encoding="utf-8"))
    baseline = Counter(
        {(item["path"], item["code"], item["message"], item["source"]): item["count"]
         for item in entries}
    )
    added = current - baseline
    if added:
        print(f"{sum(added.values())} new Ruff finding(s):", file=sys.stderr)
        for (path, code, message, source), count in sorted(added.items()):
            print(f"  {path}: {code} {message} (x{count})\n    {source}", file=sys.stderr)
        return 1

    print(f"Ruff: no new findings ({sum(current.values())} existing, "
          f"{sum(baseline.values())} baselined).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
