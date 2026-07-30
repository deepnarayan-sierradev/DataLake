variable "environment" {
  description = "Environment name; namespaces the signing profile."
  type        = string
}

variable "untrusted_artifact_on_deployment" {
  description = "Warn or Enforce. Enforce rejects unsigned artefacts, which the current local build produces."
  type        = string
  default     = "Warn"

  validation {
    condition     = contains(["Warn", "Enforce"], var.untrusted_artifact_on_deployment)
    error_message = "untrusted_artifact_on_deployment must be Warn or Enforce."
  }
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}

variable "name_prefix" {
  type        = string
  description = "Resource name prefix for the platform (e.g. 'datalake'). Combined with the environment to form every resource name."
}
