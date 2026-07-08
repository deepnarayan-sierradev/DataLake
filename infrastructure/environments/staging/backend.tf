terraform {
  required_version = ">= 1.8, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# Bucket name embeds the staging account ID rather than the literal word
# "staging": S3 bucket names are unique across all of AWS, not just this
# account, so environment-as-account-ID is what actually guarantees no
# collision with dev/prod — the word "staging" would not.
# STAGING_ACCOUNT_ID is a placeholder — replace with the real 12-digit AWS
# account ID once the staging account exists and this bucket is bootstrapped
# (staging is not provisioned yet — see docs/PLATFORM_STATUS.md).
terraform {
  backend "s3" {
    bucket         = "edl-terraform-state-STAGING_ACCOUNT_ID"
    key            = "environments/staging/terraform.tfstate"
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
      Environment = "staging"
      ManagedBy   = "terraform"
      CostCenter  = var.cost_center
    }
  }
}
