"""ScenarioRegistry — tracks scenario lifecycle across local and remote machines."""

from __future__ import annotations
import json
import time
from typing import TYPE_CHECKING

from chaos_jungle.db.session_db import SessionDB

if TYPE_CHECKING:
    from chaos_jungle.scenario import Scenario
    from chaos_jungle.targets.base import Target


class ScenarioRegistry:
    """Manages scenario registration and status tracking in the local DB.

    Each machine (local, SSH remote, HTTP daemon) maintains its own registry.
    The scenario UUID is the shared key that links entries across machines.

    Parameters
    ----------
    db : SessionDB, optional
        Database instance. Defaults to the standard ``~/.chaos-jungle/chaos_jungle.db``.

    Examples
    --------
    >>> from chaos_jungle import Scenario, NetworkDelay
    >>> from chaos_jungle.registry import ScenarioRegistry
    >>>
    >>> registry = ScenarioRegistry()
    >>> scenario = Scenario("wan-test", [NetworkDelay("100ms")])
    >>> registry.register(scenario)          # auto-registered as type=local
    >>> registry.set_running(scenario.id)
    >>> registry.set_done(scenario.id, session_id=42)
    """

    def __init__(self, db: SessionDB | None = None) -> None:
        self.db = db or SessionDB()

    def register(
        self,
        scenario: "Scenario",
        type: str = "local",
        target_ip: str = "",
        source_ip: str = "",
    ) -> str:
        """Register a scenario in the local registry. Returns its UUID."""
        self.db.register_scenario(
            scenario_id=scenario.id,
            name=scenario.name,
            faults_json=json.dumps(
                [{"kind": f.__class__.__name__, "params": f._parameters()}
                 for f in scenario.faults]
            ),
            type=type,
            target_ip=target_ip,
            source_ip=source_ip,
        )
        return scenario.id

    def get(self, scenario_id: str) -> dict | None:
        """Fetch a scenario entry by UUID."""
        return self.db.get_scenario(scenario_id)

    def set_running(self, scenario_id: str) -> None:
        self.db.update_scenario_status(scenario_id, "running")

    def set_done(self, scenario_id: str, session_id: int | None = None) -> None:
        self.db.update_scenario_status(scenario_id, "done", session_id=session_id)

    def set_failed(self, scenario_id: str, session_id: int | None = None) -> None:
        self.db.update_scenario_status(scenario_id, "failed", session_id=session_id)

    def status(self, scenario_id: str) -> str | None:
        """Return current status string, or None if not found."""
        entry = self.db.get_scenario(scenario_id)
        return entry["status"] if entry else None

    def list(
        self,
        status: str | None = None,
        type: str | None = None,
    ) -> list[dict]:
        """List scenarios, optionally filtered by status or type."""
        return self.db.list_scenarios(status=status, type=type)

    def watch(
        self,
        scenario_id: str,
        target: "Target | None" = None,
        poll_interval: float = 5.0,
        timeout: float = 600.0,
    ) -> dict:
        """Block until scenario reaches done/failed, polling every ``poll_interval`` s.

        For local scenarios, reads the local DB directly.
        For remote scenarios, calls ``target.scenario_status(scenario_id)``
        which issues a brief SSH/HTTP request.

        Parameters
        ----------
        scenario_id : str
            UUID of the scenario to watch.
        target : Target, optional
            Remote target to poll. ``None`` means local DB only.
        poll_interval : float
            Seconds between polls. Default 5.
        timeout : float
            Maximum seconds to wait before raising ``TimeoutError``. Default 600.

        Returns
        -------
        dict
            Final scenario registry entry with ``status`` and ``session_id``.

        Raises
        ------
        TimeoutError
            If the scenario does not finish within ``timeout`` seconds.
        ValueError
            If the scenario is not found in the registry.
        """
        deadline = time.monotonic() + timeout
        while True:
            if target is not None:
                entry = target.scenario_status(scenario_id)
            else:
                entry = self.db.get_scenario(scenario_id)

            if entry is None:
                raise ValueError(
                    f"Scenario {scenario_id!r} not found in registry. "
                    "Make sure it was registered before watching."
                )

            if entry["status"] in ("done", "failed"):
                # Sync final status back to local DB if this was a remote poll
                if target is not None:
                    if entry["status"] == "done":
                        self.set_done(scenario_id, session_id=entry.get("session_id"))
                    else:
                        self.set_failed(scenario_id)
                return entry

            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"Timed out waiting for scenario {scenario_id!r} "
                    f"after {timeout}s (current status: {entry['status']!r})"
                )

            time.sleep(poll_interval)

    def watch_all(
        self,
        scenario_ids: list[str],
        targets: "dict[str, Target] | None" = None,
        poll_interval: float = 5.0,
        timeout: float = 600.0,
    ) -> dict[str, dict]:
        """Watch multiple scenarios, return when all are done/failed.

        Parameters
        ----------
        scenario_ids : list[str]
            UUIDs to watch.
        targets : dict[str, Target], optional
            Mapping of scenario_id → Target for remote scenarios.
            Local scenarios (not in this dict) are polled from local DB.
        poll_interval : float
        timeout : float

        Returns
        -------
        dict[str, dict]
            Mapping of scenario_id → final registry entry.
        """
        targets = targets or {}
        results: dict[str, dict] = {}
        pending = set(scenario_ids)
        deadline = time.monotonic() + timeout

        while pending:
            for sid in list(pending):
                tgt = targets.get(sid)
                if tgt is not None:
                    entry = tgt.scenario_status(sid)
                else:
                    entry = self.db.get_scenario(sid)

                if entry is None:
                    raise ValueError(f"Scenario {sid!r} not found in registry.")

                if entry["status"] in ("done", "failed"):
                    # Sync final status back to local DB if this was a remote poll
                    if tgt is not None:
                        if entry["status"] == "done":
                            self.set_done(sid, session_id=entry.get("session_id"))
                        else:
                            self.set_failed(sid)
                    results[sid] = entry
                    pending.discard(sid)

            if pending and time.monotonic() > deadline:
                raise TimeoutError(
                    f"Timed out after {timeout}s. "
                    f"Still pending: {pending}"
                )

            if pending:
                time.sleep(poll_interval)

        return results
