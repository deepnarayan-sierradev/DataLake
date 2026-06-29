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
  description = "S3 bucket that holds the extraction pipeline Lambda deployment zip."
  default     = "dev-edl-terraform-state"
}

variable "lambda_package_s3_key" {
  type        = string
  description = "S3 key of the Lambda deployment zip (e.g. 'lambda/extraction-pipeline-v1.0.0.zip')."
  default     = "lambda/extraction-pipeline.zip"
}

variable "lambda_package_source_hash" {
  type        = string
  description = "Base64 SHA-256 of the Lambda zip. Run 'make lambda-package' to obtain this value."
  default     = ""
}

# ---------------------------------------------------------------------------
# Pipeline Lambda ARNs
# Passed in from CI/CD after Lambda packages are deployed. These are not
# computed by Terraform because Lambda packages are deployed separately from
# infrastructure; the ARNs are stable once Lambdas are first created.
# ---------------------------------------------------------------------------

variable "extraction_pipeline_lambda_arn" {
  type        = string
  description = "ARN of the deployed extraction pipeline Lambda function."
  default     = ""
}

variable "entity_resolution_lambda_arn" {
  type        = string
  description = "ARN of the deployed entity resolution Lambda function."
  default     = ""
}

variable "analytics_publisher_lambda_arn" {
  type        = string
  description = "ARN of the deployed analytics layer publisher Lambda function."
  default     = ""
}

variable "serving_store_loader_lambda_arn" {
  type        = string
  description = "ARN of the deployed serving store loader Lambda function."
  default     = ""
}
