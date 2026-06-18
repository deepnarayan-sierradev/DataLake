terraform {
  required_version = ">= 1.8, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

terraform {
  backend "s3" {
    bucket         = "staging-edl-terraform-state"
    key            = "environments/staging/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    kms_key_id     = "alias/staging-terraform-state"
    dynamodb_table = "staging-edl-terraform-state-lock"
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
