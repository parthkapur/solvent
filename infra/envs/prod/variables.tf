variable "image_tag" {
  type        = string
  description = "Git SHA of the image in ACR. Set by the pipeline."
}

variable "approval_secret" {
  type      = string
  sensitive = true
}

variable "api_key" {
  type        = string
  sensitive   = true
  description = "Bearer key callers must present on /mcp."
}

variable "approvers" {
  type        = string
  default     = ""
  description = "Comma-separated identities allowed to mint approval tokens."
}

variable "alert_email" {
  type = string
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
