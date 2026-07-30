variable "aws_region" {
  type        = string
  description = "AWS region for the dev environment."
  default     = "us-east-1"
}

variable "cost_center" {
  type        = string
  description = "Cost center tag value for all resources."
  default     = "engineering"
}

variable "alert_email" {
  type        = string
  description = "Email address for CloudWatch alarm SNS notifications. Leave empty to skip."
  default     = ""
}

variable "replay_operator_role_arns" {
  type        = list(string)
  description = "IAM role ARNs permitted to read and process the extraction failure DLQ."
  default     = []
}

variable "github_org" {
  type        = string
  description = "GitHub organisation name for CI/CD OIDC trust policy."
  default     = "your-github-org"
}

variable "github_repo" {
  type        = string
  description = "GitHub repository name for CI/CD OIDC trust policy."
  default     = "enterprise-data-lake"
}

variable "cicd_deployment_policy_arns" {
  type        = list(string)
  description = "IAM managed policy ARNs to attach to the CI/CD deployment role."
  default     = []
}


variable "lambda_package_s3_bucket" {
  type        = string
  description = "S3 bucket that holds the Lambda deployment artefacts (separate from Terraform state)."
  default     = "datalake-lambda-artefacts-uat-use1"
}

variable "lambda_package_s3_key" {
  type        = string
  description = "S3 key of the Lambda deployment zip."
  default     = "lambda/extraction-pipeline.zip"
}

variable "lambda_package_source_hash" {
  type        = string
  description = "Base64-encoded SHA-256 hash of the Lambda zip for change detection."
  default     = ""
}

variable "extraction_pipeline_lambda_arn" {
  type        = string
  description = "ARN of the deployed extraction pipeline Lambda function."
  default     = ""
}

variable "analytics_reader_principals" {
  type        = list(string)
  description = "IAM principal ARNs granted Lake Formation SELECT+DESCRIBE on curated/analytics tables (see infrastructure/modules/glue/variables.tf)."
  default     = []
}


variable "waf_enforcement_mode" {
  description = <<-EOT
    Control-plane WAF mode: "audit" counts what would be blocked, "enforce" blocks.

    Defaults to audit in every environment. DL-SEC-13 requires alarming before blocking, so
    promoting to enforce is a deliberate, reviewed change per environment — not a default.
  EOT
  type        = string
  default     = "audit"
}

variable "client_vpn_enabled" {
  description = <<-EOT
    Whether to provision the Client VPN endpoint for BI access (DL-SERV-01).

    False until the customer decides on VPN topology. While false, gap register item 4 (no
    network path for any BI tool) remains open and the serving store stays unreachable.
  EOT
  type        = bool
  default     = false
}

variable "private_subnet_cidrs" {
  description = "CIDRs of the private subnets, for Client VPN routes and client SG egress."
  type        = list(string)
  default     = []
}

variable "vpn_server_certificate_arn" {
  description = "ACM ARN of the Client VPN server certificate."
  type        = string
  default     = ""
}

variable "vpn_client_root_certificate_arn" {
  description = "ACM ARN of the client root certificate chain per-tenant certs are issued from."
  type        = string
  default     = ""
}

variable "vpn_tenant_access_groups" {
  description = <<-EOT
    Map of VPN access-group id to the CIDR that group may reach.

    Per-tenant by construction: one issued certificate must not be sufficient to reach the
    whole VPC.
  EOT
  type        = map(string)
  default     = {}
}

variable "lake_formation_tenant_codes" {
  description = "Tenant codes forming the `tenant` LF-Tag value set (DL-SERV-07)."
  type        = list(string)
  default     = ["demo"]
}

variable "lake_formation_tenant_scoped_principals" {
  description = <<-EOT
    Principals granted tag-scoped Athena read access, replacing the wildcard grant.

    Empty means no principal can query through Lake Formation — which is the correct
    fail-closed default, not an oversight.
  EOT
  type = map(object({
    principal_arn = string
    tenant_code   = string
    department    = string
  }))
  default = {}
}

variable "lake_formation_admin_arns" {
  description = <<-EOT
    Lake Formation data-lake administrators. Keep short: an admin bypasses every tag grant.
    Empty skips managing the data-lake settings resource entirely.
  EOT
  type        = list(string)
  default     = []
}

variable "replica_region" {
  description = "Region holding the cross-region S3 replicas. Must differ from aws_region."
  type        = string
  default     = "us-west-2"

  validation {
    condition     = var.replica_region != ""
    error_message = "replica_region must be set; cross-region replication needs a second region."
  }
}
