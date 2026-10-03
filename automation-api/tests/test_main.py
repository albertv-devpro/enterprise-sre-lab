import json
import subprocess

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOMATION_API_TOKEN", "test-token")
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "audit.sqlite3")
    with TestClient(main.app) as test_client:
        yield test_client


def test_deployment_is_authenticated_and_queued(client, monkeypatch):
    queued = []
    monkeypatch.setattr(
        main,
        "run_playbook",
        lambda target, ticket_id: queued.append((target, ticket_id)),
    )

    response = client.post(
        "/api/v1/deployments",
        headers={"X-API-Key": "test-token"},
        json={"target": "webserver"},
    )

    assert response.status_code == 202
    ticket = response.json()["ticket"]
    assert ticket["target"] == "webserver"
    assert ticket["status"] == "QUEUED"
    assert queued == [("webserver", ticket["ticket_id"])]
    assert client.get(
        f"/api/v1/tickets/{ticket['ticket_id']}",
        headers={"X-API-Key": "test-token"},
    ).json()["status"] == "QUEUED"


def test_deployment_rejects_unknown_targets(client):
    response = client.post(
        "/api/v1/deployments",
        headers={"X-API-Key": "test-token"},
        json={"target": "arbitrary-command"},
    )

    assert response.status_code == 422


def test_deployment_requires_api_key(client):
    response = client.post(
        "/api/v1/deployments",
        json={"target": "webserver"},
    )

    assert response.status_code == 401


def test_deployment_accepts_bearer_token(client, monkeypatch):
    queued = []
    monkeypatch.setattr(
        main,
        "run_playbook",
        lambda target, ticket_id: queued.append((target, ticket_id)),
    )

    response = client.post(
        "/api/v1/deployments",
        headers={"Authorization": "Bearer test-token"},
        json={"target": "webserver"},
    )

    assert response.status_code == 202
    assert queued[0][0] == "webserver"


def test_alertmanager_payload_is_recorded(client):
    response = client.post(
        "/api/v1/webhooks/alertmanager",
        headers={"Authorization": "Bearer test-token"},
        json={
            "receiver": "lab-webhook",
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "ExampleAlert"},
                }
            ],
        },
    )

    assert response.status_code == 202
    ticket_id = response.json()["incident_id"]
    ticket = client.get(
        f"/api/v1/tickets/{ticket_id}",
        headers={"X-API-Key": "test-token"},
    ).json()
    assert ticket["kind"] == "alert"
    assert ticket["status"] == "RECEIVED"
    assert ticket["details"]["receiver"] == "lab-webhook"


def test_ticket_list_is_auditable(client):
    with main.connect_db() as connection:
        connection.execute(
            """
            INSERT INTO audit_events
                (id, kind, target, status, created_at, updated_at, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "CHG-test",
                "deployment",
                "webserver",
                "SUCCEEDED",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                json.dumps({"target": "webserver"}),
            ),
        )

    response = client.get(
        "/api/v1/tickets",
        headers={"X-API-Key": "test-token"},
    )

    assert response.status_code == 200
    assert response.json()["audit_trail"][0]["ticket_id"] == "CHG-test"


def test_playbook_runner_uses_fixed_paths_and_target_tags(client, monkeypatch):
    ticket_id = "CHG-runner-test"
    main.create_event(
        event_id=ticket_id,
        kind="deployment",
        target="observability",
        event_status="QUEUED",
        payload={"target": "observability"},
    )
    monkeypatch.setattr(main.shutil, "which", lambda _: "/usr/bin/ansible-playbook")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "playbook ok", "")

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    main.run_playbook("observability", ticket_id)

    command, options = calls[0]
    assert command == [
        "/usr/bin/ansible-playbook",
        "--inventory",
        str(main.PROJECT_ROOT / "inventory.yml"),
        str(main.PLAYBOOK),
        "--tags",
        "observability",
    ]
    assert options["cwd"] == main.Path.home()
    event = main.get_event(ticket_id)
    assert event["status"] == "SUCCEEDED"
    assert event["output"] == "playbook ok"
