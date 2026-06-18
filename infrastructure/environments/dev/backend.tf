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
# before the first terraform init. See docs/runbooks/terraform-state-bootstrap.md
terraform {
  backend "s3" {
    bucket         = "dev-edl-terraform-state"
    key            = "environments/dev/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    kms_key_id     = "alias/dev-terraform-state" # Created during bootstrap
    dynamodb_table = "dev-edl-terraform-state-lock"
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
