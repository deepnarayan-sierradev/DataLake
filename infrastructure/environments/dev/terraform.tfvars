# dev environment — non-sensitive variable values
# Sensitive values (secrets, credentials) are NEVER stored here.
# Pass sensitive values via environment variables: TF_VAR_variable_name

aws_region  = "us-east-1"
cost_center = "engineering"
github_org  = "your-github-org" # Update to actual org before first deployment
github_repo = "enterprise-data-lake"
alert_email = "" # Set to ops team email when ready

# Lambda deployment package — produced by 'make lambda-package && make lambda-upload'
lambda_package_s3_bucket   = "edl-terraform-state-087972550871"
lambda_package_s3_key      = "lambda/extraction-pipeline.zip"
lambda_package_source_hash = "DzNW3lgbfXlocSAAXNvwOWDa2Ih1F4sR7/ywU78xpSs="

# Pipeline stage Lambda ARNs (required by Step Functions orchestration module)
# Populate after deploying each Lambda stage package.
extraction_pipeline_lambda_arn = "arn:aws:lambda:us-east-1:087972550871:function:EdlExtractionPipeline"
# serving_store_loader_lambda_arn    = "arn:aws:lambda:us-east-1:123456789012:function:EdlServingStoreLoader"
