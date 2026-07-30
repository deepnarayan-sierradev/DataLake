variable "enabled" {
  description = <<-EOT
    Whether to provision the Client VPN endpoint.

    Defaults to false: DL-SERV-01 needs a customer decision on VPN topology, and an idle
    endpoint bills hourly. The module is complete so the decision is the only blocker.
  EOT
  type        = bool
  default     = false
}

variable "environment" {
  description = "Deployment environment (dev, uat, prod)."
  type        = string

  validation {
    condition     = contains(["dev", "uat", "prod"], var.environment)
    error_message = "environment must be one of dev, uat, prod."
  }
}

variable "vpc_id" {
  description = "VPC the serving store lives in."
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnets to associate the endpoint with (one per availability zone)."
  type        = list(string)
  default     = []
}

variable "private_subnet_cidrs" {
  description = "CIDRs of those private subnets; used for routes and the client SG egress."
  type        = list(string)
  default     = []
}

variable "client_cidr_block" {
  description = <<-EOT
    CIDR assigned to VPN clients. Must not overlap the VPC, and must be at least /22 —
    AWS requires that, and a smaller block runs out of addresses as tenant count grows.
  EOT
  type        = string
  default     = "10.100.0.0/22"
}

variable "server_certificate_arn" {
  description = "ACM ARN of the VPN server certificate."
  type        = string
  default     = ""
}

variable "client_root_certificate_arn" {
  description = <<-EOT
    ACM ARN of the client root certificate chain. Per-tenant client certificates are issued
    from this chain, which is what makes a single tenant's access individually revocable.
  EOT
  type        = string
  default     = ""
}

variable "tenant_access_groups" {
  description = <<-EOT
    Map of access-group id to the CIDR that group may reach.

    Per-tenant by construction. Deliberately not a single authorize-all-groups rule: one
    issued certificate must not be sufficient to reach the whole VPC.
  EOT
  type        = map(string)
  default     = {}
}

variable "serving_store_port" {
  description = "Port the serving store listens on (3306 MySQL, 5432 PostgreSQL, 5439 Redshift)."
  type        = number
  default     = 3306
}

variable "dns_servers" {
  description = "DNS servers pushed to clients; empty uses the VPC resolver."
  type        = list(string)
  default     = []
}

variable "session_timeout_hours" {
  description = "Hours before a connected client must re-authenticate."
  type        = number
  default     = 8

  validation {
    condition     = contains([8, 10, 12, 24], var.session_timeout_hours)
    error_message = "session_timeout_hours must be one of 8, 10, 12, 24 (the AWS-permitted values)."
  }
}

variable "certificate_expiry_warning_days" {
  description = "Days before certificate expiry at which to alarm."
  type        = number
  default     = 30
}

variable "concurrent_connection_warning" {
  description = "Concurrent connections above which to alarm (a sizing signal, not a limit)."
  type        = number
  default     = 100
}

variable "log_retention_days" {
  description = "Retention for VPN connection logs — the audit trail of serving-store access."
  type        = number
  default     = 365
}

variable "logs_kms_key_arn" {
  description = "KMS CMK ARN for encrypting the VPN log group."
  type        = string
}

variable "alarm_sns_topic_arn" {
  description = "SNS topic for VPN alarms. Empty disables them."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Common resource tags."
  type        = map(string)
  default     = {}
}

variable "name_prefix" {
  type        = string
  description = "Resource name prefix for the platform (e.g. 'datalake'). Combined with the environment to form every resource name."
}
