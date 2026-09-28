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
| Python + MCP SDK | `app/server.py`, `app/policy.py`, `app/entra.py`, `app/nonces.py` | The server and the gate. |
| pytest | `app/tests/` | Policy rules and tool wiring, with the Azure SDK mocked. |
| Terraform (azurerm 4) | `infra/modules/solvent`, `infra/envs/` | One module, two environment roots, both applied from a saved plan; `dev` shares `prod`'s registry and Container App Environment rather than standing up second ones. |
| Azure DevOps Pipelines | `pipelines/` | Lint/test → plan → build → approval → apply → smoke. Workload identity federation, no stored credentials. |
| OpenTelemetry + Azure Monitor | `app/policy.py`, `app/server.py`, `infra/modules/solvent/main.tf` | Audit log, metrics and traces in Application Insights; workbook, denial-burst and latency-SLO alerts, and an immutable export, all as Terraform. |
| Container Apps + ACR | `infra/modules/solvent/main.tf`, `Dockerfile` | Runs the image with a managed identity; scales to zero. |

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
  infra/envs/       prod and dev, both applied from a saved plan; dev shares prod's ACR and env
  pipelines/        Azure DevOps: Validate ──► Build ──► Deploy (environment approval gate)
```

## Controls layer

`app/policy.yaml` classifies each tool:

```yaml
# Tool class decides the gate. read = AI-assisted, runs freely. write = autonomous
# execution, blocked unless a human minted an approval token for this exact call.
tools:
  get_cost_summary: read
  get_resource_health: read
  restart_container_app: write

# How long an approval stays good, per tool. Bigger blast radius, shorter window.
# Anything not listed gets TOKEN_TTL_S (600s).
ttl_s:
  restart_container_app: 120

# Who may mint an approval. Empty means anyone holding APPROVAL_SECRET, which is
# what local development wants. Terraform sets APPROVERS in the deployed app.
approvers: []

# What the data is, independent of blast radius. A read can be harmless to run and still
# expose something commercially sensitive - that's a second axis on top of read/write.
classification:
  get_cost_summary: financial  # spend is commercially sensitive even though reading is harmless
  get_resource_health: operational
  restart_container_app: operational

# Any classification listed here takes the approval path even for a read.
gated:
  - financial
```

`@governed` wraps every tool. Per call:

1. Unknown tool → `{"denied": true, "reason": "not_in_policy"}`.
2. A `read` whose classification is not gated → runs.
3. Everything else — a `write`, or a `read` of gated data — requires
   `approval_token = "<jwt>~<hmac>.<ts>.<approver>"` (the bare `<hmac>.<ts>.<approver>` when Entra
   is unconfigured), `hmac = HMAC_SHA256(APPROVAL_SECRET, "tool:canonical_json(args):approver:ts")`.
   The approver is inside the signed message, so the identity cannot be relabelled; it goes
   last so an identity containing dots survives the split. Checked in order: signature
   (`approval_required`/`classification_gated`), the approver's own proof when Entra is
   configured (`approver_unproven`, `approver_mismatch` if it verifies but names someone else),
   allow-list (`approver_not_authorized`), age (`approval_expired` — 600s by default, 120s for
   `restart_container_app`), reuse (`approval_replayed`, `ledger_unavailable` if the replay
   ledger itself cannot answer). The allow-list is checked *after* the signature so an unsigned
   claim never reveals who is on it.
4. Denials and tool errors are returned as structured content, not raised.
5. One audit line (JSON, logger `solvent.audit`):
   `ts, caller, tool, args_hash, decision, reason, latency_ms, trace_id, span_id`, plus
   `approved_by` once a signature has verified.
   Metrics: `mcp.tool.calls{tool,decision}`, `mcp.tool.latency_ms{tool}`.

Every reason the gate and the caller guard produce:

| Reason | Decision | When |
|---|---|---|
| `read` | allowed | an ungated read |
| `approved` | allowed | signature verified, approver proven (if Entra is configured) and on the allow-list, still inside its window, first use |
| `not_in_policy` | denied | the tool name is not in `policy.yaml` |
| `no_approval_secret` | denied | approval is required but `APPROVAL_SECRET` is unset |
| `approval_required` | denied | a write with a missing or non-verifying token |
| `classification_gated` | denied | a read of gated data with a missing or non-verifying token |
| `approver_not_authorized` | denied | the approver is not on the `approvers` allow-list |
| `approver_unproven` | denied | Entra is configured and the approval's proof is missing, or present but does not verify (invalid or expired) |
| `approver_mismatch` | denied | the approver's token verified but names someone else |
| `approval_expired` | denied | the token is older than its tool's window |
| `approval_replayed` | denied | the signature has already been consumed |
| `ledger_unavailable` | denied | the replay ledger (Azure Table or in-process dict) raised instead of answering |
| `unauthenticated` | denied, 401 | `/mcp` got no `Authorization` header at all |
| `caller_token_invalid` | denied, 401 | the header was neither a valid JWT nor the shared key |

In front of all of that, `/mcp` needs either an Entra-issued JWT — verified against the tenant
JWKS, audience and issuer checked — or the shared `API_KEY`. The audit line names the caller: the
token's `oid`, or `shared_key` when the shared key was used, so the weaker path is visible in the
record rather than looking like the strong one. A rejected request is a 401 and an audit line with
`reason: "unauthenticated"`, so probing shows up beside every other denial.

Mint a token:

```bash
APPROVAL_SECRET=... ENTRA_AUDIENCE=<the aud your tenant issues> \
  uv run python -m app.approve restart_container_app name=solvent-app
