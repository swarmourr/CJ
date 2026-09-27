"""Chaos daemon — FastAPI service that exposes faults over HTTP.

Run on a remote machine so a local controller can inject faults via HTTPTarget:

    cj-daemon --port 7777 --token mysecret

Security model
--------------
* Authentication is **mandatory** unless ``--dev`` mode is explicitly enabled.
* Token comparison uses constant-time ``hmac.compare_digest``.
* ``/exec`` is **disabled by default** and must be explicitly enabled with
  ``--enable-exec``. Even when enabled, ``python``, ``python3``, ``dd``,
  ``crontab``, and ``cat`` are not in the allowlist — they provide equivalent
  arbitrary execution to shell access.
* Scenarios are managed as typed, validated operations via the scenario
  registry rather than arbitrary shell commands.
* Upload/download paths are validated via canonical ``realpath()`` comparison.
* Request bodies are capped at 1 MB.
* The daemon binds to ``127.0.0.1`` by default — use ``--host 0.0.0.0``
  only behind a TLS reverse proxy.
* A background watchdog reverts experiments whose lease has expired, protecting
  against controller disconnection.
"""

from __future__ import annotations

import hmac
import os
import shlex
import shutil
import subprocess
import threading
import time
from typing import Annotated

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, UploadFile, Form
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# ── Configuration ─────────────────────────────────────────────────

_TOKEN: str = os.environ.get("CJ_DAEMON_TOKEN", "")
_DEV_MODE: bool = False
_EXEC_ENABLED: bool = False       # /exec is OFF by default — must be opt-in
_MAX_BODY_BYTES: int = 1_048_576  # 1 MiB

# Commands allowed in non-dev mode when /exec is enabled.
# Intentionally excludes python, python3, dd, cat, crontab — these are
# equivalent to arbitrary code/data execution and must not be in the allowlist.
_EXEC_ALLOWLIST: set[str] = {
    "tc", "ip", "iptables", "ip6tables",
    "pkill", "kill", "systemctl", "docker",
    "df", "du", "ls", "echo",
    "stress-ng", "stress",
    "redis-cli", "psql",
}

# Paths allowed for file upload/download in non-dev mode.
_FILE_ALLOWLIST: list[str] = [
    "/tmp",
    os.path.expanduser("~/chaos-jungle"),
]

# ── In-process scenario state ──────────────────────────────────────
# Scenarios are string-keyed by UUID (Scenario.id), not integer DB session IDs.

_SCENARIO_STORE: dict[str, dict] = {}           # scenario_id → scenario.to_dict()
_RUNNER_REGISTRY: dict[str, object] = {}        # scenario_id → ChaosRunner
_RUNNER_STATUS: dict[str, str] = {}             # scenario_id → status string
_REGISTRY_LOCK = threading.Lock()

# ── Lease management ──────────────────────────────────────────────
# Each scenario run has an optional lease (expiry timestamp). A background
# watchdog reverts runs whose lease expires without a heartbeat renewal.

_LEASES: dict[str, float] = {}  # scenario_id → expiry unix timestamp
_LEASE_LOCK = threading.Lock()
_LEASE_DEFAULT_S: float = 120.0

# ── Emergency stop ────────────────────────────────────────────────
_emergency_stop = threading.Event()

# ── App ───────────────────────────────────────────────────────────

app = FastAPI(title="chaos-jungle daemon", version="2.0.0")


# ── Auth + safety middleware ──────────────────────────────────────

def _check_auth(authorization: str | None) -> None:
    if _DEV_MODE:
        return
    if not _TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Daemon started without a token. "
                   "Pass --token or set CJ_DAEMON_TOKEN, or use --dev for development mode.",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization header required")
    provided = authorization[len("Bearer "):]
    if not hmac.compare_digest(provided.encode(), _TOKEN.encode()):
        raise HTTPException(status_code=401, detail="Invalid token")


