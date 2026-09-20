output "app_url" {
  value = "https://${azurerm_container_app.main.ingress[0].fqdn}"
}

output "acr_name" {
  value = azurerm_container_registry.main.name
}
