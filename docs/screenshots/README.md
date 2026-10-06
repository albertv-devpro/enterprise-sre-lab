# Runtime screenshots

Store only genuine screenshots captured from a running, verified lab here.
The root README embeds the Grafana dashboard, Prometheus targets, healthy
alert-rule baseline, Prometheus firing state, and Grafana during the controlled
exporter-down test. Redacted FastAPI and Alertmanager output is preserved in
`fastapi-alertmanager-incident-summary.txt` and
`alertmanager-active-node-exporter-alert.txt`. Do not use mockups or screenshots
that imply a test passed when it did not.

## Suggested evidence

Capture a small set that demonstrates distinct parts of the end-to-end system:

| File | Capture | Evidence shown / verification |
| --- | --- | --- |
| `grafana-fleet-dashboard-healthy.png` | Provisioned Enterprise SRE Lab fleet dashboard | Shows exporter availability and host metrics at healthy baseline |
| `prometheus-targets-all-up.png` | Prometheus **Status → Targets** | Shows all four `enterprise-nodes` targets `UP` |
| `prometheus-alerts-healthy.png` | Prometheus **Alerts** page | Shows both rules inactive, with zero firing |
| `prometheus-alert-node-exporter-pending.png` | Prometheus **Alerts** page | Shows `NodeExporterDown` pending, with zero firing |
| `prometheus-alert-node-exporter-firing.png` | Prometheus **Alerts** page | Shows one alert firing during the controlled exporter-down test |
| `grafana-fleet-dashboard-exporter-down.png` | Grafana fleet dashboard during the test | Shows 75% exporter availability and one firing alert |
| `fastapi-alertmanager-incident-summary.txt` | Redacted FastAPI ticket and Uvicorn access-log summary | Confirms the firing incident was recorded and webhook requests returned HTTP 202; access logs do not identify payload/status per request |
| `alertmanager-active-node-exporter-alert.txt` | Redacted `amtool` and Alertmanager API output | Confirms the active `app01` alert and `sre-api-webhook` receiver; Debian package has no web UI |
| `api-incident-log.png` | A safe view of the FastAPI incident/event record | Confirm the event arrived and redact tokens, credentials, personal paths, and unrelated payload data |

Prefer three or four informative captures over several near-duplicates. A
single well-framed dashboard may communicate more than multiple partial views.
Only include the alert and incident screenshots when those paths have actually
been exercised and verified.

The Prometheus firing screenshot, Grafana firing dashboard, and Alertmanager
active-alert output are consistent with the same controlled exporter-down
test. The FastAPI audit record currently available is from a separate earlier
delivery, not a timestamp-matched record of that Alertmanager snapshot. Capture
a fresh API audit entry and a resolved notification for exact delivery and
lifecycle correlation.

## Before committing images

- Crop to the relevant application content; avoid browser profiles, account
  names, personal notifications, unrelated windows, and desktop details.
- Remove or obscure credentials, API tokens, webhook secrets, authorization
  headers, private keys, session cookies, and sensitive incident payloads.
- Review hostnames, IP addresses, usernames, file paths, repository URLs, and
  timestamps for information you do not want public. Keep screenshots
  consistent with the privacy-safe examples in the documentation.
- Ensure text is readable at normal README display size and the image is not
  excessively large.
- Verify that the screenshot depicts the current committed configuration, not
  stale data or an earlier implementation.

Do not add certificates, key files, logs, database files, or raw alert payloads
to this directory. This directory is only for reviewed image evidence.
