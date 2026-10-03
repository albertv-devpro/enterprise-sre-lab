# Enterprise SRE Automation API

Local FastAPI service for allowlisted Ansible runs, Alertmanager incidents, and
signed GitHub push notifications. This is a WSL-based lab service, not a
production API gateway.

## Security and behavior

- Use `127.0.0.1` for local-only operation. VM webhooks require Uvicorn on
  `0.0.0.0`, Windows host-only port forwarding, and its restricted firewall
  rule.
- API operations use `AUTOMATION_API_TOKEN`; deployment and audit endpoints
  accept `X-API-Key`, while Alertmanager can send it as a Bearer token.
- The GitHub endpoint verifies `X-Hub-Signature-256`, and requires repository,
  branch, and unique delivery allowlists. It only queues a run when the signed
  push SHA equals the API checkout's current `HEAD`; stale checkouts return
  `409`. It never pulls, checks out, or executes unreviewed remote code.
- GitHub.com cannot reach the private Windows host-only IP. A real hosted
  webhook needs separately secured, reachable HTTPS ingress. Do not expose
  this development API directly to the internet.
- Optional remediation is disabled unless `AUTO_REMEDIATION_ENABLED=true`.
  Only firing `NodeExporterDown` alerts matching a known inventory host can
  run the `node_exporter` role, limited to that host, with a ten-minute
  persistent cooldown. It cannot repair an unreachable VM or network.
- Jobs run sequentially in-process. Audit records persist in SQLite under
  `$XDG_STATE_HOME` or `~/.local/state`; queued jobs do not survive API process
  shutdown. Run one Uvicorn worker.
- Alert payloads and Ansible output may contain sensitive data. Keep tokens,
  TLS private keys, and audit data outside the repository.

## Install and create credentials

From the repository root in Ubuntu 24.04 WSL:

```bash
sudo apt update
sudo apt install -y python3-venv openssl

python3 -m venv "$HOME/.venvs/enterprise-sre-automation-api"
source "$HOME/.venvs/enterprise-sre-automation-api/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r automation-api/requirements.txt

install -d -m 700 "$HOME/.config/enterprise-sre-automation"
export AUTOMATION_API_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
printf '%s' "$AUTOMATION_API_TOKEN" \
  > "$HOME/.config/enterprise-sre-automation/api-token"
chmod 600 "$HOME/.config/enterprise-sre-automation/api-token"
bash automation-api/generate-dev-certs.sh
```

The script creates a private local development CA and an API certificate with
SANs for `localhost`, `127.0.0.1`, and `192.168.56.1`. Certificate/private-key
files live under
`~/.config/enterprise-sre-automation/tls`, outside Git. The CA private key
`lab-ca.key` must never be copied to a VM or committed.

## Run locally over HTTPS

```bash
uvicorn --app-dir automation-api main:app \
  --host 127.0.0.1 --port 5000 \
  --ssl-certfile "$HOME/.config/enterprise-sre-automation/tls/api.crt" \
  --ssl-keyfile "$HOME/.config/enterprise-sre-automation/tls/api.key"
```

In a second WSL terminal:

```bash
export AUTOMATION_API_TOKEN="$(cat "$HOME/.config/enterprise-sre-automation/api-token")"
curl --cacert "$HOME/.config/enterprise-sre-automation/tls/lab-ca.crt" \
  -sS https://127.0.0.1:5000/healthz
```

## API endpoints

| Method | Path | Authentication | Purpose |
| --- | --- | --- | --- |
| `GET` | `/healthz` | None | Health check |
| `POST` | `/api/v1/deployments` | `X-API-Key` | Queue an approved Ansible target |
| `POST` | `/api/v1/webhooks/alertmanager` | Bearer token or `X-API-Key` | Record alerts and optionally queue guarded remediation |
| `POST` | `/api/v1/webhooks/github` | GitHub HMAC signature | Queue an allowlisted, checkout-matched push |
| `GET` | `/api/v1/tickets` | `X-API-Key` | List audit records |
| `GET` | `/api/v1/tickets/{ticket_id}` | `X-API-Key` | Inspect a deployment or incident |

Allowed deployment targets are `baseline`, `node_exporter`, `webserver`,
`observability`, and `all`. For example:

```bash
curl --cacert "$HOME/.config/enterprise-sre-automation/tls/lab-ca.crt" \
  -sS -X POST https://127.0.0.1:5000/api/v1/deployments \
  -H "X-API-Key: $AUTOMATION_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"target":"webserver"}'
```

Use the returned ticket ID to inspect status and bounded playbook output:

```bash
curl --cacert "$HOME/.config/enterprise-sre-automation/tls/lab-ca.crt" \
  -sS -H "X-API-Key: $AUTOMATION_API_TOKEN" \
  https://127.0.0.1:5000/api/v1/tickets/CHG-your-ticket-id
```

