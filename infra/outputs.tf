output "app_url" {
  value = "https://${azurerm_container_app.main.ingress[0].fqdn}"
}

output "acr_name" {
  value = azurerm_container_registry.main.name
}

output "audit_storage_account" {
  value = azurerm_storage_account.audit.name
}

output "workbook_id" {
  value = azurerm_application_insights_workbook.controls.id
}
