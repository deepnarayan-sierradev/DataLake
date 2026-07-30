# dev environment — non-sensitive variable values
#
# DEV_ACCOUNT_ID is a placeholder: replace with the new 12-digit AWS account ID before the first
# apply. The analytics_reader_principals below are account-specific (an SSO role path and a
# SageMaker execution role) and must be re-pointed at principals that exist in the new account.
# Sensitive values (secrets, credentials) are NEVER stored here.
# Pass sensitive values via environment variables: TF_VAR_variable_name

aws_region  = "us-east-1"
cost_center = "engineering"
github_org  = "your-github-org" # Update to actual org before first deployment
github_repo = "enterprise-data-lake"
alert_email = "" # Set to ops team email when ready

# Lambda deployment package — produced by 'make lambda-package && make lambda-upload'
lambda_package_s3_bucket   = "datalake-lambda-artefacts-dev-use1"
lambda_package_s3_key      = "lambda/extraction-pipeline.zip"
lambda_package_source_hash = "2ZXnkXA7ya2J/b6hyILBjXNjjNtfmKqphKY7Fg450Aw="

# Pipeline stage Lambda ARNs (required by Step Functions orchestration module)
# Populate after deploying each Lambda stage package.
extraction_pipeline_lambda_arn = "arn:aws:lambda:us-east-1:DEV_ACCOUNT_ID:function:datalake-extraction-dev"

# Human/analyst principals needing to query Athena — see infrastructure/modules/glue/variables.tf
analytics_reader_principals = [
  "arn:aws:iam::DEV_ACCOUNT_ID:user/datalake-dev-user",
  "arn:aws:iam::DEV_ACCOUNT_ID:role/aws-reserved/sso.amazonaws.com/us-west-1/AWSReservedSSO_AdministratorAccess_590681e1faa45613",
  "arn:aws:iam::DEV_ACCOUNT_ID:role/service-role/AmazonSageMakerAdminIAMExecutionRole_1",
]
