# Names come from backend.hcl, written by bootstrap.sh:  terraform init -backend-config=backend.hcl
terraform {
  backend "azurerm" {}
}
