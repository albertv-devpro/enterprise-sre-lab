# Enterprise SRE Automation API

A local FastAPI service for approved Ansible runs, Alertmanager incidents, and
signed GitHub push notifications. It is a learning tool for the WSL control
node, not a production API gateway.

## Security and behavior

- Bind to `127.0.0.1` for local use. The VM webhook setup requires binding to
  `0.0.0.0` in WSL and using the included Windows port-forward script, whose
  firewall rule allows only `monitoring01`.
- Deployment and ticket endpoints require `X-API-Key`. Alertmanager accepts
  the same API token as an HTTP Bearer token.
- GitHub pushes use a separate `GITHUB_WEBHOOK_SECRET`, verified against
  `X-Hub-Signature-256`. Only the configured repository and branch are
  accepted.
- Deployment targets are allowlisted; callers cannot provide shell commands,
  inventory paths, playbook paths, or arbitrary Ansible limits.
- Alert-driven remediation is disabled by default. When enabled, only firing
  `NodeExporterDown` alerts for a known inventory host can queue the
  `node_exporter` role for that host. A persistent ten-minute per-host
  cooldown prevents repeated runs.
- Remediation only reapplies the Node Exporter package/service role. It cannot
  repair an unreachable VM, host, or network.
- The GitHub endpoint runs the full playbook using the API server's existing
  checkout. It does **not** fetch or check out the commit from GitHub.
- Jobs run sequentially in-process. Audit records persist in SQLite, but queued
  jobs do not survive API process shutdown. Run one Uvicorn worker.
- Audit payloads and captured Ansible output may contain sensitive data.
  SQLite is stored under `$XDG_STATE_HOME` or `~/.local/state`.
- Alert events create audit records only, except for the explicitly enabled
  and constrained Node Exporter remediation policy.

## Install and run in WSL

From the project root:

```bash
sudo apt update
sudo apt install -y python3-venv

python3 -m venv "$HOME/.venvs/enterprise-sre-automation-api"
source "$HOME/.venvs/enterprise-sre-automation-api/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r automation-api/requirements.txt

install -d -m 700 "$HOME/.config/enterprise-sre-automation"
export AUTOMATION_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
printf '%s' "$AUTOMATION_API_TOKEN" \
  > "$HOME/.config/enterprise-sre-automation/api-token"
chmod 600 "$HOME/.config/enterprise-sre-automation/api-token"
```

Start for local-only API use:

```bash
uvicorn --app-dir automation-api main:app --host 127.0.0.1 --port 5000
```

In a second WSL terminal:

```bash
export AUTOMATION_API_TOKEN="$(cat "$HOME/.config/enterprise-sre-automation/api-token")"
curl -sS http://127.0.0.1:5000/healthz
```

The API runner uses this repository's fixed inventory and playbook. Ensure
`~/.ansible.cfg` in WSL includes a `roles_path` for this checkout.

## API endpoints

| Method | Path | Authentication | Purpose |
| --- | --- | --- | --- |
| `GET` | `/healthz` | None | Health check |
| `POST` | `/api/v1/deployments` | `X-API-Key` | Queue an approved Ansible target |
| `POST` | `/api/v1/webhooks/alertmanager` | Bearer token or `X-API-Key` | Record alerts; optionally queue guarded remediation |
| `POST` | `/api/v1/webhooks/github` | GitHub HMAC signature | Queue an allowlisted push sync |
| `GET` | `/api/v1/tickets` | `X-API-Key` | List audit records |
| `GET` | `/api/v1/tickets/{ticket_id}` | `X-API-Key` | Inspect a deployment or incident |

Approved deployment targets are `baseline`, `node_exporter`, `webserver`,
`observability`, and `all`.

Queue an idempotent webserver role run:

```bash
curl -sS -X POST http://127.0.0.1:5000/api/v1/deployments \
  -H "X-API-Key: $AUTOMATION_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"target":"webserver"}'
```

Use its returned ticket ID to inspect status and bounded Ansible output:

```bash
curl -sS \
  -H "X-API-Key: $AUTOMATION_API_TOKEN" \
  http://127.0.0.1:5000/api/v1/tickets/CHG-your-ticket-id
```

## Alertmanager and guarded remediation

Alertmanager runs in `monitoring01`; its `127.0.0.1` is not WSL. For VM
webhooks, start the API on all WSL interfaces:

