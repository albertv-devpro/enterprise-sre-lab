"""Local self-service API for the Enterprise SRE Lab."""

from __future__ import annotations

import hashlib
import hmac
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
    Request,
    status,
)
from pydantic import BaseModel, Field, ValidationError


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK = PROJECT_ROOT / "playbooks" / "site.yml"
LAB_CONFIG = json.loads((PROJECT_ROOT / "lab-config.json").read_text())
TARGET_TAGS = {
    "baseline": "baseline",
    "node_exporter": "node_exporter",
    "webserver": "webserver",
    "observability": "observability",
}
INVENTORY_HOSTS = LAB_CONFIG["vm_ips"]
ADDRESS_TO_HOST = {address: host for host, address in INVENTORY_HOSTS.items()}
REMEDIATION_ALERTS = {"NodeExporterDown"}
REMEDIATION_COOLDOWN_SECONDS = 600
MAX_GIT_WEBHOOK_BYTES = 1_000_000
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


class GitHubPushEvent(BaseModel):
    ref: str = Field(max_length=512)
    after: str = Field(pattern=r"^[0-9a-fA-F]{40,64}$")
    repository: dict[str, Any]
    head_commit: dict[str, Any] | None = None
    deleted: bool = False


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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS remediation_cooldowns (
                host TEXT PRIMARY KEY,
                last_run TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS github_deliveries (
                delivery_id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL
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
    payload_patch: dict[str, Any] | None = None,
) -> None:
    with connect_db() as connection:
        row = connection.execute(
            "SELECT payload FROM audit_events WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Audit event {event_id} does not exist.")
        payload = json.loads(row["payload"])
        for key, value in (payload_patch or {}).items():
            if isinstance(value, dict) and isinstance(payload.get(key), dict):
                payload[key].update(value)
            else:
                payload[key] = value
        connection.execute(
            """
            UPDATE audit_events
            SET status = ?, updated_at = ?, payload = ?, output = ?, error = ?
            WHERE id = ?
            """,
            (event_status, utc_now(), json.dumps(payload), output, error, event_id),
        )


def resolve_inventory_host(instance: str) -> str | None:
    address = instance.rsplit(":", maxsplit=1)[0]
    if address in INVENTORY_HOSTS:
        return address
    return ADDRESS_TO_HOST.get(address)


def reserve_remediation(host: str) -> bool:
    now = datetime.now(timezone.utc)
    with connect_db() as connection:
        row = connection.execute(
            "SELECT last_run FROM remediation_cooldowns WHERE host = ?", (host,)
        ).fetchone()
        if row:
            last_run = datetime.fromisoformat(row["last_run"])
            elapsed = (now - last_run).total_seconds()
            if elapsed < REMEDIATION_COOLDOWN_SECONDS:
                return False
        connection.execute(
            """
            INSERT INTO remediation_cooldowns (host, last_run)
            VALUES (?, ?)
            ON CONFLICT(host) DO UPDATE SET last_run = excluded.last_run
            """,
            (host, now.isoformat()),
        )
    return True


def create_gitops_event_once(
    *,
    event_id: str,
    delivery_id: str,
    payload: dict[str, Any],
) -> str | None:
    timestamp = utc_now()
    with connect_db() as connection:
        inserted = connection.execute(
            """
            INSERT OR IGNORE INTO github_deliveries (delivery_id, event_id)
            VALUES (?, ?)
            """,
            (delivery_id, event_id),
        )
        if inserted.rowcount == 0:
            row = connection.execute(
                "SELECT event_id FROM github_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            return row["event_id"] if row else None
        connection.execute(
            """
            INSERT INTO audit_events
                (id, kind, target, status, created_at, updated_at, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                "gitops_sync",
                "all",
                "QUEUED",
                timestamp,
                timestamp,
                json.dumps(payload),
            ),
        )
    return None


def checked_out_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Unable to inspect the local Git checkout: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"Unable to inspect the local Git checkout: {result.stderr.strip()}"
        )
    return result.stdout.strip()


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
    background_tasks: BackgroundTasks,
    _: None = Depends(require_api_key),
) -> dict[str, str]:
    incident_id = f"INC-{uuid.uuid4().hex}"
    remediation_hosts: set[str] = set()
    if os.environ.get("AUTO_REMEDIATION_ENABLED", "").lower() == "true":
        for item in alert.alerts:
            labels = item.get("labels") or {}
            if not isinstance(labels, dict):
                continue
            if (
                item.get("status") != "firing"
                or labels.get("alertname") not in REMEDIATION_ALERTS
            ):
                continue
            host = resolve_inventory_host(str(labels.get("instance", "")))
            if host:
                remediation_hosts.add(host)

    queued_hosts = sorted(
        host for host in remediation_hosts if reserve_remediation(host)
    )
    cooldown_hosts = sorted(remediation_hosts.difference(queued_hosts))
    event_status = "REMEDIATION_QUEUED" if queued_hosts else "RECEIVED"
    details = alert.model_dump(mode="json")
    details["remediation"] = {
        "status": "QUEUED" if queued_hosts else ("COOLDOWN" if cooldown_hosts else "NOT_REQUESTED"),
        "hosts": queued_hosts,
        "cooldown_hosts": cooldown_hosts,
    }
    create_event(
        event_id=incident_id,
        kind="alert",
        target=",".join(queued_hosts) or None,
        event_status=event_status,
        payload=details,
    )
    if queued_hosts:
        background_tasks.add_task(run_remediation, queued_hosts, incident_id)
    return {
        "status": "received",
        "incident_id": incident_id,
        "remediation": "queued" if queued_hosts else "not_queued",
    }


@app.post("/api/v1/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
async def receive_github_push(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
    x_github_delivery: str | None = Header(
        default=None, alias="X-GitHub-Delivery"
    ),
    x_hub_signature_256: str | None = Header(
        default=None, alias="X-Hub-Signature-256"
    ),
) -> dict[str, str]:
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
    repository_allowlist = os.environ.get("GITOPS_REPOSITORY")
    if not secret or not repository_allowlist:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub webhook secret and repository allowlist are required.",
        )

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Invalid Content-Length header."
            ) from exc
        if declared_size < 0 or declared_size > MAX_GIT_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Webhook payload is too large.")
    body = await request.body()
    if len(body) > MAX_GIT_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Webhook payload is too large.")
    if not x_hub_signature_256 or not x_hub_signature_256.startswith("sha256="):
        raise HTTPException(status_code=401, detail="Missing GitHub webhook signature.")

    expected_signature = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid GitHub webhook signature.")
    if x_github_event != "push":
        return {"status": "ignored", "reason": "only push events are accepted"}
    if not x_github_delivery or len(x_github_delivery) > 128:
        raise HTTPException(status_code=400, detail="Invalid GitHub delivery ID.")

    try:
        push = GitHubPushEvent.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    repository = push.repository.get("full_name")
    allowed_branch = os.environ.get("GITOPS_BRANCH", "main")
    expected_ref = f"refs/heads/{allowed_branch}"
    if repository != repository_allowlist:
        raise HTTPException(status_code=403, detail="Repository is not allowlisted.")
    if push.ref != expected_ref or push.deleted or not push.head_commit:
        return {"status": "ignored", "reason": "push is not a deployable branch update"}
    if not hmac.compare_digest(push.after.lower(), str(push.head_commit.get("id", "")).lower()):
        raise HTTPException(
            status_code=422,
            detail="Push commit does not match the head commit in the payload.",
        )

    commit_id = str(push.head_commit.get("id", ""))
    try:
        local_commit = checked_out_commit()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not hmac.compare_digest(local_commit.lower(), push.after.lower()):
        raise HTTPException(
            status_code=409,
            detail=(
                "Push commit is not checked out by the API service. Update and "
                "review the local checkout, then redeliver the webhook."
            ),
        )

    ticket_id = f"GITOPS-{uuid.uuid4().hex}"
    duplicate_event_id = create_gitops_event_once(
        event_id=ticket_id,
        delivery_id=x_github_delivery,
        payload={
            "repository": repository,
            "branch": allowed_branch,
            "commit": commit_id,
            "message": str(push.head_commit.get("message", ""))[:1000],
        },
    )
    if duplicate_event_id:
        return {"status": "duplicate", "ticket_id": duplicate_event_id}
    background_tasks.add_task(run_playbook, "all", ticket_id)
    return {"status": "queued", "ticket_id": ticket_id}


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


def run_remediation(hosts: list[str], incident_id: str) -> None:
    update_event(
        incident_id,
        "REMEDIATING",
        payload_patch={"remediation": {"status": "RUNNING", "hosts": hosts}},
    )
    run_playbook(
        "node_exporter",
        incident_id,
        limit_hosts=hosts,
        remediation=True,
    )


def run_playbook(
    target: str,
    ticket_id: str,
    *,
    limit_hosts: list[str] | None = None,
    remediation: bool = False,
) -> None:
    with PLAYBOOK_LOCK:
        _run_playbook(target, ticket_id, limit_hosts, remediation)


def _run_playbook(
    target: str,
    ticket_id: str,
    limit_hosts: list[str] | None,
    remediation: bool,
) -> None:
    if target not in TARGET_TAGS and target != "all":
        raise ValueError(f"Unsupported playbook target: {target}")
    if limit_hosts and any(host not in INVENTORY_HOSTS for host in limit_hosts):
        raise ValueError("Playbook limit contains a host outside the inventory allowlist.")

    running_status = "REMEDIATING" if remediation else "RUNNING"
    update_event(
        ticket_id,
        running_status,
        payload_patch={"remediation": {"status": "RUNNING"}}
        if remediation
        else None,
    )
    executable = shutil.which("ansible-playbook")
    if executable is None:
        update_event(
            ticket_id,
            "REMEDIATION_FAILED" if remediation else "FAILED",
            error="ansible-playbook was not found on the API server PATH.",
            payload_patch={"remediation": {"status": "FAILED"}}
            if remediation
            else None,
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
    if limit_hosts:
        command.extend(["--limit", ",".join(sorted(limit_hosts))])

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
            "REMEDIATION_FAILED" if remediation else "FAILED",
            output=output[-MAX_OUTPUT_LENGTH:],
            error=f"Playbook exceeded the {PLAYBOOK_TIMEOUT_SECONDS}-second timeout.",
            payload_patch={"remediation": {"status": "FAILED"}}
            if remediation
            else None,
        )
        return
    except (FileNotFoundError, OSError) as exc:
        update_event(
            ticket_id,
            "REMEDIATION_FAILED" if remediation else "FAILED",
            error=f"Unable to run Ansible: {exc}",
            payload_patch={"remediation": {"status": "FAILED"}}
            if remediation
            else None,
        )
        return

    output = (result.stdout + "\n" + result.stderr).strip()
    succeeded = result.returncode == 0
    update_event(
        ticket_id,
        (
            "REMEDIATED" if succeeded else "REMEDIATION_FAILED"
        )
        if remediation
        else ("SUCCEEDED" if succeeded else "FAILED"),
        output=output[-MAX_OUTPUT_LENGTH:],
        error=None if succeeded else f"Ansible exited with code {result.returncode}.",
        payload_patch={
            "remediation": {
                "status": "SUCCEEDED" if succeeded else "FAILED",
            }
        }
        if remediation
        else None,
    )
