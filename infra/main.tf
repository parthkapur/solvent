data "azurerm_subscription" "current" {}

locals {
  suffix = substr(sha1(data.azurerm_subscription.current.subscription_id), 0, 6)
  image  = "${azurerm_container_registry.main.login_server}/${var.project}:${var.image_tag}"
  tags   = { project = var.project, managed_by = "terraform" }
}

resource "azurerm_resource_group" "main" {
  name     = "${var.project}-rg"
  location = var.location
  tags     = local.tags
}

# --- observability -----------------------------------------------------------

resource "azurerm_log_analytics_workspace" "main" {
  name                = "${var.project}-law"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.tags
}

resource "azurerm_application_insights" "main" {
  name                = "${var.project}-appi"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  workspace_id        = azurerm_log_analytics_workspace.main.id
  application_type    = "web"
  tags                = local.tags
}

# Audit lines an operator cannot edit. Unlocked immutability is reversible, which is what a
# demo wants; a regulated deployment would set state = "Locked" and accept that it is forever.
resource "azurerm_storage_account" "audit" {
  name                            = "${var.project}audit${local.suffix}"
  resource_group_name             = azurerm_resource_group.main.name
  location                        = azurerm_resource_group.main.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false

  blob_properties {
    versioning_enabled = true
  }

  immutability_policy {
    state                         = "Unlocked"
    period_since_creation_in_days = 7
    allow_protected_append_writes = true
  }

  tags = local.tags
}

resource "azurerm_log_analytics_data_export_rule" "audit" {
  name                    = "${var.project}-audit-export"
  resource_group_name     = azurerm_resource_group.main.name
  workspace_resource_id   = azurerm_log_analytics_workspace.main.id
  destination_resource_id = azurerm_storage_account.audit.id
  table_names             = ["AppTraces"]
  enabled                 = true
}

resource "azurerm_monitor_action_group" "main" {
  name                = "${var.project}-ops"
  resource_group_name = azurerm_resource_group.main.name
  short_name          = "solventops"
  email_receiver {
    name          = "ops"
    email_address = var.alert_email
  }
}

# Fires when something keeps hitting the gate: >5 denials in 5 minutes.
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "denied_writes" {
  name                 = "${var.project}-denied-writes"
  resource_group_name  = azurerm_resource_group.main.name
  location             = azurerm_resource_group.main.location
  scopes               = [azurerm_log_analytics_workspace.main.id]
  severity             = 2
  evaluation_frequency = "PT5M"
  window_duration      = "PT5M"
  criteria {
    # Any refusal: a denied write, an unlisted approver, a replayed token, or an
    # unauthenticated request. Bursts mean something is hammering the gate.
    query                   = <<-KQL
      AppTraces
      | where tostring(Properties.decision) == "denied"
    KQL
    time_aggregation_method = "Count"
    operator                = "GreaterThan"
    threshold               = 5
  }
  action {
    action_groups = [azurerm_monitor_action_group.main.id]
  }
  tags = local.tags
}

# Objective: a tool call answers in under 2s. The gate itself is microseconds, so anything
# slow is Azure upstream - this fires on the dependency, not on the policy layer.
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "latency_slo" {
  name                 = "${var.project}-latency-slo"
  resource_group_name  = azurerm_resource_group.main.name
  location             = azurerm_resource_group.main.location
  scopes               = [azurerm_log_analytics_workspace.main.id]
  severity             = 3
  evaluation_frequency = "PT5M"
  window_duration      = "PT15M"

  criteria {
    # AppMetrics stores pre-aggregated buckets, so this is p95 over each bucket's max - an
    # upper bound on the real p95, which is the safe direction to be wrong in for an SLO.
    query                   = <<-KQL
      AppMetrics
      | where Name == "mcp.tool.latency_ms"
      | summarize p95 = percentile(Max, 95)
    KQL
    time_aggregation_method = "Maximum"
    metric_measure_column   = "p95"
    operator                = "GreaterThan"
    threshold               = 2000
  }

  action {
    action_groups = [azurerm_monitor_action_group.main.id]
  }

  tags = local.tags
}

# --- identity & registry -----------------------------------------------------

resource "azurerm_user_assigned_identity" "app" {
  name                = "${var.project}-app-id"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.tags
}

resource "azurerm_container_registry" "main" {
  name                = "${var.project}acr${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "Basic"
  admin_enabled       = false
  tags                = local.tags
}

