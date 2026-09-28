output "app_url" {
  value = "https://${azurerm_container_app.main.ingress[0].fqdn}"
}

output "acr_name" {
  value = var.registry_id != "" ? "" : azurerm_container_registry.main[0].name
}

output "acr_id" {
  value = local.registry_id
}

output "acr_login_server" {
  value = local.registry_server
}

output "audit_storage_account" {
  value = azurerm_storage_account.audit.name
}

output "nonce_table_endpoint" {
  value = "https://${azurerm_storage_account.audit.name}.table.core.windows.net"
}

output "workbook_id" {
  value = azurerm_application_insights_workbook.controls.id
}
