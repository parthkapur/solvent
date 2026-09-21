# Solvent: a governed MCP server on Azure

An MCP server with three tools against Azure and a controls layer in front of them:

- **read** tools (`get_cost_summary`, `get_resource_health`) run freely.
- **write** tools (`restart_container_app`) are refused unless the call carries an approval token
  a human minted for that exact action within the last 10 minutes.
- every call, allowed or denied, is logged with reason, latency and trace id, and counted in a
  metric. Bursts of denials raise an alert.

Deployed to Azure Container Apps by Terraform through an Azure DevOps pipeline with a manual
approval before apply. The app is small by design; the project is about running the controls
layer properly.

Live at https://solvent-app.happyocean-04abcb36.eastus.azurecontainerapps.io (`/` describes the
service, `/healthz`, `/mcp`).

## Stack

| Piece | Where | What it does |
|---|---|---|
| Python + MCP SDK | `app/server.py`, `app/policy.py` | The server and the gate. |
| pytest | `app/tests/` | Policy rules and tool wiring, with the Azure SDK mocked. |
| Terraform (azurerm 4) | `infra/` | All Azure resources, remote state, applied from a saved plan. |
| Azure DevOps Pipelines | `pipelines/` | Lint/test → plan → build → approval → apply → smoke. Workload identity federation, no stored credentials. |
| OpenTelemetry + Azure Monitor | `app/policy.py`, `app/server.py`, `infra/main.tf` | Audit log, metrics and traces in Application Insights; alert rule on denied-write bursts. |
| Container Apps + ACR | `infra/main.tf`, `Dockerfile` | Runs the image with a managed identity; scales to zero. |

## Architecture

```
  Claude / any MCP client
          │  streamable HTTP  (POST /mcp)
          ▼
┌─────────────────────────────────────────────────────┐   Azure Container Apps
│  app/server.py      MCPServer + / + /healthz         │   (user-assigned identity)
│  app/policy.py      @governed ─ allow-list           │
│                              ─ HMAC approval token   │──► Cost Management (read)
│                              ─ audit event + metrics │──► Resource Health   (read)
│  app/azure_clients.py                                │──► Container Apps   (write, gated)
└───────────────┬─────────────────────────────────────┘
                │ OpenTelemetry (azure-monitor-opentelemetry)
                ▼
  Application Insights ──► Log Analytics ──► scheduled-query alert (>5 denied writes / 5 min)

  infra/            Terraform: RG, Log Analytics, App Insights, ACR, Container Apps env + app,
                    managed identity + RBAC, action group, alert rule
  pipelines/        Azure DevOps: Validate ──► Build ──► Deploy (environment approval gate)
```

## Controls layer

`app/policy.yaml` classifies each tool:

```yaml
tools:
  get_cost_summary: read
  get_resource_health: read
  restart_container_app: write
```

`@governed` wraps every tool. Per call:

1. Unknown tool → `{"denied": true, "reason": "not_in_policy"}`.
2. `read` → runs.
3. `write` → requires `approval_token = "<sig>.<ts>"`,
   `sig = HMAC_SHA256(APPROVAL_SECRET, "tool:canonical_json(args):ts")`.
   Missing or wrong → `approval_required`; older than 10 minutes → `approval_expired`.
   The token is bound to tool, arguments and mint time.
4. Denials and tool errors are returned as structured content, not raised.
5. One audit line (JSON, logger `solvent.audit`):
   `ts, tool, args_hash, decision, reason, latency_ms, trace_id, span_id`.
   Metrics: `mcp.tool.calls{tool,decision}`, `mcp.tool.latency_ms{tool}`.

Mint a token:

```bash
APPROVAL_SECRET=... uv run python -m app.approve restart_container_app name=solvent-app
```

## Run locally

```bash
uv sync
az login
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

Tests: `uv run pytest`. Policy tests hit `decide`/`governed` directly; contract tests go through
`MCPServer.call_tool` with `azure_clients` monkeypatched.

## Try it

Add the deployed server to an MCP client. Claude Code:

```bash
claude mcp add --transport http solvent https://solvent-app.happyocean-04abcb36.eastus.azurecontainerapps.io/mcp
claude
```

Claude Desktop: Settings > Connectors > Add custom connector, URL
`https://solvent-app.happyocean-04abcb36.eastus.azurecontainerapps.io/mcp`.

Then, in the chat:

1. **Read tool, no gate.** "Is everything in the solvent-rg resource group healthy?"
   Claude calls `get_resource_health` and answers.
2. **Read tool, upstream error surfaced as data.** "What did solvent-rg cost in the last 3 days?"
   Claude calls `get_cost_summary`. On a new subscription this returns
   `{"error": "(429) Too many requests"}` until Cost Management has billing history.
3. **Write tool, denied.** "Restart the solvent-app container app using only the solvent tools."
   Claude calls `restart_container_app`, gets `{"denied": true, "reason": "approval_required"}`,
   and asks for a token.
