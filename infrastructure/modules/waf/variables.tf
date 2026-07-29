variable "environment" {
  description = "Deployment environment (dev, staging, prod)."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of dev, staging, prod."
  }
}

variable "region" {
  description = "AWS region; required as a WAF alarm dimension."
  type        = string
}

variable "api_gateway_stage_arn" {
  description = <<-EOT
    ARN of the API Gateway stage to associate the WAF with. Empty leaves the ACL
    unassociated, which is how a WAF is provisioned ahead of the API it protects
    without a dependency cycle.
  EOT
  type        = string
  default     = ""
}

variable "enforcement_mode" {
  description = <<-EOT
    "audit" counts what would be blocked without blocking it; "enforce" blocks.

    Audit first is not optional caution — DL-SEC-13 requires alarming before blocking, and a
    managed rule set enforced blind will reject a legitimate request shape sooner or later.
  EOT
  type        = string
  default     = "audit"

  validation {
    condition     = contains(["audit", "enforce"], var.enforcement_mode)
    error_message = "enforcement_mode must be audit or enforce."
  }
}

variable "per_ip_rate_limit" {
  description = <<-EOT
    Requests per five minutes from one IP before the rate rule fires.

    Deliberately generous: a dashboard refreshing a dozen widgets behind a corporate NAT
    generates far more traffic per apparent IP than a single user does.
  EOT
  type        = number
  default     = 10000

  validation {
    condition     = var.per_ip_rate_limit >= 100
    error_message = "per_ip_rate_limit must be at least 100 (the WAF minimum)."
  }
}

variable "per_tenant_rate_limit" {
  description = "Requests per five minutes per tenant path before the rate rule fires."
  type        = number
  default     = 20000

  validation {
    condition     = var.per_tenant_rate_limit >= 100
    error_message = "per_tenant_rate_limit must be at least 100 (the WAF minimum)."
  }
}

variable "max_request_body_bytes" {
  description = "Request bodies larger than this are blocked outright in both modes."
  type        = number
  default     = 1048576
}

variable "blocked_request_alarm_threshold" {
  description = "Blocked (or counted, in audit mode) requests per five minutes before alarming."
  type        = number
  default     = 50
}

variable "security_log_retention_days" {
  description = <<-EOT
    Retention for the WAF security log group. Longer than application logs by design:
    OWASP A09 and SOC 2 evidence both need a forensics window measured in months.
  EOT
  type        = number
  default     = 365
}

variable "logs_kms_key_arn" {
  description = "KMS CMK ARN for encrypting the WAF log group."
  type        = string
}

variable "alarm_sns_topic_arn" {
  description = "SNS topic for WAF alarms. Empty disables the alarms."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Common resource tags."
  type        = map(string)
  default     = {}
}
