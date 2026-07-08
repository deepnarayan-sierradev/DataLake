terraform {
  required_version = ">= 1.8, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# Remote state — S3 bucket and DynamoDB lock table must be bootstrapped manually once
# before the first terraform init.
#
# Bucket name embeds the dev account ID (087972550871) rather than the literal
# word "dev": S3 bucket names are unique across all of AWS, not just this
# account, so environment-as-account-ID is what actually guarantees no
# collision with staging/prod — the word "dev" would not.
terraform {
  backend "s3" {
    bucket         = "edl-terraform-state-087972550871"
    key            = "environments/dev/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    kms_key_id     = "alias/EdlTerraformState" # Created during bootstrap
    dynamodb_table = "EdlTerraformStateLock"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "enterprise-data-lake"
      Environment = "dev"
      ManagedBy   = "terraform"
      CostCenter  = var.cost_center
    }
  }
}
