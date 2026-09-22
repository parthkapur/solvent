#!/usr/bin/env bash
# One-time: create the storage account that holds Terraform state, write backend.hcl.
set -euo pipefail
RG=solvent-tfstate
LOC=${LOCATION:-eastus}
SA=solventtf$(az account show --query id -o tsv | tr -d - | cut -c1-8)

for p in Microsoft.App Microsoft.OperationalInsights Microsoft.ContainerRegistry Microsoft.Insights \
         Microsoft.AlertsManagement Microsoft.ManagedIdentity Microsoft.ResourceHealth Microsoft.CostManagement; do
  az provider register -n "$p" -o none
done

az group create -n "$RG" -l "$LOC" -o none
az storage account create -n "$SA" -g "$RG" -l "$LOC" --sku Standard_LRS --allow-blob-public-access false -o none
az storage container create -n tfstate --account-name "$SA" --auth-mode login -o none

# One backend file per environment root; same container, different state key.
for env in prod dev; do
  case "$env" in
    prod) KEY=solvent.tfstate ;;   # unchanged from the pre-module layout
    dev) KEY=dev.tfstate ;;
  esac
  cat > "$(dirname "$0")/envs/$env/backend.hcl" <<HCL
resource_group_name  = "$RG"
storage_account_name = "$SA"
container_name       = "tfstate"
key                  = "$KEY"
HCL
done
echo "wrote envs/{prod,dev}/backend.hcl -> $SA"
