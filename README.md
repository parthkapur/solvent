# Solvent — a governed MCP server on Azure

## What this is

AI assistants are increasingly given tools that act on real systems. In a regulated environment
the question is not *whether* to allow that but *how*: which actions may an AI take on its own,
which need a human to sign off, and how is every action recorded?

Solvent is a small, deployed answer to that question. It is an MCP server (the standard way AI
assistants such as Claude call external tools) with three tools against Azure, and a **controls
layer** in front of them:

- **read** tools (`get_cost_summary`, `get_resource_health`) run freely — AI-assisted work.
- **write** tools (`restart_container_app`) are refused unless the call carries an approval token
  that a human minted for *that exact action, in the last 10 minutes* — autonomous execution is
  never allowed by default.
- every call, allowed or denied, is logged with why, how long it took, and a trace id, and
  counted in a metric. Repeated denials raise an alert.

The application is deliberately small. The point of the project is running that controls layer
the way a regulated platform team would: infrastructure as code, a gated pipeline, tests, and
telemetry.

## Where each piece of the stack is used, and why

| Piece | Where | Why it is here |
|---|---|---|
| **Python + MCP SDK** | `app/server.py`, `app/policy.py` | The server and the gate. MCP so any AI client can use it without custom integration. |
| **pytest** | `app/tests/` | Proves the gate's rules (read allowed, write denied, token accepted, tampered/expired token denied) and that each tool is wired correctly — without ever calling Azure. If a rule breaks, the pipeline stops. |
| **Terraform (azurerm)** | `infra/` | Every Azure resource — compute, registry, logging, alerting, permissions — is declared in code and applied from a reviewed plan, not clicked together in the portal. Reproducible, reviewable, destroyable. |
| **Azure DevOps Pipelines** | `pipelines/` | Push to `main` → tests → plan → image build → **human approval** → apply → smoke test. The same "human gate before a write" idea as the app, applied to infrastructure. Authenticates to Azure with workload identity federation, so there is no secret to leak. |
| **OpenTelemetry + Azure Monitor** | `app/policy.py`, `app/server.py`, `infra/main.tf` | The audit trail and the smoke detector. Logs, metrics and traces from one wrapper, shipped to Application Insights, queryable in KQL, with an alert rule on abuse. This is what makes the control *observable* rather than just present. |
| **Azure Container Apps + ACR** | `infra/main.tf`, `Dockerfile` | Runs the container with a managed identity (no credentials in the app), scales to zero when idle. |

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
3. `write` → requires `approval_token = "<sig>.<ts>"` where
   `sig = HMAC_SHA256(APPROVAL_SECRET, "tool:canonical_json(args):ts")`.
   Missing or wrong → `{"denied": true, "reason": "approval_required"}`; older than 10 minutes →
   `{"denied": true, "reason": "approval_expired"}`. The token is bound to the tool, the exact
   arguments **and** the time it was minted, so it cannot be replayed against another target or
   kept for later.
4. Denials and tool errors are returned as data, never raised, so the AI gets a clear signal to
   ask a human instead of an opaque error to retry.
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

Tests: `uv run pytest` — policy decisions, plus contract tests that call each tool through
`MCPServer.call_tool` with the Azure SDK monkeypatched. Azure is never called in tests.

Use it from Claude Code:

```bash
claude mcp add --transport http solvent https://<app-url>/mcp
```

## Deploy

**Once, to bootstrap:**

```bash
az login
./infra/bootstrap.sh                      # registers providers, creates the state storage account, writes infra/backend.hcl
cd infra && terraform init -backend-config=backend.hcl
printf 'approval_secret = "%s"\nalert_email = "you@example.com"\nimage_tag = "bootstrap"\n' "$(openssl rand -hex 32)" > secret.auto.tfvars   # gitignored
terraform apply -target=azurerm_container_registry.main   # the registry must exist before the first image push
ACR=$(terraform output -raw acr_name); az acr login -n $ACR
docker build --platform linux/amd64 -t $ACR.azurecr.io/solvent:bootstrap .. && docker push $ACR.azurecr.io/solvent:bootstrap
terraform apply
curl $(terraform output -raw app_url)/healthz
```

**Azure DevOps, once:**

1. Service connection `solvent-azure` — Azure Resource Manager, workload identity federation
   (no client secret). Its app registration has Contributor + User Access Administrator on the
   subscription (the latter because Terraform creates role assignments).
2. Variable group `solvent`: `AZURE_SERVICE_CONNECTION`, `APPROVAL_SECRET` (secret), `ALERT_EMAIL`.
3. Environment `prod` with an **Approvals** check. This is the human gate for every apply.
4. Pipeline from `pipelines/azure-pipelines.yml`, triggered on `main`.

After that, every push to `main` deploys through the pipeline. Local `terraform apply` is for
bootstrap only.

## The pipeline, stage by stage

The pipeline is the assembly line from "code changed" to "running in production". Three stages,
each one a gate the change must pass.

**1. Validate** — *is this change correct?* Two jobs run in parallel:

- *App*: `ruff` (lint) and `pytest` (the policy and contract tests). Results are published to the
  run's **Tests** tab.
- *Infra*: `terraform fmt -check` (formatting), `terraform validate` (syntax and references),
  `tflint` (Azure-specific mistakes), then `terraform plan -out=tfplan`. The plan is the exact
  list of changes this commit would make to Azure. It is saved as a **build artifact** so a
  reviewer can read it, and so the Deploy stage applies *that* plan and nothing else.

**2. Build** — *package it.* `docker build` on the agent, tagged with the full git commit SHA, then
pushed to the container registry. The tag means any running revision can be traced back to the
exact commit that produced it. The agent logs in to the registry with the pipeline's federated
identity; no registry password exists.

**3. Deploy** — *put it in production, with a human in the loop.*

- The stage targets the `prod` **environment**, which has an approval check. The run pauses here
  until a named person approves it in Azure DevOps. The approval is recorded against the run.
- `terraform apply tfplan` applies the *saved plan from stage 1*. If Azure has drifted since the
  plan was made, apply refuses rather than doing something the reviewer did not see.
- Smoke test: wait until the Container App reports the **new** revision as ready (the previous
  revision keeps serving until then, so a bare health check can pass against old code), then hit
  `/healthz` and send an MCP `tools/list` request and check the three tools are listed.

The Terraform steps authenticate with `ARM_USE_OIDC` and the short-lived id token the service
connection issues — the same workload-identity pattern as the app's managed identity. Nowhere in
the repository, the pipeline or the variable group is there a cloud credential.

`pipelines/install-tools.yml` installs pinned versions of Terraform and tflint; the hosted
`ubuntu-latest` image no longer ships them.

## Observability

Every tool call produces a request span (auto-instrumented), an audit trace with the decision in
`Properties`, and two custom metrics. `trace_id` on the audit line joins it to its request.

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

Alert (`azurerm_monitor_scheduled_query_rules_alert_v2` in Terraform): more than 5 denied writes
in 5 minutes → email via the action group. A single denial is normal; a burst is the signal.

## Not implemented

- Single-use nonces (a token can be replayed within its 10-minute window).
- RBAC on who may mint approvals, with the minting itself audited.
- A second environment via Terraform modules and a `dev` stage without approval.
- SLO burn-rate alerts on `mcp.tool.latency_ms`; dashboard as code.
- Gate on blast radius / data classification, not just read vs write.
