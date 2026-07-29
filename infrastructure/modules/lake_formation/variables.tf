variable "environment" {
  description = "Deployment environment (dev, staging, prod)."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of dev, staging, prod."
  }
}

variable "glue_database_name" {
  description = "Glue database holding the analytics tables the tags govern."
  type        = string
}

variable "tenant_codes" {
  description = <<-EOT
    Tenant codes forming the `tenant` LF-Tag value set.

    Must include every onboarded tenant: a table tagged with a value absent from this list
    cannot be granted, so an omission fails closed (no access) rather than open.
  EOT
  type        = list(string)
  default     = ["demo"]

  validation {
    condition     = length(var.tenant_codes) > 0
    error_message = "tenant_codes must not be empty; an empty tag value set grants nothing."
  }
}

variable "tenant_scoped_principals" {
  description = <<-EOT
    Principals granted tag-scoped read access, keyed by a stable name.

    `department` empty grants across every department for that tenant (a tenant-admin role);
    a named department restricts to it plus `shared` reference data (DL-SEC-10).
  EOT
  type = map(object({
    principal_arn = string
    tenant_code   = string
    department    = string
  }))
  default = {}
}

variable "data_lake_admin_arns" {
  description = <<-EOT
    Lake Formation data-lake administrators.

    Keep this list short: an admin bypasses every tag grant, so each entry is a hole in the
    isolation the tags provide. Empty skips managing the settings resource entirely.
  EOT
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Common resource tags."
  type        = map(string)
  default     = {}
}

variable "scope_unit_ids" {
  description = <<-EOT
    Registered scope unit ids (franchisee/brand/location) that may appear as `scope_unit` LF-Tag
    values. Empty means no partitioned tenant exists yet, in which case the tag carries only the
    implicit single-tenant sentinel. Keep in step with the `EdlScopeUnit` table — a scope unit
    with no tag value cannot be granted, and Athena would return its rows to anyone holding the
    tenant tag (DL-SCOPE-14).
  EOT
  type        = list(string)
  default     = []
}