# the approver defaults to `az account show --query user.name`; override with --approver
# ENTRA_AUDIENCE set means the token carries an az-issued proof of the approver's identity
# it is the token's `aud` claim, which for accessTokenAcceptedVersion 2 is the client id, not
# the api:// URI - see `## Deploy` item 5
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

`ENTRA_TENANT_ID` and `NONCE_TABLE_ENDPOINT` are unset locally, so the JWT path and the durable
replay ledger stay off and the server needs nothing from Azure to run. Deployed, Terraform sets
all four.

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
2. **Read tool, gated on classification.** "What did solvent-rg cost in the last 3 days?"
   Claude calls `get_cost_summary` and gets
   `{"denied": true, "reason": "classification_gated"}` — spend is classified `financial`, and a
   read of financial data needs a human even though running it is harmless.
3. **Write tool, denied.** "Restart the solvent-app container app using only the solvent tools."
   Claude calls `restart_container_app`, gets `{"denied": true, "reason": "approval_required"}`,
   and asks for a token.
4. **Mint a token** in a terminal, with the same `APPROVAL_SECRET` the deployment uses:
   ```bash
   APPROVAL_SECRET=... ENTRA_AUDIENCE=<the aud your tenant issues, see ## Deploy item 5> \
     uv run python -m app.approve restart_container_app name=solvent-app
   ```
   The token now carries an `az`-issued proof of the approver's identity, checked before the
   allow-list — see step 8 for what a relabelled approver runs into once that proof exists.
   Paste it to Claude: "Here is the approval token: <token>". Claude retries with
   `approval_token` set and gets `{"restarted": "solvent-app", "revision": "..."}`.
5. **Same token, different target.** "Use that token to restart an app called other-app."
   Denied, `approval_required`: the signature covers the arguments.
6. **Same token, two minutes later.** Denied, `approval_expired` — `restart_container_app`
   has a 120s window, not the 600s default.
7. **Same token, twice.** Approve a restart, then ask for the same restart again with the same
   token. Denied, `approval_replayed`.
8. **An identity that is not on the list.** Mint with `--approver someone@else.com`. With
   `ENTRA_TENANT_ID`/`ENTRA_AUDIENCE` configured — the state this plan deploys — the proof is
   checked first: it still names the real signed-in operator, not the relabelled claim, so this
   denies `approver_mismatch` before the allow-list is ever consulted (a token with no proof
   attached at all would instead deny `approver_unproven`, and only once a proof verifies for the
   claimed approver does the allow-list get a say, denying `approver_not_authorized`). On the live
   app today, with Entra unconfigured, the proof check is skipped and this denies
   `approver_not_authorized` directly — the audit line names the identity that tried either way.
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
terraform apply -target='module.solvent.azurerm_container_registry.main[0]'   # registry first, image push needs it
ACR=$(terraform output -raw acr_name); az acr login -n $ACR
docker build --platform linux/amd64 -t $ACR.azurecr.io/solvent:bootstrap .. && docker push $ACR.azurecr.io/solvent:bootstrap
terraform apply
```

Azure DevOps, once:

1. Service connection `solvent-azure`: ARM, workload identity federation. App registration has
   Contributor + User Access Administrator on the subscription.
2. Variable group `solvent`: `AZURE_SERVICE_CONNECTION`, `APPROVAL_SECRET`, `API_KEY`,
   `DEV_APPROVAL_SECRET`, `DEV_API_KEY` (all four secret), `APPROVERS`, `ALERT_EMAIL`,
   `ENTRA_TENANT_ID`, `ENTRA_AUDIENCE`.
3. Environment `prod` with an Approvals check.
4. Pipeline from `pipelines/azure-pipelines.yml`, trigger on `main`, `*.md` excluded.
5. The app registration exposing the API, with a scope and the Azure CLI (`04b07795-8ddb-461a-bbee-02f9e1bf7b46`)
   pre-authorized against it — without that, minting fails at `az account get-access-token` before
   the approval is ever signed. **`ENTRA_AUDIENCE` is the value that lands in the token's `aud`
   claim, which is not always the Application ID URI:** with `accessTokenAcceptedVersion: 2` Entra
   issues `aud` = the application (client) id, and only v1 tokens carry the URI. Read a real token
   rather than assuming — `az account get-access-token --resource <uri> --query accessToken -o tsv`,
   then decode the payload and use whatever `aud` says. Either token version works; `app/entra.py`
   accepts both issuer forms. A verified caller is authenticated, not authorized (see
   `## Not implemented`); set "Assignment required?" on the enterprise application to scope who can
   obtain a token at all.

