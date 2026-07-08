terraform {
  required_version = ">= 1.8, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# Bucket name embeds the prod account ID rather than the literal word "prod":
# S3 bucket names are unique across all of AWS, not just this account, so
# environment-as-account-ID is what actually guarantees no collision with
# dev/staging — the word "prod" would not.
# PROD_ACCOUNT_ID is a placeholder — replace with the real 12-digit AWS
# account ID once the prod account exists and this bucket is bootstrapped
# (prod is not provisioned yet — see docs/PLATFORM_STATUS.md).
terraform {
  backend "s3" {
    bucket         = "edl-terraform-state-PROD_ACCOUNT_ID"
    key            = "environments/prod/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    kms_key_id     = "alias/EdlTerraformState"
    dynamodb_table = "EdlTerraformStateLock"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "enterprise-data-lake"
      Environment = "prod"
      ManagedBy   = "terraform"
      CostCenter  = var.cost_center
    }
  }
}
