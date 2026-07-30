variable "environment" {
  description = "Deployment environment (dev, uat, prod)."
  type        = string

  validation {
    condition     = contains(["dev", "uat", "prod"], var.environment)
    error_message = "environment must be one of dev, uat, prod."
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
    implicit single-tenant sentinel. Keep in step with the `datalake-scope-units-<env>` table — a scope unit
    with no tag value cannot be granted, and Athena would return its rows to anyone holding the
    tenant tag (DL-SCOPE-14).
  EOT
  type        = list(string)
  default     = []
}

variable "scope_unit_row_filters" {
  description = <<-DESC
    Row filters for Athena scope isolation, keyed `{table}:{scope_unit}`.

    One entry per (table, scope unit) pair that needs row-level isolation. Empty by default: the
    mechanism is present and validates, but enforces nothing until real tables and units are named.
    An LF-Tag cannot do this — tags apply to tables and columns, not rows.
  DESC
  type = map(object({
    table_name    = string
    scope_unit_id = string
    catalog_id    = string
  }))
  default = {}

  validation {
    condition = alltrue([
      for filter in values(var.scope_unit_row_filters) :
      can(regex("^[a-z_][a-z0-9_-]{1,63}$", filter.scope_unit_id))
    ])
    error_message = "Each scope_unit_id must match the platform scope-unit pattern (tenancy/scope_contract.py's SCOPE_UNIT_ID_PATTERN); it is interpolated into a row-filter expression."
  }

  validation {
    condition = alltrue([
      for filter in values(var.scope_unit_row_filters) :
      can(regex("^[a-z][a-z0-9_]{0,127}$", filter.table_name))
    ])
    error_message = "Each table_name must be a plain lowercase identifier — it is interpolated into a Lake Formation resource name."
  }
}

variable "scope_unit_grants" {
  description = <<-DESC
    Athena row-filter grants, keyed by an arbitrary label.

    `filter_key` must match a key of `scope_unit_row_filters`. Empty by default and deliberately so:
    the alternative is inventing principal ARNs, and a grant to a guessed principal is worse than no
    grant. Populate with the real analyst/BI role ARNs per scope unit before applying.
  DESC
  type = map(object({
    principal_arn = string
    table_name    = string
    filter_key    = string
    catalog_id    = string
  }))
  default = {}

  validation {
    condition = alltrue([
      for grant in values(var.scope_unit_grants) : can(regex("^arn:aws[a-z-]*:iam::", grant.principal_arn))
    ])
    error_message = "Each principal_arn must be an IAM ARN — a Lake Formation grant to a non-IAM principal fails at apply, not at plan."
  }
}

variable "name_prefix" {
  type        = string
  description = "Resource name prefix for the platform (e.g. 'datalake'). Combined with the environment to form every resource name."
}
