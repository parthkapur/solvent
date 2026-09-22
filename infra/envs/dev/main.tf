# Planned in CI, never applied. It proves the module composes for a second environment;
# applying it would stand up a second Container Apps environment and cost real money.

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

  project         = "solventdev"
  location        = "eastus"
  image_tag       = var.image_tag
  approval_secret = var.approval_secret
  api_key         = var.api_key
  approvers       = var.approvers
  alert_email     = var.alert_email
}
