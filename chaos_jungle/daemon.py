"""Chaos daemon — FastAPI service that exposes faults over HTTP.

Run on a remote machine so a local controller can inject faults via HTTPTarget:

    cj-daemon --port 7777 --token mysecret

Security notes
--------------
* Authentication is **mandatory** unless ``--dev`` mode is explicitly enabled.
* Token comparison uses constant-time ``hmac.compare_digest`` to prevent
  timing-based token leakage.
* ``/exec`` uses ``shlex.split`` (no ``shell=True``) and enforces a command
  allowlist unless ``--dev`` mode is on.
* Upload paths are validated against a configured allowlist.
* Request bodies are capped at 1 MB.
* The daemon binds to ``127.0.0.1`` by default — pass ``--host 0.0.0.0``
  only when behind a TLS reverse proxy.
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
_DEV_MODE: bool = False          # set by run() — allows unauthenticated access
_MAX_BODY_BYTES: int = 1_048_576  # 1 MiB

# Commands allowed in non-dev mode.  Extend as needed via run().
_EXEC_ALLOWLIST: set[str] = {
    "tc", "ip", "iptables", "ip6tables",
    "pkill", "kill", "systemctl", "docker",
    "dd", "df", "du", "ls", "cat", "echo",
    "stress-ng", "stress",
    "redis-cli", "psql",
    "python3", "python",
    "crontab",
}

# Paths allowed for file upload/download in non-dev mode.
_FILE_ALLOWLIST: list[str] = [
    "/tmp",
    os.path.expanduser("~/chaos-jungle"),
]

# Emergency-stop event — set by POST /emergency-stop
_emergency_stop = threading.Event()

# ── App ───────────────────────────────────────────────────────────

app = FastAPI(title="chaos-jungle daemon", version="1.0.0")


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
    if not hmac.compare_digest(provided, _TOKEN):
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
        detail=f"Path {path!r} is outside the allowed directories.",
    )


def _check_cmd_allowed(argv: list[str]) -> None:
    if _DEV_MODE or not argv:
        return
    binary = os.path.basename(argv[0])
    if binary not in _EXEC_ALLOWLIST:
        raise HTTPException(
            status_code=403,
            detail=f"Command {binary!r} is not in the exec allowlist. "
                   f"Use --dev mode to allow arbitrary commands.",
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
    return {
        "status": "ok",
        "dev_mode": _DEV_MODE,
        "emergency_stop": _emergency_stop.is_set(),
        "version": "1.0.0",
    }


# ── Emergency stop ────────────────────────────────────────────────

class EmergencyStopRequest(BaseModel):
    reason: str = "remote emergency stop"


@app.post("/emergency-stop")
def emergency_stop(
    body: EmergencyStopRequest,
    authorization: Annotated[str | None, Header()] = None,
):
    """Signal all running experiments to abort immediately."""
    _check_auth(authorization)
    _emergency_stop.set()
    return {"status": "emergency_stop_set", "reason": body.reason}


@app.delete("/emergency-stop")
def clear_emergency_stop(
    authorization: Annotated[str | None, Header()] = None,
):
    """Clear a previously set emergency stop."""
    _check_auth(authorization)
    _emergency_stop.clear()
    return {"status": "cleared"}


# ── Exec ──────────────────────────────────────────────────────────

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

    The command is split via ``shlex.split`` (no ``shell=True``).
    Only commands in the exec allowlist are permitted outside dev mode.
    """
    _check_auth(authorization)
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


# ── File transfer ────────────────────────────────────────────────

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
# Thin pass-through to the local SessionDB + ChaosRunner so the HTTP
# target can manage scenarios without arbitrary shell execution.

def _get_db():
    from chaos_jungle.db.session_db import SessionDB
    return SessionDB()


class ScenarioCreateRequest(BaseModel):
    name: str
    faults: list[dict]


class ScenarioRunRequest(BaseModel):
    target_type: str = "local"
    duration: str | None = None


@app.post("/scenarios", status_code=201)
def create_scenario(
    body: ScenarioCreateRequest,
    authorization: Annotated[str | None, Header()] = None,
):
    """Register a named scenario in the local database."""
    _check_auth(authorization)
    db = _get_db()
    sid = db.open_session(body.name)
    db.update_session_status(sid, "created")
    return {"scenario_id": sid, "name": body.name}


@app.get("/scenarios")
def list_scenarios(
    authorization: Annotated[str | None, Header()] = None,
):
    """List all sessions/scenarios recorded in the local database."""
    _check_auth(authorization)
    db = _get_db()
    rows = db.list_sessions()
    return {"scenarios": [dict(r) for r in rows]}


@app.get("/scenarios/{scenario_id}")
def get_scenario(
    scenario_id: int,
    authorization: Annotated[str | None, Header()] = None,
):
    """Get a single scenario by id."""
    _check_auth(authorization)
    db = _get_db()
    row = db.get_session(scenario_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return dict(row)


@app.get("/scenarios/{scenario_id}/status")
def scenario_status(
    scenario_id: int,
    authorization: Annotated[str | None, Header()] = None,
):
    """Return just the status of a scenario."""
    _check_auth(authorization)
    db = _get_db()
    row = db.get_session(scenario_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return {"scenario_id": scenario_id, "status": row["status"]}


@app.post("/scenarios/{scenario_id}/stop")
def stop_scenario(
    scenario_id: int,
    authorization: Annotated[str | None, Header()] = None,
):
    """Mark a scenario as stopped in the database."""
    _check_auth(authorization)
    db = _get_db()
    row = db.get_session(scenario_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    db.close_session(scenario_id, status="reverted")
    return {"scenario_id": scenario_id, "status": "reverted"}


# ── Entry point ───────────────────────────────────────────────────

def run(
    host: str = "127.0.0.1",
    port: int = 7777,
    token: str = "",
    dev: bool = False,
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
    reload : bool
        Enable auto-reload (development only).
    """
    global _TOKEN, _DEV_MODE

    if dev:
        _DEV_MODE = True
        print("[chaos-jungle daemon] WARNING: dev mode enabled — auth and allowlists disabled")
    elif not token and not os.environ.get("CJ_DAEMON_TOKEN"):
        raise RuntimeError(
            "Daemon requires a token. Pass --token or set CJ_DAEMON_TOKEN. "
            "Use --dev only for local development."
        )

    if token:
        _TOKEN = token
        os.environ["CJ_DAEMON_TOKEN"] = token

    uvicorn.run(
        "chaos_jungle.daemon:app",
        host=host,
        port=port,
        reload=reload,
    )
