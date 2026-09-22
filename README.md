# Solvent: a governed MCP server on Azure

An MCP server with three tools against Azure and a controls layer in front of them:

- `/mcp` needs a bearer key at all; `/` and `/healthz` stay open for probes.
- **read** tools (`get_cost_summary`, `get_resource_health`) then run freely.
- **write** tools (`restart_container_app`) are refused unless the call carries a single-use
  approval token a named human minted for that exact action, still inside its window.
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
| Terraform (azurerm 4) | `infra/modules/solvent`, `infra/envs/` | One module, two environment roots. `prod` is applied from a saved plan; `dev` is planned in CI and never applied. |
| Azure DevOps Pipelines | `pipelines/` | Lint/test → plan → build → approval → apply → smoke. Workload identity federation, no stored credentials. |
| OpenTelemetry + Azure Monitor | `app/policy.py`, `app/server.py`, `infra/main.tf` | Audit log, metrics and traces in Application Insights; workbook, denial-burst and latency-SLO alerts, and an immutable export, all as Terraform. |
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

  infra/modules/    Terraform module: RG, Log Analytics, App Insights, ACR, Container Apps env +
                    app, managed identity + RBAC, action group, alerts, workbook, audit export
  infra/envs/       prod (applied) and dev (planned in CI, never applied)
  pipelines/        Azure DevOps: Validate ──► Build ──► Deploy (environment approval gate)
```

## Controls layer

`app/policy.yaml` classifies each tool:

```yaml
tools:
  get_cost_summary: read
  get_resource_health: read
  restart_container_app: write
ttl_s:
  restart_container_app: 120   # bigger blast radius, shorter approval window
approvers: []                  # empty = anyone holding the secret; Terraform sets APPROVERS
```

`@governed` wraps every tool. Per call:

1. Unknown tool → `{"denied": true, "reason": "not_in_policy"}`.
2. `read` → runs.
3. `write` → requires `approval_token = "<sig>.<ts>.<approver>"`,
   `sig = HMAC_SHA256(APPROVAL_SECRET, "tool:canonical_json(args):approver:ts")`.
   The approver is inside the signed message, so the identity cannot be relabelled; it goes
   last so an identity containing dots survives the split. Checked in order: signature
   (`approval_required`), allow-list (`approver_not_authorized`), age (`approval_expired` —
   600s by default, 120s for `restart_container_app`), reuse (`approval_replayed`).
   The allow-list is checked *after* the signature so an unsigned claim never reveals who
   is on it.
4. Denials and tool errors are returned as structured content, not raised.
5. One audit line (JSON, logger `solvent.audit`):
   `ts, tool, args_hash, decision, reason, latency_ms, trace_id, span_id`, plus
   `approved_by` once a signature has verified.
   Metrics: `mcp.tool.calls{tool,decision}`, `mcp.tool.latency_ms{tool}`.

In front of all of that, `/mcp` requires `Authorization: Bearer $API_KEY`. A rejected request
is a 401 and an audit line with `reason: "unauthenticated"`, so probing shows up beside every
other denial.

Mint a token:

```bash
APPROVAL_SECRET=... uv run python -m app.approve restart_container_app name=solvent-app
# the approver defaults to `az account show --query user.name`; override with --approver
```

## Run locally

```bash
uv sync
az login
export AZURE_SUBSCRIPTION_ID=<sub> AZURE_RESOURCE_GROUP=solvent-rg APPROVAL_SECRET=devsecret
uv run uvicorn app.server:app --port 8000
```

Leaving `API_KEY` unset keeps the local server open, which is what development wants. Set it
and every `/mcp` call needs `-H "authorization: Bearer $API_KEY"`.

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
claude mcp add --transport http solvent \
  --header "Authorization: Bearer $API_KEY" \
  https://solvent-app.happyocean-04abcb36.eastus.azurecontainerapps.io/mcp
claude
```

Claude Desktop: Settings > Connectors > Add custom connector, URL
`https://solvent-app.happyocean-04abcb36.eastus.azurecontainerapps.io/mcp`. Its connector UI
may not accept custom headers — if it does not, run the walkthrough from Claude Code or curl.

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
6. **Same token, two minutes later.** Denied, `approval_expired` — `restart_container_app`
   has a 120s window, not the 600s default.
7. **Same token, twice.** Approve a restart, then ask for the same restart again with the same
   token. Denied, `approval_replayed`.
8. **An identity that is not on the list.** Mint with `--approver someone@else.com`. Denied,
   `approver_not_authorized` — and the audit line names the identity that tried.
