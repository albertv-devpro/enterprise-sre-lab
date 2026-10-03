"""Local self-service API for the Enterprise SRE Lab."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    status,
)
from pydantic import BaseModel, Field


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK = PROJECT_ROOT / "playbooks" / "site.yml"
TARGET_TAGS = {
    "baseline": "baseline",
    "node_exporter": "node_exporter",
    "webserver": "webserver",
    "observability": "observability",
}
MAX_OUTPUT_LENGTH = 4000
PLAYBOOK_TIMEOUT_SECONDS = 1800
DEFAULT_DB_PATH = (
    Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    / "enterprise-sre-automation"
    / "audit.sqlite3"
)
DB_PATH = Path(os.environ.get("AUTOMATION_DB_PATH", DEFAULT_DB_PATH))
PLAYBOOK_LOCK = threading.Lock()


class DeploymentRequest(BaseModel):
    target: Literal["baseline", "node_exporter", "webserver", "observability", "all"]


class AlertmanagerWebhook(BaseModel):
    receiver: str
    status: Literal["firing", "resolved"]
    alerts: list[dict[str, Any]] = Field(max_length=500)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_db() -> None:
    with connect_db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                target TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                output TEXT,
                error TEXT
            )
            """
        )
    DB_PATH.chmod(0o600)


def create_event(
    *,
    event_id: str,
    kind: str,
    target: str | None,
    event_status: str,
    payload: dict[str, Any],
) -> None:
    timestamp = utc_now()
    with connect_db() as connection:
        connection.execute(
            """
            INSERT INTO audit_events
                (id, kind, target, status, created_at, updated_at, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                kind,
                target,
                event_status,
                timestamp,
                timestamp,
                json.dumps(payload),
            ),
        )


def update_event(
    event_id: str,
    event_status: str,
    *,
    output: str | None = None,
    error: str | None = None,
) -> None:
    with connect_db() as connection:
        connection.execute(
            """
            UPDATE audit_events
            SET status = ?, updated_at = ?, output = ?, error = ?
            WHERE id = ?
            """,
            (event_status, utc_now(), output, error, event_id),
        )


def serialize_event(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "ticket_id": row["id"],
        "kind": row["kind"],
        "target": row["target"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "details": json.loads(row["payload"]),
        "output": row["output"],
        "error": row["error"],
    }


def get_event(event_id: str) -> dict[str, Any] | None:
    with connect_db() as connection:
        row = connection.execute(
            "SELECT * FROM audit_events WHERE id = ?", (event_id,)
        ).fetchone()
    return serialize_event(row) if row else None


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_db()
    yield


app = FastAPI(
    title="Enterprise SRE Automation API",
    version="1.0.0",
    description="Authenticated local API for approved lab playbook runs and alert events.",
    lifespan=lifespan,
)


def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> None:
    configured_key = os.environ.get("AUTOMATION_API_TOKEN")
    if not configured_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The API is not configured with an authentication token.",
        )
    bearer_token = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer_token = authorization[7:].strip()
    provided_key = x_api_key or bearer_token
    if provided_key is None or not secrets.compare_digest(configured_key, provided_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )


@app.get("/healthz")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/deployments", status_code=status.HTTP_202_ACCEPTED)
def queue_deployment(
    request: DeploymentRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(require_api_key),
) -> dict[str, Any]:
    ticket_id = f"CHG-{uuid.uuid4().hex}"
    create_event(
        event_id=ticket_id,
        kind="deployment",
        target=request.target,
        event_status="QUEUED",
        payload={"target": request.target},
    )
    background_tasks.add_task(run_playbook, request.target, ticket_id)
    return {
        "message": "Deployment queued.",
        "ticket": get_event(ticket_id),
    }


@app.post("/api/v1/webhooks/alertmanager", status_code=status.HTTP_202_ACCEPTED)
def receive_alert(
    alert: AlertmanagerWebhook,
    _: None = Depends(require_api_key),
) -> dict[str, str]:
    incident_id = f"INC-{uuid.uuid4().hex}"
    create_event(
        event_id=incident_id,
        kind="alert",
        target=None,
        event_status="RECEIVED",
        payload=alert.model_dump(mode="json"),
    )
    return {"status": "received", "incident_id": incident_id}


@app.get("/api/v1/tickets")
def list_tickets(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: None = Depends(require_api_key),
) -> dict[str, list[dict[str, Any]]]:
    with connect_db() as connection:
        rows = connection.execute(
            """
            SELECT * FROM audit_events
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return {"audit_trail": [serialize_event(row) for row in rows]}


@app.get("/api/v1/tickets/{ticket_id}")
def ticket_status(
    ticket_id: str,
    _: None = Depends(require_api_key),
) -> dict[str, Any]:
    event = get_event(ticket_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    return event


def run_playbook(target: str, ticket_id: str) -> None:
    with PLAYBOOK_LOCK:
        _run_playbook(target, ticket_id)


def _run_playbook(target: str, ticket_id: str) -> None:
    update_event(ticket_id, "RUNNING")
    executable = shutil.which("ansible-playbook")
    if executable is None:
        update_event(
            ticket_id,
            "FAILED",
            error="ansible-playbook was not found on the API server PATH.",
        )
        return

    command = [
        executable,
        "--inventory",
        str(PROJECT_ROOT / "inventory.yml"),
        str(PLAYBOOK),
    ]
    if target != "all":
        command.extend(["--tags", TARGET_TAGS[target]])

    ansible_config = Path.home() / ".ansible.cfg"
    environment = os.environ.copy()
    if "ANSIBLE_CONFIG" not in environment and ansible_config.is_file():
        environment["ANSIBLE_CONFIG"] = str(ansible_config)

    try:
        result = subprocess.run(
            command,
            cwd=Path.home(),
            capture_output=True,
            text=True,
            timeout=PLAYBOOK_TIMEOUT_SECONDS,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        output_parts = []
        for part in (exc.stdout, exc.stderr):
            if isinstance(part, bytes):
                output_parts.append(part.decode(errors="replace"))
            elif part:
                output_parts.append(part)
        output = "\n".join(output_parts)
        update_event(
            ticket_id,
            "FAILED",
            output=output[-MAX_OUTPUT_LENGTH:],
            error=f"Playbook exceeded the {PLAYBOOK_TIMEOUT_SECONDS}-second timeout.",
        )
        return
    except (FileNotFoundError, OSError) as exc:
        update_event(ticket_id, "FAILED", error=f"Unable to run Ansible: {exc}")
        return

    output = (result.stdout + "\n" + result.stderr).strip()
    update_event(
        ticket_id,
        "SUCCEEDED" if result.returncode == 0 else "FAILED",
        output=output[-MAX_OUTPUT_LENGTH:],
        error=None if result.returncode == 0 else f"Ansible exited with code {result.returncode}.",
    )
