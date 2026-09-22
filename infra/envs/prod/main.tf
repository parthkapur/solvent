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

  project         = "solvent"
  location        = "eastus"
  image_tag       = var.image_tag
  approval_secret = var.approval_secret
  api_key         = var.api_key
  approvers       = var.approvers
  alert_email     = var.alert_email
}
