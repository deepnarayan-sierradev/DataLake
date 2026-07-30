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
    bucket         = "datalake-terraform-state-uat-use1"
    key            = "environments/uat/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    kms_key_id     = "alias/datalake-terraform-state-uat"
    dynamodb_table = "datalake-terraform-state-lock-uat"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Application = "datalake"
      Project     = "datalake"
      Environment = "uat"
      ManagedBy   = "terraform"
      CostCenter  = var.cost_center
    }
  }
}

provider "aws" {
  alias  = "replica"
  region = var.replica_region

  default_tags {
    tags = {
      Application = "datalake"
      Project     = "datalake"
      Environment = "uat"
      ManagedBy   = "terraform"
      CostCenter  = var.cost_center
    }
  }
}
