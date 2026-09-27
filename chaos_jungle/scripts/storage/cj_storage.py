#! /usr/bin/env python3
"""Storage corruption service — schedules cj_corrupt.py via crontab."""

import argparse
import os
import sys

_VAR_RUN_FILE = "/var/run/chaosjungle"


class StorageChaosService:
    """Manage the cj_corrupt crontab service lifecycle.

    Parameters
    ----------
    target_directory : str
        Directory to corrupt files under.
    target_files : list[str]
        File patterns to target.
    frequency : str
        Schedule string: ``"Nh"`` for every N hours, ``"Nm"`` for every N minutes.
    recursive : bool
        Whether to recurse into subdirectories.
    probability : float or None
        Corruption probability (0–1).
    quiet : bool
        Suppress non-essential output.
    """

    def __init__(
        self,
        target_directory: str = "",
        target_files: list | None = None,
        frequency: str = "",
        recursive: bool = False,
        probability: float | None = None,
        quiet: bool = False,
    ) -> None:
        self.target_directory = target_directory
        self.target_files = target_files or []
        self.frequency = frequency
        self.recursive = recursive
        self.probability = probability
        self.quiet = quiet

    # ── State helpers ─────────────────────────────────────────────

    @staticmethod
    def is_running() -> bool:
        return os.path.isfile(_VAR_RUN_FILE)

    def _mark_running(self) -> None:
        with open(_VAR_RUN_FILE, "w") as f:
            f.write(
                f"uid {os.getuid()} -d {self.target_directory} "
                f"-f {self.target_files}"
            )

    @staticmethod
    def _unmark_running() -> None:
        if os.path.isfile(_VAR_RUN_FILE):
            try:
                os.remove(_VAR_RUN_FILE)
            except OSError:
                pass

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(self, cron) -> None:
        """Register a crontab job to run the corruption service."""
        if self.is_running():
            sys.exit("StorageChaosService is already running — use stop() first.")
        if not self.target_directory or not self.target_files or not self.frequency:
            sys.exit("Provide target_directory, target_files, and frequency before calling start().")

        if not self.quiet:
            print(f"RECURSIVE is {'ON' if self.recursive else 'OFF'}")

        filepath = os.path.realpath(__file__)
        extra = []
        if self.target_directory:
            extra += ["-d", f"'{self.target_directory}'"]
        if self.target_files:
            extra += ["-f", f"'{' '.join(self.target_files)}'"]
        if self.recursive:
            extra.append("-r")
        if self.probability is not None:
            extra += ["-p", str(self.probability)]

        cmd = " ".join(["python3", filepath, "--onetime"] + extra + [">/dev/null", "2>&1"])
        job = cron.new(command=cmd, comment="cj_corrupt")

        freq = self.frequency
        if "h" in freq:
            hour = int(freq[: freq.find("h")])
            if hour < 24:
                self._mark_running()
                job.every(hour).hours()
                cron.write()
                print(f"Started — every {hour} hour(s)")
                return
        elif "m" in freq:
            mins = int(freq[: freq.find("m")])
            if mins < 60:
                self._mark_running()
                job.minute.every(mins)
                cron.write()
                print(f"Started — every {mins} minute(s)")
                return
        sys.exit(f"Invalid frequency: {freq!r}. Use e.g. '2h' or '10m'.")

    def stop(self, cron) -> None:
        """Remove the crontab job."""
        print("Stopping storage chaos service")
        self._unmark_running()
        cron.remove_all(comment="cj_corrupt")
        cron.write()

    def run_once(self, args) -> None:
        """Run a single corruption pass (called by the crontab job)."""
        from cj_corrupt import run_corrupt  # noqa: PLC0415 (script context)
        run_corrupt(args)


# ── CLI entry-point ───────────────────────────────────────────────

def _parse_args():
    parser = argparse.ArgumentParser(
        description="[WARNING] Corrupts files — use with CAUTION!"
    )
    parser.add_argument("--onetime", action="store_true")
    parser.add_argument("--filelist", dest="inputfile")
    parser.add_argument("--revert", action="store_true")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("-f", dest="target_files", nargs="*")
    parser.add_argument("-d", dest="target_directory")
    parser.add_argument("-r", "--recursive", action="store_true", default=False)
    parser.add_argument("-p", dest="probability", type=float)
    parser.add_argument("-F", dest="frequency")
    parser.add_argument("-i", dest="index")
    parser.add_argument("-q", "--quiet", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    svc = StorageChaosService(
        target_directory=args.target_directory or "",
        target_files=args.target_files or [],
        frequency=args.frequency or "",
        recursive=args.recursive,
        probability=args.probability,
        quiet=args.quiet,
    )

    if args.onetime or args.wait or args.revert or args.inputfile:
        svc.run_once(args)
    elif args.stop:
        from crontab import CronTab  # noqa: PLC0415
        svc.stop(CronTab(user=True))
    elif args.start:
        from crontab import CronTab  # noqa: PLC0415
        svc.start(CronTab(user=True))
    else:
        sys.exit("Specify an action: --onetime / --start / --stop / --wait / --revert")


if __name__ == "__main__":
    main()
