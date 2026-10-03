import hashlib
import hmac
import json
import subprocess

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOMATION_API_TOKEN", "test-token")
    monkeypatch.setenv("AUTO_REMEDIATION_ENABLED", "false")
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
    assert ticket["details"]["remediation"]["status"] == "NOT_REQUESTED"


def test_node_exporter_alert_queues_only_allowlisted_host(client, monkeypatch):
    queued = []
    monkeypatch.setenv("AUTO_REMEDIATION_ENABLED", "true")
    monkeypatch.setattr(
        main,
        "run_remediation",
        lambda hosts, incident_id: queued.append((hosts, incident_id)),
    )

    response = client.post(
        "/api/v1/webhooks/alertmanager",
        headers={"Authorization": "Bearer test-token"},
        json={
            "receiver": "lab-webhook",
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "NodeExporterDown",
                        "instance": "192.168.56.31:9100",
                    },
                },
                {
                    "status": "firing",
                    "labels": {
                        "alertname": "NodeExporterDown",
                        "instance": "all:9100",
                    },
                },
                {
                    "status": "resolved",
                    "labels": {
                        "alertname": "NodeExporterDown",
                        "instance": "192.168.56.32:9100",
                    },
                },
            ],
        },
    )

    assert response.status_code == 202
    incident_id = response.json()["incident_id"]
    assert queued == [(["app01"], incident_id)]
    incident = client.get(
        f"/api/v1/tickets/{incident_id}",
        headers={"X-API-Key": "test-token"},
    ).json()
    assert incident["status"] == "REMEDIATION_QUEUED"
    assert incident["details"]["remediation"]["hosts"] == ["app01"]


def test_remediation_cooldown_prevents_duplicate_host_job(client, monkeypatch):
    monkeypatch.setenv("AUTO_REMEDIATION_ENABLED", "true")
    queued = []
    monkeypatch.setattr(
        main,
        "run_remediation",
        lambda hosts, incident_id: queued.append(hosts),
    )
    payload = {
        "receiver": "lab-webhook",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "NodeExporterDown",
                    "instance": "192.168.56.30:9100",
                },
            }
        ],
    }

    first = client.post(
        "/api/v1/webhooks/alertmanager",
        headers={"Authorization": "Bearer test-token"},
        json=payload,
    )
    second = client.post(
        "/api/v1/webhooks/alertmanager",
        headers={"Authorization": "Bearer test-token"},
        json=payload,
    )

    assert first.json()["remediation"] == "queued"
    assert second.json()["remediation"] == "not_queued"
    assert queued == [["web01"]]
    second_incident = client.get(
        f"/api/v1/tickets/{second.json()['incident_id']}",
        headers={"X-API-Key": "test-token"},
    ).json()
    assert second_incident["details"]["remediation"]["status"] == "COOLDOWN"


def _github_push_payload():
    return json.dumps(
        {
            "ref": "refs/heads/main",
            "after": "a" * 40,
            "repository": {"full_name": "example/enterprise-sre-lab"},
            "head_commit": {
                "id": "a" * 40,
                "message": "Update lab configuration",
            },
            "deleted": False,
        },
        separators=(",", ":"),
    ).encode()


def _github_signature(body, secret="github-test-secret"):
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_signed_allowlisted_github_push_queues_sync(client, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "github-test-secret")
    monkeypatch.setenv("GITOPS_REPOSITORY", "example/enterprise-sre-lab")
    queued = []
    monkeypatch.setattr(
        main,
        "run_playbook",
        lambda target, ticket_id: queued.append((target, ticket_id)),
    )
    monkeypatch.setattr(main, "checked_out_commit", lambda: "a" * 40)
    body = _github_push_payload()

    response = client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-test-1",
            "X-Hub-Signature-256": _github_signature(body),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 202
    ticket_id = response.json()["ticket_id"]
    assert queued == [("all", ticket_id)]
    ticket = client.get(
        f"/api/v1/tickets/{ticket_id}",
        headers={"X-API-Key": "test-token"},
    ).json()
    assert ticket["kind"] == "gitops_sync"
    assert ticket["details"]["commit"] == "a" * 40


def test_github_push_requires_matching_local_checkout(client, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "github-test-secret")
    monkeypatch.setenv("GITOPS_REPOSITORY", "example/enterprise-sre-lab")
    queued = []
    monkeypatch.setattr(
        main,
        "run_playbook",
        lambda target, ticket_id: queued.append((target, ticket_id)),
    )
    monkeypatch.setattr(main, "checked_out_commit", lambda: "b" * 40)
    body = _github_push_payload()

    response = client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-stale-checkout",
            "X-Hub-Signature-256": _github_signature(body),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 409
    assert queued == []


def test_github_push_rejects_invalid_signature(client, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "github-test-secret")
    monkeypatch.setenv("GITOPS_REPOSITORY", "example/enterprise-sre-lab")
    body = _github_push_payload()

    response = client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-invalid-signature",
            "X-Hub-Signature-256": "sha256=" + ("0" * 64),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401


def test_github_push_rejects_unallowlisted_repository(client, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "github-test-secret")
    monkeypatch.setenv("GITOPS_REPOSITORY", "example/another-repo")
    body = _github_push_payload()

    response = client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-unlisted-repo",
            "X-Hub-Signature-256": _github_signature(body),
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 403


def test_github_delivery_replay_does_not_queue_a_second_run(client, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "github-test-secret")
    monkeypatch.setenv("GITOPS_REPOSITORY", "example/enterprise-sre-lab")
    monkeypatch.setattr(main, "checked_out_commit", lambda: "a" * 40)
    queued = []
    monkeypatch.setattr(
        main,
        "run_playbook",
        lambda target, ticket_id: queued.append((target, ticket_id)),
    )
    body = _github_push_payload()
    headers = {
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": "delivery-replay-test",
        "X-Hub-Signature-256": _github_signature(body),
        "Content-Type": "application/json",
    }

    first = client.post("/api/v1/webhooks/github", content=body, headers=headers)
    replay = client.post("/api/v1/webhooks/github", content=body, headers=headers)

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json() == {
        "status": "duplicate",
        "ticket_id": first.json()["ticket_id"],
    }
    assert len(queued) == 1


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


def test_remediation_runner_limits_node_exporter_tag_to_host(client, monkeypatch):
    incident_id = "INC-remediation-test"
    main.create_event(
        event_id=incident_id,
        kind="alert",
        target="db01",
        event_status="REMEDIATION_QUEUED",
        payload={"remediation": {"status": "QUEUED", "hosts": ["db01"]}},
    )
    monkeypatch.setattr(main.shutil, "which", lambda _: "/usr/bin/ansible-playbook")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "remediation ok", "")

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    main.run_remediation(["db01"], incident_id)

    assert calls[0][-4:] == ["--tags", "node_exporter", "--limit", "db01"]
    event = main.get_event(incident_id)
    assert event["status"] == "REMEDIATED"
    assert event["details"]["remediation"]["status"] == "SUCCEEDED"
    assert event["details"]["remediation"]["hosts"] == ["db01"]
