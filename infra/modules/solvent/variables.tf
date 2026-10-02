variable "location" {
  type = string
}

variable "project" {
  type = string
}

variable "image_tag" {
  type        = string
  description = "Git SHA of the image in ACR. Set by the pipeline."
}

variable "approval_secret" {
  type      = string
  sensitive = true
}

variable "alert_email" {
  type = string
}

variable "api_key" {
  type        = string
  sensitive   = true
  description = "Bearer key callers must present on /mcp."
}

variable "approvers" {
  type        = string
  default     = ""
  description = "Comma-separated identities allowed to mint approval tokens. Empty means anyone holding approval_secret."
}

variable "registry_id" {
  type        = string
  default     = ""
  description = "Existing ACR to attach to. Empty creates one, which is what prod does."
}

variable "registry_login_server" {
  type        = string
  default     = ""
  description = "Login server of registry_id. Required when registry_id is set."
}

variable "entra_tenant_id" {
  type        = string
  default     = ""
  description = "Tenant whose JWKS verifies caller and approver tokens. Empty disables the JWT path."
}

variable "entra_audience" {
  type        = string
  default     = ""
  description = "Application ID URI tokens must be addressed to."
}

variable "image_repository" {
  type        = string
  default     = ""
  description = "Repository inside the registry to pull from. Empty uses the project name, which is what prod does."
}

variable "container_app_environment_id" {
  type        = string
  default     = ""
  description = "Existing Container App Environment to run in. Empty creates one, which is what prod does."
}

variable "fault_injection" {
  type        = bool
  default     = false
  description = "Let authenticated callers add latency with the X-Solvent-Fault header. Dev only; the demo harness uses it."
}
