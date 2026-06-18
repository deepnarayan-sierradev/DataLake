variable "environment" {
  type        = string
  description = "Deployment environment: dev, staging, or prod."
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "vpc_cidr" {
  type        = string
  description = "CIDR block for the VPC (e.g. '10.0.0.0/16')."
  default     = "10.0.0.0/16"
}

variable "availability_zones" {
  type        = list(string)
  description = "List of AZs to use. Must match the count of private and public subnet CIDRs."
}

variable "private_subnet_cidrs" {
  type        = list(string)
  description = "CIDR blocks for private subnets — one per AZ. All data lake compute runs here."
}

variable "public_subnet_cidrs" {
  type        = list(string)
  description = "CIDR blocks for public subnets — one per AZ. NAT gateways only."
}

variable "single_nat_gateway" {
  type        = bool
  default     = true
  description = "Use a single NAT Gateway (cost-optimised for dev). Set false for HA in staging/prod."
}

variable "flow_log_retention_days" {
  type        = number
  default     = 90
  description = "CloudWatch log retention in days for VPC Flow Logs."
  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.flow_log_retention_days)
    error_message = "flow_log_retention_days must be a valid CloudWatch retention period."
  }
}

variable "flow_logs_kms_key_arn" {
  type        = string
  description = "KMS key ARN used to encrypt the VPC Flow Logs CloudWatch log group."
}

# ── VPC Interface endpoint flags (conditional for cost control) ──────────────

variable "enable_secrets_manager_endpoint" {
  type        = bool
  default     = true
  description = "Enable VPC Interface endpoint for AWS Secrets Manager."
}

variable "enable_cloudwatch_logs_endpoint" {
  type        = bool
  default     = true
  description = "Enable VPC Interface endpoint for CloudWatch Logs."
}

variable "enable_cloudwatch_monitoring_endpoint" {
  type        = bool
  default     = true
  description = "Enable VPC Interface endpoint for CloudWatch Monitoring (metrics)."
}

variable "enable_step_functions_endpoint" {
  type        = bool
  default     = true
  description = "Enable VPC Interface endpoint for AWS Step Functions."
}

variable "enable_glue_endpoint" {
  type        = bool
  default     = false
  description = "Enable VPC Interface endpoint for AWS Glue (required in prod; optional in dev)."
}

variable "enable_kms_endpoint" {
  type        = bool
  default     = true
  description = "Enable VPC Interface endpoint for AWS KMS."
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Additional resource tags merged with module-managed tags."
}