def _check_path_allowed(path: str) -> str:
    """Resolve and validate *path* against the file allowlist. Return resolved path."""
    if _DEV_MODE:
        return os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    resolved = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    for allowed in _FILE_ALLOWLIST:
        real_allowed = os.path.realpath(os.path.abspath(allowed))
        try:
            if os.path.commonpath([resolved, real_allowed]) == real_allowed:
                return resolved
        except ValueError:
            continue
    raise HTTPException(
        status_code=403,
        detail="Path is outside the allowed directories.",
    )


def _check_cmd_allowed(argv: list[str]) -> None:
    if _DEV_MODE or not argv:
        return
    binary = os.path.basename(argv[0])
    if binary not in _EXEC_ALLOWLIST:
        raise HTTPException(
            status_code=403,
            detail=f"Command {binary!r} is not in the exec allowlist.",
        )


@app.middleware("http")
async def _limit_body_size(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > _MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={"detail": "Request body too large"})
    return await call_next(request)


# ── Health ────────────────────────────────────────────────────────

@app.get("/health")
def health():
    """Daemon health check — no auth required."""
    with _REGISTRY_LOCK:
        running = list(_RUNNER_REGISTRY.keys())
    return {
        "status": "ok",
        "dev_mode": _DEV_MODE,
        "exec_enabled": _EXEC_ENABLED,
        "emergency_stop": _emergency_stop.is_set(),
        "running_scenarios": running,
        "version": "2.0.0",
    }


# ── Emergency stop ────────────────────────────────────────────────

class EmergencyStopRequest(BaseModel):
    reason: str = "remote emergency stop"


@app.post("/emergency-stop")
def emergency_stop(
    body: EmergencyStopRequest,
    authorization: Annotated[str | None, Header()] = None,
):
    """Stop and revert all active experiments immediately."""
    _check_auth(authorization)
    _emergency_stop.set()

    with _REGISTRY_LOCK:
        runners = list(_RUNNER_REGISTRY.items())
        _RUNNER_REGISTRY.clear()

    with _LEASE_LOCK:
        _LEASES.clear()

    stopped: list[str] = []
    errors: list[dict] = []

    for sid, runner in runners:
        try:
            runner.stop(_status_override="aborted")
            stopped.append(sid)
            _RUNNER_STATUS[sid] = "aborted"
        except Exception as exc:
            errors.append({"scenario_id": sid, "error": str(exc)})
            _RUNNER_STATUS[sid] = "revert_failed"

    return {
        "status": "emergency_stop_executed",
        "reason": body.reason,
        "stopped": len(stopped),
        "errors": errors,
    }


@app.delete("/emergency-stop")
def clear_emergency_stop(
    authorization: Annotated[str | None, Header()] = None,
):
    """Clear a previously set emergency stop."""
    _check_auth(authorization)
    _emergency_stop.clear()
    return {"status": "cleared"}


# ── Exec ──────────────────────────────────────────────────────────
# Disabled by default. Enabled only in dev mode or with --enable-exec.
# Even when enabled the allowlist intentionally excludes python/dd/cat/crontab.

class ExecRequest(BaseModel):
    cmd: str
    timeout_s: float = 30.0


class ExecResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str