## Connect Alertmanager and optional remediation

Alertmanager runs in `monitoring01`; its `127.0.0.1` is not WSL. Start Uvicorn
on WSL interfaces with the development certificate:

```bash
source "$HOME/.venvs/enterprise-sre-automation-api/bin/activate"
export AUTOMATION_API_TOKEN="$(cat "$HOME/.config/enterprise-sre-automation/api-token")"
uvicorn --app-dir automation-api main:app \
  --host 0.0.0.0 --port 5000 \
  --ssl-certfile "$HOME/.config/enterprise-sre-automation/tls/api.crt" \
  --ssl-keyfile "$HOME/.config/enterprise-sre-automation/tls/api.key"
```

In elevated Windows PowerShell, run the port-forward script. It binds
`192.168.56.1:5000`, forwards to WSL, and permits only `monitoring01`
(`192.168.56.33`):

```powershell
powershell.exe -ExecutionPolicy Bypass -File "$env:USERPROFILE\enterprise-sre-lab\automation-api\windows-portproxy.ps1"
```

Rerun after WSL restarts if its address changes. With the API running and the
API token and CA present, deploy Alertmanager's HTTPS receiver and Prometheus
rules:

```bash
ansible-playbook playbooks/site.yml --tags observability
```

The role installs only the public CA certificate on `monitoring01`; it does
not copy the CA private key. Test connectivity from the VM:

```bash
vagrant ssh monitoring01 -c \
  "curl --cacert /etc/alertmanager/api-ca.crt -fsS https://192.168.56.1:5000/healthz"
```

Remediation is disabled by default. To enable the narrowly scoped
`NodeExporterDown` policy, stop Uvicorn and restart it after:

```bash
export AUTO_REMEDIATION_ENABLED=true
```

Only a firing `NodeExporterDown` alert with a known inventory hostname/IP can
queue the exporter role. For a manual `db01` test:

```bash
curl --cacert "$HOME/.config/enterprise-sre-automation/tls/lab-ca.crt" \
  -sS -X POST https://127.0.0.1:5000/api/v1/webhooks/alertmanager \
  -H "Authorization: Bearer $AUTOMATION_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"receiver":"sre-api-webhook","status":"firing","alerts":[{"status":"firing","labels":{"alertname":"NodeExporterDown","instance":"192.168.56.32:9100","severity":"critical"},"annotations":{"summary":"Manual remediation policy test"}}]}'
```

This test may reinstall/restart Node Exporter on `db01`; inspect the returned
incident status before repeating it.

## GitHub push endpoint

Configure a dedicated HMAC secret and exact repository/branch allowlist before
starting Uvicorn:

```bash
export GITHUB_WEBHOOK_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export GITOPS_REPOSITORY="your-owner/enterprise-sre-lab"
export GITOPS_BRANCH=main
```

The local endpoint requires the signed push's commit to equal the currently
checked-out `HEAD`. It does not perform `git pull`. GitHub.com cannot reach the
private lab address; the Windows forwarding script is not public ingress.
Until a separately secured public HTTPS endpoint exists, exercise signature,
allowlist, stale-commit and replay behavior through the tests.

## Grafana access

The observability role explicitly disables anonymous access and prevents
viewers from editing dashboards. Grafana OSS provides its built-in
Viewer/Editor/Admin roles; fine-grained RBAC features require a suitable
Grafana edition. Change the initial admin password after first login.

## Tests and troubleshooting

```bash
source "$HOME/.venvs/enterprise-sre-automation-api/bin/activate"
python -m pip install -r automation-api/requirements-dev.txt
cd automation-api
python -m pytest
```

- **Alert returns 401:** check the token in Uvicorn and
  `/etc/alertmanager/api-token`; rerun the observability tag after token
  rotation.
- **No remediation queued:** ensure the process was restarted with
  `AUTO_REMEDIATION_ENABLED=true`, the alert is firing and named
  `NodeExporterDown`, and its instance matches an inventory host. Check the
  incident for cooldown status.
- **TLS verification fails:** confirm the server certificate SAN includes
  `192.168.56.1` and the deployed CA file matches the WSL
  `lab-ca.crt`. Reapply the observability role after regenerating the CA.
- **GitHub returns 401/403:** verify the HMAC secret, event signature, and
  repository `owner/name`.
- **GitHub returns 409:** the local checkout does not match the pushed commit;
  review/update the checkout before redelivery.
- **VM cannot connect:** ensure Uvicorn listens on `0.0.0.0`, rerun the
  elevated Windows forwarding script, and test HTTPS using the deployed CA.
- **Roles/inventory not found:** set inventory and `roles_path` in WSL's
  Linux-side `~/.ansible.cfg`; Ansible ignores project config on a world-
  writable Windows mount.