9. **Unknown tool.** Any MCP call to a tool name not in `policy.yaml` gets
   `{"denied": true, "reason": "not_in_policy"}`.
10. **No key at all.** `curl` `/mcp` without the header: 401, and an audit line with
    `reason: "unauthenticated"`.

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
  -H "authorization: Bearer $API_KEY" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"restart_container_app","arguments":{"name":"solvent-app","approval_token":"<token>"}}}'
```

## Deploy

Bootstrap, once:

```bash
az login
./infra/bootstrap.sh                      # registers providers, creates state storage, writes envs/*/backend.hcl
cd infra/envs/prod && terraform init -backend-config=backend.hcl
printf 'approval_secret = "%s"\napi_key = "%s"\napprovers = ""\nalert_email = "you@example.com"\nimage_tag = "bootstrap"\n' \
  "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" > secret.auto.tfvars   # gitignored
terraform apply -target=azurerm_container_registry.main   # registry first, image push needs it
ACR=$(terraform output -raw acr_name); az acr login -n $ACR
docker build --platform linux/amd64 -t $ACR.azurecr.io/solvent:bootstrap .. && docker push $ACR.azurecr.io/solvent:bootstrap
terraform apply
```

Azure DevOps, once:

1. Service connection `solvent-azure`: ARM, workload identity federation. App registration has
   Contributor + User Access Administrator on the subscription.
2. Variable group `solvent`: `AZURE_SERVICE_CONNECTION`, `APPROVAL_SECRET` and `API_KEY`
   (both secret), `APPROVERS`, `ALERT_EMAIL`.
3. Environment `prod` with an Approvals check.
4. Pipeline from `pipelines/azure-pipelines.yml`, trigger on `main`, `*.md` excluded.

After that, every push to `main` deploys through the pipeline.

## Pipeline

**Validate.** Two parallel jobs.
- app: `ruff`, `pytest` with JUnit results published.
- infra: `terraform fmt -check -recursive`, `tflint --recursive`, `checkov` (accepted findings
  and their reasons live in `.checkov.yaml`), then `validate` and `terraform plan -out=tfplan`
  on `envs/prod` with `image_tag = $(Build.SourceVersion)`. The plan is published as an artifact.
- infra_dev: `terraform plan` on `envs/dev`, never applied — proof the module composes for a
  second environment.

**Build.** `docker build`, tag = git SHA, `docker push` to ACR after `az acr login` with the
service connection identity.

**Deploy.** Deployment job on environment `prod`; the approval check pauses the run until
someone approves. Then `terraform apply` of the saved plan artifact, wait until
`latestReadyRevisionName == latestRevisionName` on the Container App, `curl /healthz`, an
MCP `tools/list` with the key asserting the three tools, and an unauthenticated `tools/list`
asserting a 401 — the gate is proved closed, not assumed.

A second pipeline, `pipelines/drift.yml`, runs `terraform plan -detailed-exitcode` nightly and
fails if Azure has stopped matching the state file.

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

Those three queries are the **workbook**, deployed from `infra/main.tf`
(`azurerm_application_insights_workbook`). Open App Insights > Workbooks > "Solvent - controls"
rather than pasting KQL.

Alerts, both `azurerm_monitor_scheduled_query_rules_alert_v2`, email via one action group:

- more than 5 denials in 5 minutes — any refusal, including unauthenticated probes;
- 15-minute p95 of `mcp.tool.latency_ms` over 2s, the SLO. The gate is microseconds, so this
  fires on the Azure dependency, not on the policy layer.

The audit trail is also exported out of Log Analytics to a storage account with an
immutability policy, so the record does not live only somewhere an operator can edit it.

## Not implemented

- Per-caller identity on `/mcp`. The bearer key is one shared secret, so the audit log names
  who *approved* a write but not who *called* it. An Entra-issued JWT verified against the
  tenant JWKS is the upgrade path.
- The nonce ledger is in-process. Correct at `max_replicas = 1`, but it empties on a cold
  start, so a token remains replayable across a restart inside its window. An Azure Table
  keyed by signature fixes it.
- Approver identities are asserted by whoever holds `APPROVAL_SECRET`, not proven. The
  allow-list restricts *which* identity may be claimed, not that the claimant is that person.
- Gating by data classification, as opposed to blast radius via per-tool approval windows.
- A *live* `dev` environment. `dev` is planned in CI from the same module, never applied.
- The audit export's immutability policy is `Unlocked`, so it is reversible. A regulated
  deployment would lock it and accept that the retention window is then permanent.