4. **Mint a token** in a terminal, with the same `APPROVAL_SECRET` the deployment uses:
   ```bash
   APPROVAL_SECRET=... uv run python -m app.approve restart_container_app name=solvent-app
   ```
   Paste it to Claude: "Here is the approval token: <token>". Claude retries with
   `approval_token` set and gets `{"restarted": "solvent-app", "revision": "..."}`.
5. **Same token, different target.** "Use that token to restart an app called other-app."
   Denied, `approval_required`: the signature covers the arguments.
6. **Same token, 10 minutes later.** Denied, `approval_expired`.
7. **Unknown tool.** Any MCP call to a tool name not in `policy.yaml` gets
   `{"denied": true, "reason": "not_in_policy"}`.

Every step above is now a row in App Insights (`solvent-appi` > Logs):

```kql
AppTraces
| where isnotempty(Properties.decision)
| project TimeGenerated, tool=tostring(Properties.tool), decision=tostring(Properties.decision), reason=tostring(Properties.reason)
| order by TimeGenerated desc
```

Same thing with curl, no AI client:

```bash
URL=https://solvent-app.happyocean-04abcb36.eastus.azurecontainerapps.io
curl -s -X POST $URL/mcp -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"restart_container_app","arguments":{"name":"solvent-app","approval_token":"<token>"}}}'
```

## Deploy

Bootstrap, once:

```bash
az login
./infra/bootstrap.sh                      # registers providers, creates state storage, writes infra/backend.hcl
cd infra && terraform init -backend-config=backend.hcl
printf 'approval_secret = "%s"\nalert_email = "you@example.com"\nimage_tag = "bootstrap"\n' "$(openssl rand -hex 32)" > secret.auto.tfvars   # gitignored
terraform apply -target=azurerm_container_registry.main   # registry first, image push needs it
ACR=$(terraform output -raw acr_name); az acr login -n $ACR
docker build --platform linux/amd64 -t $ACR.azurecr.io/solvent:bootstrap .. && docker push $ACR.azurecr.io/solvent:bootstrap
terraform apply
```

Azure DevOps, once:

1. Service connection `solvent-azure`: ARM, workload identity federation. App registration has
   Contributor + User Access Administrator on the subscription.
2. Variable group `solvent`: `AZURE_SERVICE_CONNECTION`, `APPROVAL_SECRET` (secret), `ALERT_EMAIL`.
3. Environment `prod` with an Approvals check.
4. Pipeline from `pipelines/azure-pipelines.yml`, trigger on `main`, `*.md` excluded.

After that, every push to `main` deploys through the pipeline.

## Pipeline

**Validate.** Two parallel jobs.
- app: `ruff`, `pytest` with JUnit results published.
- infra: `terraform fmt -check`, `validate`, `tflint`, `terraform plan -out=tfplan` with
  `image_tag = $(Build.SourceVersion)`. The plan is published as an artifact.

**Build.** `docker build`, tag = git SHA, `docker push` to ACR after `az acr login` with the
service connection identity.

**Deploy.** Deployment job on environment `prod`; the approval check pauses the run until
someone approves. Then `terraform apply` of the saved plan artifact, wait until
`latestReadyRevisionName == latestRevisionName` on the Container App, `curl /healthz`, and an
MCP `tools/list` asserting the three tools.

Terraform authenticates with `ARM_USE_OIDC=true` and the `$idToken` from `AzureCLI@2`
(`addSpnToEnvironment: true`). `pipelines/install-tools.yml` installs pinned Terraform and tflint;
`ubuntu-latest` no longer ships them.

## Observability

Every tool call produces a request span, an audit trace with the decision in `Properties`, and
two custom metrics. `trace_id` on the audit line joins it to its request.

Denied writes in the last 24h, joined to their requests:

```kql
AppTraces
| where TimeGenerated > ago(24h) and tostring(Properties.decision) == "denied"
| project TimeGenerated, tool=tostring(Properties.tool), reason=tostring(Properties.reason), OperationId
| join kind=leftouter (AppRequests | project OperationId, Url, DurationMs, ResultCode) on OperationId
| order by TimeGenerated desc
```

Calls by tool and decision:

```kql
AppMetrics
| where Name == "mcp.tool.calls"
| summarize sum(Sum) by tool=tostring(Properties.tool), decision=tostring(Properties.decision)
```

Alert: `azurerm_monitor_scheduled_query_rules_alert_v2`, more than 5 denied writes in 5 minutes,
email via action group.

## Not implemented

- Single-use nonces; a token can be replayed within its 10-minute window.
- RBAC on who may mint approvals, with minting audited.
- Second environment via Terraform modules, `dev` stage without approval.
- SLO burn-rate alerts on `mcp.tool.latency_ms`; dashboard as code.
- Gating by blast radius or data classification rather than read/write.