resource "azurerm_role_assignment" "acr_pull" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_role_assignment" "rg_reader" {
  scope                = azurerm_resource_group.main.id
  role_definition_name = "Reader"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

resource "azurerm_role_assignment" "cost_reader" {
  scope                = data.azurerm_subscription.current.id
  role_definition_name = "Cost Management Reader"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# --- runtime -----------------------------------------------------------------

resource "azurerm_container_app_environment" "main" {
  name                       = "${var.project}-env"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  tags                       = local.tags
}

resource "azurerm_container_app" "main" {
  name                         = "${var.project}-app"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  tags                         = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.app.id
  }

  secret {
    name  = "appinsights-cs"
    value = azurerm_application_insights.main.connection_string
  }
  secret {
    name  = "approval-secret"
    value = var.approval_secret
  }
  secret {
    name  = "api-key"
    value = var.api_key
  }

  template {
    min_replicas = 0
    max_replicas = 1
    container {
      name   = var.project
      image  = local.image
      cpu    = 0.25
      memory = "0.5Gi"
      env {
        name        = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        secret_name = "appinsights-cs"
      }
      env {
        name        = "APPROVAL_SECRET"
        secret_name = "approval-secret"
      }
      env {
        name        = "API_KEY"
        secret_name = "api-key"
      }
      env {
        name  = "APPROVERS"
        value = var.approvers
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.app.client_id
      }
      env {
        name  = "AZURE_SUBSCRIPTION_ID"
        value = data.azurerm_subscription.current.subscription_id
      }
      env {
        name  = "AZURE_RESOURCE_GROUP"
        value = azurerm_resource_group.main.name
      }
      liveness_probe {
        transport = "HTTP"
        path      = "/healthz"
        port      = 8000
      }
    }
  }

  ingress {
    external_enabled = true
    target_port      = 8000
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  depends_on = [azurerm_role_assignment.acr_pull]
}

# The app may restart itself (the one write tool). Scoped to the app only.
resource "azurerm_role_assignment" "self_contributor" {
  scope                = azurerm_container_app.main.id
  role_definition_name = "Contributor"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# --- dashboard ---------------------------------------------------------------

# The queries an operator actually runs, in a diff rather than pasted into a portal blade.
# Workbook names must be a GUID; a literal keeps the resource stable across applies.
resource "azurerm_application_insights_workbook" "controls" {
  name                = "7f3b0c6e-4a21-4c9d-9b8e-2f5a1d6c0e11"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  display_name        = "Solvent - controls"
  source_id           = lower(azurerm_application_insights.main.id)
  tags                = local.tags

  data_json = jsonencode({
    version = "Notebook/1.0"
    items = [
      {
        type = 1
        content = {
          json = "## Solvent controls\nEvery decision the gate has made. Defined in `infra/main.tf`."
        }
      },
      {
        type = 3
        content = {
          version       = "KqlItem/1.0"
          size          = 0
          title         = "Calls by tool and decision (24h)"
          queryType     = 0
          visualization = "barchart"
          query         = <<-KQL
            AppTraces
            | where TimeGenerated > ago(24h) and isnotempty(Properties.decision)
            | summarize calls = count() by tool = tostring(Properties.tool), decision = tostring(Properties.decision)
            | order by calls desc
          KQL
        }
      },
      {
        type = 3
        content = {
          version       = "KqlItem/1.0"
          size          = 0
          title         = "Denials, with the reason and who tried"
          queryType     = 0
          visualization = "table"
          query         = <<-KQL
            AppTraces
            | where TimeGenerated > ago(24h) and tostring(Properties.decision) == "denied"
            | project TimeGenerated,
                      tool = tostring(Properties.tool),
                      reason = tostring(Properties.reason),
                      approved_by = tostring(Properties.approved_by)
            | order by TimeGenerated desc
          KQL
        }
      },
      {
        type = 3
        content = {
          version       = "KqlItem/1.0"
          size          = 0
          title         = "Approved writes - who approved what"
          queryType     = 0
          visualization = "table"
          query         = <<-KQL
            AppTraces
            | where tostring(Properties.reason) == "approved"
            | project TimeGenerated,
                      tool = tostring(Properties.tool),
                      approved_by = tostring(Properties.approved_by),
                      args_hash = tostring(Properties.args_hash)
            | order by TimeGenerated desc
          KQL
        }
      },
    ]
  })
}
