# A real second environment from the same module: its own resource group, app and secrets, sharing
# the prod registry because a second one would cost money and prove nothing. Container Apps scales
# to zero, so an idle dev app is close to free.

terraform {
  required_version = ">= 1.9"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  features {}
}

module "solvent" {
  source = "../../modules/solvent"

  project               = "solventdev"
  location              = "eastus"
  image_tag             = var.image_tag
  approval_secret       = var.approval_secret
  api_key               = var.api_key
  approvers             = var.approvers
  alert_email           = var.alert_email
  entra_tenant_id       = var.entra_tenant_id
  entra_audience        = var.entra_audience
  registry_id           = var.registry_id
  registry_login_server = var.registry_login_server
  # ... and prod's environment too. A trial subscription allows exactly one Container App
  # Environment per region, so a second one in eastus is refused outright with
  # MaxNumberOfRegionalEnvironmentsInSubExceeded. Sharing costs nothing and dev keeps its own
  # app, secrets, workspace and App Insights - only the container console logs land in prod's
  # workspace. ponytail: a paid subscription could give dev its own; nothing else would change.
  container_app_environment_id = var.container_app_environment_id
  # dev pulls the identical image prod runs, out of prod's registry - the pipeline only ever
  # pushes one repository, "solvent", so dev must ask for that rather than "solventdev".
  image_repository = "solvent"
}
