# Enterprise SRE Automation API

A local self-service API for starting a small, allowlisted set of this lab's
Ansible plays and recording Alertmanager webhook events. It is intended for
development on the WSL control node, not as a production API gateway.

## Security and scope

- The server binds to `127.0.0.1` for local use. For the VM Alertmanager
  integration, bind Uvicorn to `0.0.0.0` only while Windows port forwarding
  is enabled; the included firewall rule permits only `monitoring01`.
- Mutating and audit endpoints accept the `X-API-Key` header. Alertmanager
  sends the same secret as an HTTP Bearer token. Keep it out of source control
  and shell history.
- Deployment requests accept only the fixed targets `baseline`,
  `node_exporter`, `webserver`, `observability`, and `all`; they cannot supply
  shell commands, inventory paths, or playbook paths.
- Playbook output and alert payloads are stored in a local SQLite audit
  database under `$XDG_STATE_HOME` or `~/.local/state`. The database is created
  with owner-only permissions where the filesystem supports POSIX modes.
- Jobs run in-process and are serialized. The audit is persistent, but queued
  work is not resumed if the API process stops; run a single Uvicorn worker.
- Treat saved output and alert payloads as potentially sensitive. Do not put
  secrets in alerts or playbook output.
- Alert webhooks are recorded as incidents only. This API does not perform
  automatic remediation.

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

uvicorn --app-dir automation-api main:app --host 127.0.0.1 --port 5000
```

Keep this terminal running. Open another WSL terminal and load the token:

```bash
export AUTOMATION_API_TOKEN="$(cat "$HOME/.config/enterprise-sre-automation/api-token")"
curl -sS http://127.0.0.1:5000/healthz
```

The API runner uses the fixed project inventory and playbook. Ensure the WSL
Ansible config at `~/.ansible.cfg` has a `roles_path` pointing to this project's
`roles/` directory.

## Try the API

Queue an idempotent webserver role run:

```bash
curl -sS -X POST http://127.0.0.1:5000/api/v1/deployments \
  -H "X-API-Key: $AUTOMATION_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"target":"webserver"}'
```

The response includes a `ticket_id`. Poll its status and bounded output:

```bash
curl -sS \
  -H "X-API-Key: $AUTOMATION_API_TOKEN" \
  http://127.0.0.1:5000/api/v1/tickets/CHG-your-ticket-id
```

List recent deployment and alert records:

```bash
curl -sS \
  -H "X-API-Key: $AUTOMATION_API_TOKEN" \
  'http://127.0.0.1:5000/api/v1/tickets?limit=50'
```

Submit an Alertmanager-compatible example event:

```bash
curl -sS -X POST http://127.0.0.1:5000/api/v1/webhooks/alertmanager \
  -H "Authorization: Bearer $AUTOMATION_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"receiver":"lab-webhook","status":"firing","alerts":[{"status":"firing","labels":{"alertname":"ExampleAlert","instance":"web01:9100"},"annotations":{"summary":"Example test alert"}}]}'
```

## Connect Alertmanager in the monitoring VM

Alertmanager runs inside `monitoring01`; its `127.0.0.1` is the VM, not WSL.
The role sends webhook requests to the Windows VirtualBox host-only address
`192.168.56.1`. The port-forward script points that address at the current
WSL IPv4 address and restricts the Windows firewall rule to `monitoring01`.

Start the API bound to all WSL interfaces from the project root:

```bash
source "$HOME/.venvs/enterprise-sre-automation-api/bin/activate"
export AUTOMATION_API_TOKEN="$(cat "$HOME/.config/enterprise-sre-automation/api-token")"
uvicorn --app-dir automation-api main:app --host 0.0.0.0 --port 5000
```

In an **elevated Windows PowerShell** window, configure the restricted
forwarding rule (rerun after WSL restarts if its IP address changes):

```powershell
powershell.exe -ExecutionPolicy Bypass -File $env:USERPROFILE\enterprise-sre-lab\automation-api\windows-portproxy.ps1
```

With the API running, deploy the Prometheus rules and Alertmanager receiver:

```bash
ansible-playbook playbooks/site.yml --tags observability
```

Alert events are recorded only; no alert triggers automated remediation. To
test the webhook manually from WSL, use the same Bearer-token authentication
as Alertmanager:

```bash
curl -sS -X POST http://127.0.0.1:5000/api/v1/webhooks/alertmanager \
  -H "Authorization: Bearer $AUTOMATION_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"receiver":"sre-api-webhook","status":"firing","alerts":[{"status":"firing","labels":{"alertname":"NodeExporterDown","instance":"db01:9100","severity":"critical"},"annotations":{"summary":"Simulated webhook test"}}]}'

curl -sS \
  -H "X-API-Key: $AUTOMATION_API_TOKEN" \
  'http://127.0.0.1:5000/api/v1/tickets?limit=20'
```

Interactive API documentation is available at `http://127.0.0.1:5000/docs`.

## Tests

```bash
cd automation-api
python -m pip install -r requirements-dev.txt
python -m pytest
```