After that, every push to `main` deploys through the pipeline.

## Pipeline

**Validate.** Two parallel jobs.
- app: `ruff`, `pytest` with JUnit results published.
- infra: `terraform fmt -check -recursive`, `tflint --recursive`, `checkov` (accepted findings
  and their reasons live in `.checkov.yaml`), then `validate` and `terraform plan -out=tfplan`
  on `envs/prod` with `image_tag = $(Build.SourceVersion)`. The plan is published as an artifact.
- infra_dev: `terraform plan` on `envs/dev`, saved as an artifact and applied in the Deploy
  stage — a real second environment, not just proof the module composes.

**Build.** `docker build`, tag = git SHA, `docker push` to ACR after `az acr login` with the
service connection identity.

**Deploy.** Two deployment jobs, both against environment `prod`, so the same single approval
check covers both and pauses the run until someone approves. `dev` applies first: `terraform
apply` of its saved plan against `envs/dev`, no smoke test. `prod` depends on `dev` and runs
second: `terraform apply` of its own saved plan, wait until `latestReadyRevisionName ==
latestRevisionName` on the Container App, `curl /healthz`, an MCP `tools/list` with the key
asserting the three tools, and an unauthenticated `tools/list` asserting a 401 — the gate is
proved closed, not assumed. Running `dev` first means a module change that breaks an environment
breaks the cheap one before it ever touches `prod`.

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

Those three queries are the **workbook**, deployed from `infra/modules/solvent/main.tf`
(`azurerm_application_insights_workbook`). Open App Insights > Workbooks > "Solvent - controls"
rather than pasting KQL.

Alerts, both `azurerm_monitor_scheduled_query_rules_alert_v2`, email via one action group:

- more than 5 denials in 5 minutes — any refusal, including unauthenticated probes;
- 15-minute p95 of `mcp.tool.latency_ms` over 2s, the SLO. The gate is microseconds, so this
  fires on the Azure dependency, not on the policy layer.

The audit trail is also exported out of Log Analytics to a storage account whose immutability
window matches the 30-day retention, so the record is protected for as long as it exists and does
not live only somewhere an operator can edit it.

## Not implemented

- The shared `API_KEY` still opens `/mcp`. A JWT is preferred and audited by `oid`, but the key
  remains because the walkthrough above runs from clients that cannot set custom headers. Removing
  it means deleting one `if` branch in `_authenticate` once every caller can get a token.
- A verified caller is authenticated, not authorized. Any identity in the tenant that can obtain
  a token for `ENTRA_AUDIENCE` passes the guard and may run every ungated tool — `APPROVERS`
  constrains who may *approve*, not who may *call*. The cheapest closure, short of an app-role
  check, is setting "Assignment required?" on the enterprise application (see `## Deploy`).
- An approver's proof is not bound to the approval it accompanies. It proves "someone holds a
  token issued to this person", not "this person approved this call" — a captured approver token
  is reusable, within its lifetime, by anyone who also holds `APPROVAL_SECRET`.
- Classification is a label a human applies to a tool, not a property of the data a call returns.
  A tool that starts returning financial data is not gated until someone relabels it.
- The audit export's immutability policy is `Unlocked`, so it is reversible. A regulated deployment
  would set `state = "Locked"` and accept that the window is then permanent, including for
  tear-down.
- `dev` shares the prod registry *and* the prod repository — it pulls the exact image prod runs,
  since the pipeline only ever pushes one. So an image that cannot be pulled fails in both
  environments at once. Giving dev its own build is a second ACR repository and a second push.
- `dev` also shares prod's Container App Environment, because a trial subscription allows exactly
  one per region and refuses the second with `MaxNumberOfRegionalEnvironmentsInSubExceeded`. Dev
  still has its own resource group, app, secrets, identity, workspace and App Insights; what it
  gives up is network and console-log isolation, since the environment owns both. The module takes
  `container_app_environment_id` for this, the same shape as `registry_id`, so a paid subscription
  restores the separation by leaving it empty.
- The JWKS cache is per process, refetched every 5 minutes (PyJWKClient's default) or on any
  unknown `kid`, not once per cold start. At `max_replicas = 1` that is one process's worth of
  refetching; scaled out it is one per replica.
- No rate limiting. The gate refuses unapproved writes but does nothing about volume.