@app.post("/exec", response_model=ExecResponse)
def exec_cmd(
    body: ExecRequest,
    authorization: Annotated[str | None, Header()] = None,
):
    """Run a shell command on this machine and return the result.

    Disabled by default. Requires ``--enable-exec`` or ``--dev``.
    """
    _check_auth(authorization)

    if not _DEV_MODE and not _EXEC_ENABLED:
        raise HTTPException(
            status_code=403,
            detail=(
                "The /exec endpoint is disabled. Use scenario endpoints "
                "(POST /scenarios, POST /scenarios/{id}/run) instead. "
                "Pass --enable-exec to opt in to raw command execution."
            ),
        )

    if _emergency_stop.is_set():
        raise HTTPException(status_code=503, detail="Emergency stop is active")

    try:
        argv = shlex.split(body.cmd)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid command syntax: {exc}")

    _check_cmd_allowed(argv)

    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=min(body.timeout_s, 120.0),
    )
    return ExecResponse(
        exit_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


# ── File transfer ─────────────────────────────────────────────────

@app.post("/files/upload")
def upload_file(
    file: UploadFile,
    dest: Annotated[str, Form()],
    authorization: Annotated[str | None, Header()] = None,
):
    """Upload a file to the daemon machine (allowlisted paths only)."""
    _check_auth(authorization)
    resolved = _check_path_allowed(dest)
    os.makedirs(os.path.dirname(resolved), exist_ok=True)
    with open(resolved, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"status": "ok", "dest": resolved}


@app.get("/files/download")
def download_file(
    path: str,
    authorization: Annotated[str | None, Header()] = None,
):
    """Download a file from the daemon machine (allowlisted paths only)."""
    _check_auth(authorization)
    resolved = _check_path_allowed(path)
    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(resolved)


# ── Scenarios ─────────────────────────────────────────────────────
# Scenarios are keyed by their UUID (scenario.id), not by integer DB session IDs.
# Fault definitions are parsed and stored in _SCENARIO_STORE.
# Running instances are tracked in _RUNNER_REGISTRY.

class ScenarioCreateRequest(BaseModel):
    id: str            # UUID from Scenario.id
    name: str
    faults: list[dict]


class ScenarioRunRequest(BaseModel):
    duration: str | None = None
    lease_s: float = _LEASE_DEFAULT_S


class HeartbeatRequest(BaseModel):
    lease_s: float = _LEASE_DEFAULT_S


@app.post("/scenarios", status_code=201)
def create_scenario(
    body: ScenarioCreateRequest,
    authorization: Annotated[str | None, Header()] = None,
):
    """Register a scenario definition (with its faults) in the daemon store."""
    _check_auth(authorization)
    with _REGISTRY_LOCK:
        _SCENARIO_STORE[body.id] = body.model_dump()
        _RUNNER_STATUS[body.id] = "created"
    return {"scenario_id": body.id, "name": body.name}


@app.get("/scenarios")
def list_scenarios(
    authorization: Annotated[str | None, Header()] = None,
):
    """List all registered scenario definitions and their current status."""
    _check_auth(authorization)
    with _REGISTRY_LOCK:
        result = [
            {
                "id": sid,
                "name": data["name"],
                "fault_count": len(data.get("faults", [])),
                "status": _RUNNER_STATUS.get(sid, "created"),
            }
            for sid, data in _SCENARIO_STORE.items()
        ]
    return {"scenarios": result}


@app.get("/scenarios/{scenario_id}")
def get_scenario(
    scenario_id: str,
    authorization: Annotated[str | None, Header()] = None,
):
    """Get the definition and status of a single scenario."""
    _check_auth(authorization)
    with _REGISTRY_LOCK:
        stored = _SCENARIO_STORE.get(scenario_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return {
        **stored,
        "status": _RUNNER_STATUS.get(scenario_id, "created"),
    }


@app.get("/scenarios/{scenario_id}/status")
def scenario_status(
    scenario_id: str,
    authorization: Annotated[str | None, Header()] = None,
):
    """Return just the lifecycle status of a scenario."""
    _check_auth(authorization)
    with _REGISTRY_LOCK:
        if scenario_id not in _SCENARIO_STORE:
            raise HTTPException(status_code=404, detail="Scenario not found")
        status = _RUNNER_STATUS.get(scenario_id, "created")
    return {"scenario_id": scenario_id, "status": status}


@app.post("/scenarios/{scenario_id}/run", status_code=202)
def run_scenario(
    scenario_id: str,
    body: ScenarioRunRequest = ScenarioRunRequest(),
    authorization: Annotated[str | None, Header()] = None,
):
    """Start a previously registered scenario, returning immediately.

    The scenario runs in a background thread. Use GET /scenarios/{id}/status
    to poll for completion. Supply ``duration`` for automatic stop.
    """
    _check_auth(authorization)

    if _emergency_stop.is_set():
        raise HTTPException(status_code=503, detail="Emergency stop is active")

    with _REGISTRY_LOCK:
        stored = _SCENARIO_STORE.get(scenario_id)
        if stored is None:
            raise HTTPException(status_code=404, detail="Scenario not found — call POST /scenarios first")
        if scenario_id in _RUNNER_REGISTRY:
            raise HTTPException(status_code=409, detail="Scenario is already running")

    # Reconstruct from stored definition
    try:
        from chaos_jungle.core.scenario import Scenario
        scenario = Scenario.from_dict(stored)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Cannot reconstruct scenario: {exc}")

    # Set initial lease
    with _LEASE_LOCK:
        _LEASES[scenario_id] = time.time() + body.lease_s

    def _run() -> None:
        from chaos_jungle.targets.local import LocalTarget
        from chaos_jungle.core.runner import ChaosRunner

        runner = ChaosRunner(scenario, target=LocalTarget(), auto_preflight=False)

        with _REGISTRY_LOCK:
            _RUNNER_REGISTRY[scenario_id] = runner
            _RUNNER_STATUS[scenario_id] = "starting"

        try:
            if body.duration:
                _RUNNER_STATUS[scenario_id] = "active"
                runner.run(body.duration)
                _RUNNER_STATUS[scenario_id] = "reverted"
                # duration run is complete — clean up registry
                with _REGISTRY_LOCK:
                    _RUNNER_REGISTRY.pop(scenario_id, None)
                with _LEASE_LOCK:
                    _LEASES.pop(scenario_id, None)
            else:
                runner.start()
                _RUNNER_STATUS[scenario_id] = "active"
                # stays active until /stop is called or lease expires
        except Exception:
            _RUNNER_STATUS[scenario_id] = "error"
            with _REGISTRY_LOCK:
                _RUNNER_REGISTRY.pop(scenario_id, None)
            with _LEASE_LOCK:
                _LEASES.pop(scenario_id, None)

    t = threading.Thread(target=_run, daemon=True, name=f"cj-runner-{scenario_id[:8]}")
    t.start()

    return {"scenario_id": scenario_id, "status": "starting"}


@app.post("/scenarios/{scenario_id}/stop")
def stop_scenario(
    scenario_id: str,
    authorization: Annotated[str | None, Header()] = None,
):
    """Stop and revert a running scenario. Invokes the actual ChaosRunner.stop()."""
    _check_auth(authorization)

    with _REGISTRY_LOCK:
        runner = _RUNNER_REGISTRY.get(scenario_id)

    if runner is None:
        raise HTTPException(
            status_code=404,
            detail=f"No running scenario '{scenario_id}'. "
                   "It may have already stopped or was never started.",
        )

    try:
        runner.stop()
        with _REGISTRY_LOCK:
            _RUNNER_REGISTRY.pop(scenario_id, None)
        with _LEASE_LOCK:
            _LEASES.pop(scenario_id, None)
        _RUNNER_STATUS[scenario_id] = "reverted"
        return {"scenario_id": scenario_id, "status": "reverted"}
    except Exception as exc:
        _RUNNER_STATUS[scenario_id] = "revert_failed"
        raise HTTPException(
            status_code=500,
            detail=f"Scenario stop encountered errors: {exc}",
        )


# ── Leases / heartbeat ────────────────────────────────────────────

@app.post("/scenarios/{scenario_id}/heartbeat")
def heartbeat(
    scenario_id: str,
    body: HeartbeatRequest = HeartbeatRequest(),
    authorization: Annotated[str | None, Header()] = None,
):
    """Renew the lease for a running scenario.

    The controller should call this periodically to prevent the watchdog
    from automatically reverting the experiment on lease expiry.
    """
    _check_auth(authorization)

    with _LEASE_LOCK:
        if scenario_id not in _LEASES:
            raise HTTPException(
                status_code=404,
                detail="No active lease for this scenario. Is it running?",
            )
        _LEASES[scenario_id] = time.time() + body.lease_s
        expires_at = _LEASES[scenario_id]

    return {
        "scenario_id": scenario_id,
        "expires_at": expires_at,
        "lease_s": body.lease_s,
    }


# ── Watchdog ──────────────────────────────────────────────────────

def _start_watchdog(interval_s: float = 10.0) -> None:
    """Start the background watchdog thread that reverts experiments on lease expiry."""

    def _watchdog() -> None:
        while True:
            time.sleep(interval_s)
            now = time.time()
            expired = []
            with _LEASE_LOCK:
                for sid, exp in list(_LEASES.items()):
                    if now > exp:
                        expired.append(sid)
                        del _LEASES[sid]

            for sid in expired:
                with _REGISTRY_LOCK:
                    runner = _RUNNER_REGISTRY.pop(sid, None)

                if runner is not None:
                    print(
                        f"[chaos-jungle daemon] Watchdog: lease expired for {sid!r} — reverting"
                    )
                    try:
                        runner.stop()
                        _RUNNER_STATUS[sid] = "reverted"
                        print(f"[chaos-jungle daemon] Watchdog: {sid!r} reverted cleanly")
                    except Exception as exc:
                        _RUNNER_STATUS[sid] = "revert_failed"
                        print(
                            f"[chaos-jungle daemon] Watchdog ERROR reverting {sid!r}: {exc}"
                        )

    t = threading.Thread(target=_watchdog, daemon=True, name="cj-watchdog")
    t.start()


# ── Test helpers ──────────────────────────────────────────────────

def _reset_state() -> None:
    """Reset all global state. For use in tests only."""
    global _TOKEN, _DEV_MODE, _EXEC_ENABLED
    _TOKEN = ""
    _DEV_MODE = False
    _EXEC_ENABLED = False
    with _REGISTRY_LOCK:
        _SCENARIO_STORE.clear()
        _RUNNER_REGISTRY.clear()
        _RUNNER_STATUS.clear()
    with _LEASE_LOCK:
        _LEASES.clear()
    _emergency_stop.clear()


# ── Entry point ───────────────────────────────────────────────────

def run(
    host: str = "127.0.0.1",
    port: int = 7777,
    token: str = "",
    dev: bool = False,
    enable_exec: bool = False,
    reload: bool = False,
) -> None:
    """Start the chaos daemon.

    Parameters
    ----------
    host : str
        Interface to bind to. Default ``127.0.0.1`` (localhost only).
        Pass ``0.0.0.0`` only when behind a TLS reverse proxy.
    port : int
        TCP port. Default ``7777``.
    token : str
        Bearer token for authentication. Mandatory unless *dev* is ``True``.
    dev : bool
        Development mode — disables auth, command allowlist, and path
        restrictions. **Never use in production.**
    enable_exec : bool
        Enable the ``/exec`` endpoint. Off by default even in production mode.
        Dev mode enables it implicitly.
    reload : bool
        Enable auto-reload (development only).
    """
    global _TOKEN, _DEV_MODE, _EXEC_ENABLED

    if dev:
        _DEV_MODE = True
        _EXEC_ENABLED = True
        print("[chaos-jungle daemon] WARNING: dev mode enabled — auth and allowlists disabled")
    elif not token and not os.environ.get("CJ_DAEMON_TOKEN"):
        raise RuntimeError(
            "Daemon requires a token. Pass --token or set CJ_DAEMON_TOKEN. "
            "Use --dev only for local development."
        )

    if token:
        _TOKEN = token
        os.environ["CJ_DAEMON_TOKEN"] = token
    elif os.environ.get("CJ_DAEMON_TOKEN"):
        _TOKEN = os.environ["CJ_DAEMON_TOKEN"]

    if enable_exec:
        _EXEC_ENABLED = True
        print("[chaos-jungle daemon] WARNING: /exec endpoint enabled — use with caution")

    _start_watchdog()

    uvicorn.run(
        "chaos_jungle.control.daemon:app",
        host=host,
        port=port,
        reload=reload,
    )
