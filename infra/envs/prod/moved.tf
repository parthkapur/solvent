# Re-addresses the pre-module state into module.solvent. The backend key is unchanged, so no
# state is copied - these only tell Terraform the resources moved, not that they changed.

moved {
  from = azurerm_resource_group.main
  to   = module.solvent.azurerm_resource_group.main
}

moved {
  from = azurerm_log_analytics_workspace.main
  to   = module.solvent.azurerm_log_analytics_workspace.main
}

moved {
  from = azurerm_application_insights.main
  to   = module.solvent.azurerm_application_insights.main
}

moved {
  from = azurerm_storage_account.audit
  to   = module.solvent.azurerm_storage_account.audit
}

moved {
  from = azurerm_log_analytics_data_export_rule.audit
  to   = module.solvent.azurerm_log_analytics_data_export_rule.audit
}

moved {
  from = azurerm_monitor_action_group.main
  to   = module.solvent.azurerm_monitor_action_group.main
}

moved {
  from = azurerm_monitor_scheduled_query_rules_alert_v2.denied_writes
  to   = module.solvent.azurerm_monitor_scheduled_query_rules_alert_v2.denied_writes
}

moved {
  from = azurerm_monitor_scheduled_query_rules_alert_v2.latency_slo
  to   = module.solvent.azurerm_monitor_scheduled_query_rules_alert_v2.latency_slo
}

moved {
  from = azurerm_user_assigned_identity.app
  to   = module.solvent.azurerm_user_assigned_identity.app
}

moved {
  from = azurerm_container_registry.main
  to   = module.solvent.azurerm_container_registry.main
}

moved {
  from = azurerm_role_assignment.acr_pull
  to   = module.solvent.azurerm_role_assignment.acr_pull
}

moved {
  from = azurerm_role_assignment.rg_reader
  to   = module.solvent.azurerm_role_assignment.rg_reader
}

moved {
  from = azurerm_role_assignment.cost_reader
  to   = module.solvent.azurerm_role_assignment.cost_reader
}

moved {
  from = azurerm_container_app_environment.main
  to   = module.solvent.azurerm_container_app_environment.main
}

moved {
  from = azurerm_container_app.main
  to   = module.solvent.azurerm_container_app.main
}

moved {
  from = azurerm_role_assignment.self_contributor
  to   = module.solvent.azurerm_role_assignment.self_contributor
}

moved {
  from = azurerm_application_insights_workbook.controls
  to   = module.solvent.azurerm_application_insights_workbook.controls
}
