"""Chaos daemon — FastAPI service that exposes faults over HTTP.

Run on Machine B so Machine A can control it via HTTPTarget:

    cj-daemon --port 7777 --token mysecret
"""

from __future__ import annotations
import json
import os
import subprocess
import shutil
from typing import Annotated

import uvicorn
from fastapi import FastAPI, Header, HTTPException, UploadFile, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="chaos-jungle daemon", version="0.1.0")

# Optional bearer token auth — set via CJ_DAEMON_TOKEN env var
_TOKEN = os.environ.get("CJ_DAEMON_TOKEN", "")


def _check_auth(authorization: str | None) -> None:
    if not _TOKEN:
        return  # no auth configured
    if authorization != f"Bearer {_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Health ────────────────────────────────────────────────────────

@app.get("/health")
def health():
    """Daemon health check."""
    return {"status": "ok"}


# ── Exec ──────────────────────────────────────────────────────────

class ExecRequest(BaseModel):
    cmd: str


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

    The daemon runs as root so all privileged commands (tc, dd) work
    without sudo.
    """
    _check_auth(authorization)
    result = subprocess.run(
        body.cmd,
        shell=True,
        capture_output=True,
        text=True,
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
    """Upload a file to the daemon machine."""
    _check_auth(authorization)
    dest = os.path.expanduser(dest)
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"status": "ok", "dest": dest}


@app.get("/files/download")
def download_file(
    path: str,
    authorization: Annotated[str | None, Header()] = None,
):
    """Download a file from the daemon machine."""
    _check_auth(authorization)
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    return FileResponse(path)


# ── Entry point ───────────────────────────────────────────────────

def run(
    host: str = "0.0.0.0",
    port: int = 7777,
    token: str = "",
    reload: bool = False,
) -> None:
    """Start the chaos daemon.

    Parameters
    ----------
    host : str
        Interface to bind to. Default ``0.0.0.0``.
    port : int
        TCP port. Default ``7777``.
    token : str
        Bearer token for auth. Empty = no auth.
    reload : bool
        Enable auto-reload (development only).
    """
    if token:
        os.environ["CJ_DAEMON_TOKEN"] = token
    uvicorn.run(
        "chaos_jungle.daemon:app",
        host=host,
        port=port,
        reload=reload,
    )
