# dev environment — non-sensitive variable values
# Sensitive values (secrets, credentials) are NEVER stored here.
# Pass sensitive values via environment variables: TF_VAR_variable_name

aws_region  = "us-east-1"
cost_center = "engineering"
github_org  = "your-github-org"    # Update to actual org before first deployment
github_repo = "enterprise-data-lake"
alert_email = ""                   # Set to ops team email when ready

# Lambda deployment package — produced by 'make lambda-package && make lambda-upload'
# lambda_package_s3_bucket   = "dev-edl-terraform-state"  # Or a dedicated artifacts bucket
# lambda_package_s3_key      = "lambda/extraction-pipeline.zip"
# lambda_package_source_hash = ""   # Fill in after running make lambda-package

# Pipeline stage Lambda ARNs (required by Step Functions orchestration module)
# Populate after deploying each Lambda stage package.
# extraction_pipeline_lambda_arn     = "arn:aws:lambda:us-east-1:123456789012:function:dev-extraction-pipeline"
# transformation_pipeline_lambda_arn = "arn:aws:lambda:us-east-1:123456789012:function:dev-transformation-pipeline"
# entity_resolution_lambda_arn       = "arn:aws:lambda:us-east-1:123456789012:function:dev-entity-resolution-pipeline"
# analytics_publisher_lambda_arn     = "arn:aws:lambda:us-east-1:123456789012:function:dev-analytics-layer-publisher"
# serving_store_loader_lambda_arn    = "arn:aws:lambda:us-east-1:123456789012:function:dev-serving-store-loader"
