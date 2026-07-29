# ---------------------------------------------------------------------------
# One signing profile and one code-signing configuration for every platform Lambda
# (CKV_AWS_272). A single profile is deliberate: the trust boundary is "artefacts this
# platform built", not "artefacts this particular function built", and eleven profiles
# would be eleven key rotations to forget.
#
# `untrusted_artifact_on_deployment` defaults to Warn, not Enforce. `make lambda-deploy`
# uploads an unsigned zip built locally, so Enforce would reject every deployment this
# repo is able to perform. Warn attaches the configuration, records the violation, and
# lets the existing pipeline work — flip to Enforce once the build signs its artefact.
# ---------------------------------------------------------------------------

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
  # Signer requires the only platform AWS publishes for Lambda zip artefacts.
  platform_id = "AWSLambda-SHA384-ECDSA"
  name_prefix = "edl_${var.environment}_"

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
