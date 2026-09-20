variable "location" {
  type    = string
  default = "eastus"
}

variable "project" {
  type    = string
  default = "solvent"
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
