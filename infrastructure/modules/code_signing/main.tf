
terraform {
  required_version = ">= 1.8, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

resource "aws_signer_signing_profile" "lambda" {
  platform_id = "AWSLambda-SHA384-ECDSA"
  name_prefix = "${replace(var.name_prefix, "-", "_")}_${var.environment}_"

  signature_validity_period {
    value = 135
    type  = "MONTHS"
  }

  tags = merge(var.tags, {
    Environment = var.environment
    ManagedBy   = "terraform"
    Module      = "code_signing"
  })
}

resource "aws_lambda_code_signing_config" "platform" {
  description = "Platform Lambda code signing (${var.environment})"

  allowed_publishers {
    signing_profile_version_arns = [aws_signer_signing_profile.lambda.version_arn]
  }

  policies {
    untrusted_artifact_on_deployment = var.untrusted_artifact_on_deployment
  }
}