```bash
source "$HOME/.venvs/enterprise-sre-automation-api/bin/activate"
export AUTOMATION_API_TOKEN="$(cat "$HOME/.config/enterprise-sre-automation/api-token")"
uvicorn --app-dir automation-api main:app --host 0.0.0.0 --port 5000
```

From elevated Windows PowerShell, set the host-only port forwarding. Replace
the project directory with your own checkout location if needed:

```powershell
powershell.exe -ExecutionPolicy Bypass -File "$env:USERPROFILE\enterprise-sre-lab\automation-api\windows-portproxy.ps1"
```

Apply Prometheus alert rules and the Alertmanager receiver from WSL:

```bash
ansible-playbook playbooks/site.yml --tags observability
```

The forwarding script targets the current WSL IPv4 address; rerun it after a
WSL restart if that address changes. The host-only forwarding/firewall is not
public internet ingress.

Remediation is opt-in. Stop Uvicorn, then set this variable before restarting
it:

```bash
export AUTO_REMEDIATION_ENABLED=true
```

The policy matches only firing `NodeExporterDown` alerts whose `instance`
contains a known inventory IP or hostname. To manually test using `db01`:

```bash
curl -sS -X POST http://127.0.0.1:5000/api/v1/webhooks/alertmanager \
  -H "Authorization: Bearer $AUTOMATION_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"receiver":"sre-api-webhook","status":"firing","alerts":[{"status":"firing","labels":{"alertname":"NodeExporterDown","instance":"192.168.56.32:9100","severity":"critical"},"annotations":{"summary":"Manual remediation policy test"}}]}'

curl -sS \
  -H "X-API-Key: $AUTOMATION_API_TOKEN" \
  'http://127.0.0.1:5000/api/v1/tickets?limit=20'
```

Inspect the incident's `remediation` details and final status. The test may
reinstall/restart Node Exporter on `db01`. A per-host cooldown blocks another
attempt for ten minutes, including failed attempts.

## GitHub push webhook

Configure a dedicated webhook secret and repository allowlist in the API
process environment:

```bash
export GITHUB_WEBHOOK_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export GITOPS_REPOSITORY="your-owner/enterprise-sre-lab"
export GITOPS_BRANCH=main
```

Set the same secret in the GitHub webhook settings, choose **application/json**
and subscribe to **push** events. The endpoint verifies the HMAC signature and
queues a full playbook run only for the exact configured `owner/repository` and
branch. The API server must already have the intended code checked out; this
endpoint does not synchronize Git itself.

The current Windows port-forward/firewall permits only `monitoring01`; GitHub
cannot reach that private address. A real GitHub.com webhook needs separately
managed, authenticated HTTPS ingress and should not be implemented by exposing
this development API directly to the public internet. Until then, exercise the
HMAC/repository behavior with the automated tests.

## Tests

```bash
source "$HOME/.venvs/enterprise-sre-automation-api/bin/activate"
python -m pip install -r automation-api/requirements-dev.txt
python -m pytest automation-api/tests
```

Tests cover authenticated deployment requests, alert recording, safe
remediation targeting and cooldown, signed webhook verification, repository
allowlisting, and playbook argument construction.

## Troubleshooting

- **`401` from the Alertmanager webhook:** confirm Uvicorn has the same
  `AUTOMATION_API_TOKEN` copied to `/etc/alertmanager/api-token` by the
  observability role. Reapply `--tags observability` after changing the token.
- **No remediation queued:** confirm `AUTO_REMEDIATION_ENABLED=true`, restart
  Uvicorn, and use a `firing` `NodeExporterDown` alert with an inventory
  hostname/IP. Check the incident's remediation details for cooldown status.
- **Remediation fails:** inspect the ticket output. Ansible requires the VM to
  be reachable over SSH; remediation cannot fix host/network outages.
- **GitHub webhook returns `503`:** configure both `GITHUB_WEBHOOK_SECRET` and
  `GITOPS_REPOSITORY` before starting Uvicorn.
- **GitHub webhook returns `401` or `403`:** confirm the GitHub secret matches,
  the signed body is unchanged, and the repository is configured as
  `owner/name`.
- **VM cannot connect to the API:** ensure Uvicorn is listening on `0.0.0.0`,
  rerun the elevated Windows port-forward script after WSL restarts, and test
  `http://192.168.56.1:5000/healthz` from `monitoring01`.
- **Playbook can't find roles or inventory:** set `roles_path` and inventory in
  WSL's Linux-side `~/.ansible.cfg`; Ansible ignores project config on
  world-writable Windows mounts.
