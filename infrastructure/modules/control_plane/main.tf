terraform {
  required_version = ">= 1.8, < 2.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  common_tags = merge(var.tags, {
    Environment = var.environment
    ManagedBy   = "terraform"
    Module      = "control-plane"
  })

  control_plane_lambda_name = "${var.environment}-edl-control-plane"

  # Route table for the control-plane HTTP API. Each entry maps a stable
  # for_each key to an API Gateway v2 route_key ("METHOD /path"), matching
  # the endpoints implemented in connector_runtime/api/control_plane_handler.py.
  routes = {
    create_tenant    = "POST /tenants"
    list_entities    = "GET /tenants/{tenant_code}/entities"
    create_entity    = "POST /tenants/{tenant_code}/entities"
    trigger_pipeline = "POST /tenants/{tenant_code}/pipelines/trigger"
    get_run          = "GET /tenants/{tenant_code}/runs/{run_id}"
    list_runs        = "GET /tenants/{tenant_code}/runs"
  }
}

# ---------------------------------------------------------------------------
# Cognito User Pool — tenant/operator authentication
#
# A "tenant_code" custom attribute is carried on each user so the Lambda's
# fail-closed authorization check (connector_runtime/api/control_plane_handler
# ._authenticated_tenant_code) can cross-check the caller's tenant against
# the {tenant_code} path parameter on every tenant-scoped route.
# ---------------------------------------------------------------------------

resource "aws_cognito_user_pool" "control_plane" {
  name = "${var.environment}-edl-control-plane-users"

  password_policy {
    minimum_length    = var.cognito_password_minimum_length
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = true
  }

  # Custom attribute surfaced in the JWT as "custom:tenant_code" — read by
  # the control-plane Lambda to authorize the {tenant_code} path parameter.
  schema {
    name                = "tenant_code"
    attribute_data_type = "String"
    mutable             = true
    required            = false
    string_attribute_constraints {
      min_length = 2
      max_length = 48
    }
  }

  auto_verified_attributes = ["email"]

  tags = merge(local.common_tags, {
    Name = "${var.environment}-edl-control-plane-users"
  })
}

resource "aws_cognito_user_pool_client" "control_plane" {
  name         = "${var.environment}-edl-control-plane-client"
  user_pool_id = aws_cognito_user_pool.control_plane.id

  generate_secret = false
  explicit_auth_flows = [
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
    "ALLOW_USER_SRP_AUTH",
  ]

  access_token_validity  = 1
  id_token_validity      = 1
  refresh_token_validity = 30
  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "days"
  }
}

# ---------------------------------------------------------------------------
# HTTP API (API Gateway v2) — simpler/cheaper than a REST API and sufficient
# for a JSON proxy-integration control plane. payload_format_version = "1.0"
# is pinned on the Lambda integration below so the Lambda receives the
# classic {httpMethod, path, pathParameters, body, requestContext...} proxy
# event shape that connector_runtime/api/control_plane_handler.py parses.
#
# NOTE (honesty flag): AWS's documented location for JWT-authorizer claims
# on an HTTP API is requestContext.authorizer.jwt.claims, which may differ
# from the requestContext.authorizer.claims shape produced by a REST API +
# COGNITO_USER_POOLS authorizer. The Lambda's _extract_claims() checks BOTH
# locations defensively, but this authorizer wiring has not been exercised
# against a live deployment in this change — treat it as functionally wired
# but unverified end-to-end until a real login/token round-trip is tested.
# ---------------------------------------------------------------------------

resource "aws_apigatewayv2_api" "control_plane" {
  name          = "${var.environment}-edl-control-plane-api"
  protocol_type = "HTTP"
  description   = "Multi-tenant control-plane API: tenant provisioning, entity registration, pipeline triggering, run status."

  tags = merge(local.common_tags, {
    Name = "${var.environment}-edl-control-plane-api"
  })
}

resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.control_plane.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "${var.environment}-edl-control-plane-cognito-authorizer"

  jwt_configuration {
    audience = [aws_cognito_user_pool_client.control_plane.id]
    issuer   = "https://cognito-idp.${data.aws_region.current.name}.amazonaws.com/${aws_cognito_user_pool.control_plane.id}"
  }
}

resource "aws_apigatewayv2_integration" "control_plane_lambda" {
  api_id                 = aws_apigatewayv2_api.control_plane.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.control_plane.invoke_arn
  integration_method     = "POST"
  payload_format_version = "1.0"
}

resource "aws_apigatewayv2_route" "routes" {
  for_each = local.routes

  api_id             = aws_apigatewayv2_api.control_plane.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.control_plane_lambda.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_cloudwatch_log_group" "api_gw_access_logs" {
  name              = "/edl/${var.environment}/control-plane-api-gw-access-logs"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn

  tags = merge(local.common_tags, {
    Name = "/edl/${var.environment}/control-plane-api-gw-access-logs"
  })
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.control_plane.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gw_access_logs.arn
    format = jsonencode({
      requestId         = "$context.requestId"
      ip                = "$context.identity.sourceIp"
      requestTime       = "$context.requestTime"
      httpMethod        = "$context.httpMethod"
      routeKey          = "$context.routeKey"
      status            = "$context.status"
      protocol          = "$context.protocol"
      responseLength    = "$context.responseLength"
      integrationError  = "$context.integrationErrorMessage"
    })
  }

  tags = merge(local.common_tags, {
    Name = "${var.environment}-edl-control-plane-api-default-stage"
  })
}

resource "aws_lambda_permission" "apigw_invoke" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.control_plane.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.control_plane.execution_arn}/*/*"
}

# ---------------------------------------------------------------------------
# Control-Plane Lambda
# Ships in the same deployment package as the rest of connector_runtime
# (same pattern as transformation_lambda / entity_resolution_lambda reusing
# the extraction-pipeline.zip). IAM role is centralised in the iam module
# (aws_iam_role.control_plane) — not defined here.
# ---------------------------------------------------------------------------

resource "aws_lambda_function" "control_plane" {
  function_name = local.control_plane_lambda_name
  description   = "Multi-tenant control-plane API: tenant provisioning, entity registration, pipeline triggering, run status."

  s3_bucket        = var.lambda_package_s3_bucket
  s3_key           = var.lambda_package_s3_key
  source_code_hash = var.lambda_package_source_hash

  handler = "connector_runtime.api.control_plane_handler.lambda_handler"
  runtime = "python3.12"
  # 29s: just under the HTTP API integration's hard 30s timeout cap.
  timeout     = 29
  memory_size = 512

  role = var.control_plane_role_arn

  environment {
    variables = {
      PLATFORM_ENVIRONMENT       = var.environment
      PIPELINE_TRIGGER_QUEUE_URL = var.pipeline_trigger_queue_url
      ENTITY_CONFIG_TABLE        = var.entity_config_table_name
      ENTITY_TYPE_REGISTRY_TABLE = var.entity_type_registry_table_name
      AUDIT_LOG_TABLE            = var.run_audit_log_table_name
      AWS_REGION                 = data.aws_region.current.name
    }
  }

  kms_key_arn = var.kms_key_arn

  tracing_config {
    mode = var.enable_xray_tracing ? "Active" : "PassThrough"
  }

  tags = merge(local.common_tags, {
    Name = local.control_plane_lambda_name
  })
}

resource "aws_cloudwatch_log_group" "control_plane" {
  name              = "/aws/lambda/${local.control_plane_lambda_name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn

  tags = merge(local.common_tags, {
    Name = "/aws/lambda/${local.control_plane_lambda_name}"
  })
}
