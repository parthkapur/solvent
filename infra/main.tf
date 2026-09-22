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

resource "azurerm_monitor_action_group" "main" {
  name                = "${var.project}-ops"
  resource_group_name = azurerm_resource_group.main.name
  short_name          = "solventops"
  email_receiver {
    name          = "ops"
    email_address = var.alert_email
  }
}

# Fires when the AI side keeps hitting the gate: >5 denied writes in 5 minutes.
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "denied_writes" {
  name                 = "${var.project}-denied-writes"
  resource_group_name  = azurerm_resource_group.main.name
  location             = azurerm_resource_group.main.location
  scopes               = [azurerm_log_analytics_workspace.main.id]
  severity             = 2
  evaluation_frequency = "PT5M"
  window_duration      = "PT5M"
  criteria {
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
