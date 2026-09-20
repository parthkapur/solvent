# Solvent — a governed MCP server on Azure

An MCP server whose tools are split into **read** (AI-assisted, runs freely) and **write**
(autonomous execution, blocked unless a human minted an approval token for that exact call).
Deployed to Azure Container Apps by Terraform through an Azure DevOps pipeline, with
OpenTelemetry traces, metrics and audit events in Azure Monitor.

## Architecture

```
  Claude / any MCP client
          │  streamable HTTP  (POST /mcp)
          ▼
┌─────────────────────────────────────────────────────┐   Azure Container Apps
│  app/server.py      MCPServer + /healthz             │   (user-assigned identity)
│  app/policy.py      @governed ─ allow-list           │
│                              ─ HMAC approval token   │──► Cost Management (read)
│                              ─ audit event + metrics │──► Resource Health   (read)
│  app/azure_clients.py                                │──► Container Apps   (write, gated)
└───────────────┬─────────────────────────────────────┘
                │ OpenTelemetry (azure-monitor-opentelemetry)
                ▼
  Application Insights ──► Log Analytics ──► scheduled-query alert (>5 denied writes / 5 min)

  infra/            Terraform: RG, LAW, App Insights, ACR, CAE, Container App, RBAC, alert
  pipelines/        Azure DevOps: Validate ──► Build ──► Deploy (environment approval gate)
```

## The controls layer

`app/policy.yaml` classifies each tool:

```yaml
tools:
  get_cost_summary: read
  get_resource_health: read
  restart_container_app: write
```

`@governed` wraps every tool and, per call:

1. Unknown tool → `{"denied": true, "reason": "not_in_policy"}`.
2. `read` → runs.
3. `write` → requires `approval_token == HMAC_SHA256(APPROVAL_SECRET, "tool:canonical_json(args)")`.
   Missing or wrong → `{"denied": true, "reason": "approval_required"}`. The token is bound to
   the tool **and** the exact arguments, so an approval for `name=solvent-app` cannot be replayed
   against another app.
4. Denials are returned as data, not raised.
5. Emits one audit line (JSON, logger `solvent.audit`) with
   `ts, tool, args_hash, decision, reason, latency_ms, trace_id, span_id`, and increments
   `mcp.tool.calls{tool,decision}` / records `mcp.tool.latency_ms{tool}`.

A human mints a token on the side:

```bash
APPROVAL_SECRET=... uv run python -m app.approve restart_container_app name=solvent-app
```

## Run locally

```bash
uv sync
az login                                  # DefaultAzureCredential picks this up
export AZURE_SUBSCRIPTION_ID=<sub> AZURE_RESOURCE_GROUP=solvent-rg APPROVAL_SECRET=devsecret
uv run uvicorn app.server:app --port 8000
```

```bash
curl -s localhost:8000/healthz
curl -s -X POST localhost:8000/mcp \
  -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"restart_container_app","arguments":{"name":"solvent-app"}}}'
# → {"denied": true, "reason": "approval_required"}
```

Tests: `uv run pytest` (policy decisions + contract tests through `MCPServer.call_tool`; Azure SDK
is monkeypatched, never called).

## Deploy

**Once:**

```bash
az login
./infra/bootstrap.sh                      # state storage account → infra/backend.hcl (commit it)
cd infra && terraform init -backend-config=backend.hcl
printf 'approval_secret = "%s"\nalert_email = "you@example.com"\nimage_tag = "bootstrap"\n' "$(openssl rand -hex 32)" > secret.auto.tfvars   # gitignored
terraform apply -target=azurerm_container_registry.main   # registry must exist before the first image push
ACR=$(terraform output -raw acr_name); az acr login -n $ACR
docker build --platform linux/amd64 -t $ACR.azurecr.io/solvent:bootstrap .. && docker push $ACR.azurecr.io/solvent:bootstrap
terraform apply
curl $(terraform output -raw app_url)/healthz
```

**Azure DevOps:**

1. Service connection (ARM, workload identity or SP) with Owner on the subscription — role
   assignments need it.
2. Variable group `solvent`: `AZURE_SERVICE_CONNECTION`, `APPROVAL_SECRET` (secret), `ALERT_EMAIL`.
3. Environment `prod` with an **Approvals** check. That check is the human gate for every apply.
4. Pipeline from `pipelines/azure-pipelines.yml`.

New ADO orgs have no hosted parallelism until Microsoft grants it (a form, 2–3 business days).
Until then, run a self-hosted agent: `docker run -e AZP_URL=... -e AZP_TOKEN=... mcr.microsoft.com/azure-pipelines/vsts-agent`.

## Observability

Every tool call produces a request span (auto-instrumented Starlette), an audit trace with the
decision in `Properties`, and two custom metrics. `trace_id` on the audit line joins it to its
request.

Every denied write in the last 24h, with the request it belonged to:

```kql
AppTraces
| where TimeGenerated > ago(24h) and tostring(Properties.decision) == "denied"
| project TimeGenerated, tool=tostring(Properties.tool), reason=tostring(Properties.reason), OperationId
| join kind=leftouter (AppRequests | project OperationId, Url, DurationMs, ResultCode) on OperationId
| order by TimeGenerated desc
```

Metrics in `AppMetrics`: `mcp.tool.calls` (dims `tool`, `decision`), `mcp.tool.latency_ms` (dim `tool`).

```kql
AppMetrics
| where Name startswith "mcp."
| summarize sum(Sum) by Name, tool=tostring(Properties.tool), decision=tostring(Properties.decision)
```

Alert (Terraform, `azurerm_monitor_scheduled_query_rules_alert_v2`): more than 5 denied writes in
5 minutes → action group email.

## Known state (new subscription, 2026-09-20)

- `get_cost_summary` returns `{"error": "(429) Too many requests"}` — Cost Management throttles
  subscriptions with no billing history yet. Clears on its own.
- Resource Health intermittently returned 409 for ~30 min after provider registration; the client
  retries three times. Stable since.
- `az acr build` is unavailable on trial subscriptions; images are built on the agent/laptop.

## Pipeline notes

- **Validate**: ruff, pytest (JUnit published), `terraform fmt -check`, `validate`, `tflint`, and
  `terraform plan -out=tfplan` published as an artifact.
- **Build**: `docker build` + push on the agent, tagged with the git SHA, authenticated via
  `az acr login` from the service connection (ACR Tasks are disabled on trial subscriptions).
- **Deploy**: applies the *saved* plan from Validate, so what was reviewed is what gets applied.
  Then a smoke test: `/healthz` and an MCP `tools/list`.

## Not implemented

- Token expiry (`exp` claim) and single-use nonces — replay protection.
- RBAC on who may mint approvals, with the minting itself audited.
- A second environment via Terraform modules and a `dev` stage without approval.
- SLO burn-rate alerts on `mcp.tool.latency_ms`; dashboard as code.
- Gate on blast radius / data classification, not just read vs write.
