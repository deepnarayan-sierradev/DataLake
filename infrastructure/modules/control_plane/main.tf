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

  control_plane_lambda_name = "${var.name_prefix}-control-plane-${var.environment}"

  routes = {
    list_entities      = "GET /tenants/{tenant_code}/entities"
    create_entity      = "POST /tenants/{tenant_code}/entities"
    trigger_pipeline   = "POST /tenants/{tenant_code}/pipelines/trigger"
    get_run            = "GET /tenants/{tenant_code}/runs/{run_id}"
    list_runs          = "GET /tenants/{tenant_code}/runs"
    list_twins         = "GET /tenants/{tenant_code}/twins/{entity_type}"
    get_twin           = "GET /tenants/{tenant_code}/twins/{entity_type}/{golden_id}"
    run_semantic_query = "POST /tenants/{tenant_code}/semantic/query"
    list_saved_queries = "GET /tenants/{tenant_code}/saved-queries"
    create_saved_query = "POST /tenants/{tenant_code}/saved-queries"
    get_saved_query    = "GET /tenants/{tenant_code}/saved-queries/{query_id}"
    run_saved_query    = "POST /tenants/{tenant_code}/saved-queries/{query_id}/run"
  }
}


resource "aws_cognito_user_pool" "control_plane" {
  name = "${var.name_prefix}-control-plane-users-${var.environment}"

  password_policy {
    minimum_length    = var.cognito_password_minimum_length
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = true
  }

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
    Name = "${var.name_prefix}-control-plane-users-${var.environment}"
  })
}

resource "aws_cognito_user_pool_client" "control_plane" {
  name         = "${var.name_prefix}-control-plane-client-${var.environment}"
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


resource "aws_apigatewayv2_api" "control_plane" {
  name          = "${var.name_prefix}-control-plane-api-${var.environment}"
  protocol_type = "HTTP"
  description   = "Multi-tenant control-plane API: tenant provisioning, entity registration, pipeline triggering, run status."

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-control-plane-api-${var.environment}"
  })
}

resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.control_plane.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "${var.name_prefix}-control-plane-authorizer-${var.environment}"

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
  name              = "/${var.name_prefix}/control-plane-api-access-${var.environment}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn

  tags = merge(local.common_tags, {
    Name = "/${var.name_prefix}/control-plane-api-access-${var.environment}"
  })
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.control_plane.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gw_access_logs.arn
    format = jsonencode({
      requestId        = "$context.requestId"
      ip               = "$context.identity.sourceIp"
      requestTime      = "$context.requestTime"
      httpMethod       = "$context.httpMethod"
      routeKey         = "$context.routeKey"
      status           = "$context.status"
      protocol         = "$context.protocol"
      responseLength   = "$context.responseLength"
      integrationError = "$context.integrationErrorMessage"
    })
  }

  tags = merge(local.common_tags, {
    Name = "${var.name_prefix}-control-plane-api-default-stage-${var.environment}"
  })
}

resource "aws_lambda_permission" "apigw_invoke" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.control_plane.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.control_plane.execution_arn}/*/*"
}


locals {
  required_resource_names = [
    "BACKFILL_JOB_TABLE",
    "BRAND_REGISTRY_TABLE",
    "CONFIG_GOVERNANCE_TABLE",
    "CONFIG_RESTATEMENT_TABLE",
    "DATA_QUALITY_EXCEPTION_TABLE",
    "EFFECTIVE_CONFIG_TABLE",
    "RECONCILIATION_REPORT_TABLE",
    "RESOURCE_NAME_PREFIX",
    "SCOPE_UNIT_TABLE",
    "SECRET_PATH_PREFIX",
    "SEMANTIC_APPROVAL_TABLE",
    "SERVING_STORE_CONFIG_TABLE",
    "SOURCE_CONNECTION_TABLE",
    "SOURCE_ONBOARDING_TABLE",
  ]
  resource_name_variables = {
    for key in local.required_resource_names : key => var.resource_names[key]
  }
}

resource "aws_lambda_function" "control_plane" {
  function_name = local.control_plane_lambda_name
  description   = "Multi-tenant control-plane API: tenant provisioning, entity registration, pipeline triggering, run status."

  s3_bucket        = var.lambda_package_s3_bucket
  s3_key           = var.lambda_package_s3_key
  source_code_hash = var.lambda_package_source_hash

  handler                 = "connector_runtime.api.control_plane_handler.lambda_handler"
  runtime                 = "python3.13"
  code_signing_config_arn = var.code_signing_config_arn

  dead_letter_config {
    target_arn = aws_sqs_queue.async_dlq.arn
  }


  dynamic "vpc_config" {

    for_each = var.vpc_id == null ? [] : [1]

    content {

      subnet_ids = var.subnet_ids

      security_group_ids = concat(var.security_group_ids, aws_security_group.control_plane_lambda[*].id)

    }

  }
  timeout     = 29
  memory_size = 512

  reserved_concurrent_executions = var.reserved_concurrent_executions

  role = var.control_plane_role_arn

  environment {
    variables = merge({
      PLATFORM_ENVIRONMENT       = var.environment
      PIPELINE_TRIGGER_QUEUE_URL = var.pipeline_trigger_queue_url
      ENTITY_CONFIG_TABLE        = var.entity_config_table_name
      ENTITY_TYPE_REGISTRY_TABLE = var.entity_type_registry_table_name
      AUDIT_LOG_TABLE            = var.run_audit_log_table_name
      ANALYTICS_S3_BUCKET        = var.analytics_s3_bucket_name
      TWIN_INDEX_TABLE           = var.twin_index_table_name
      SEMANTIC_MODEL_TABLE       = var.semantic_model_table_name
      SAVED_QUERY_TABLE          = var.saved_query_table_name
    }, local.resource_name_variables)
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


resource "aws_sqs_queue" "async_dlq" {
  name                      = "${var.name_prefix}-control-plane-async-dlq-${var.environment}"
  message_retention_seconds = 1209600 # 14 days, the maximum — a DLQ that expires loses the evidence
  sqs_managed_sse_enabled   = true

  tags = merge(local.common_tags, { Name = "${var.name_prefix}-control-plane-async-dlq-${var.environment}" })
}


resource "aws_security_group" "control_plane_lambda" {
  #checkov:skip=CKV2_AWS_5:Attached via dynamic vpc_config in this module.
  count = var.vpc_id == null ? 0 : 1

  name        = "${var.name_prefix}-control-plane-${var.environment}-sg"
  description = "HTTPS egress only for the ControlPlane Lambda function(s)."
  vpc_id      = var.vpc_id

  egress {
    description = "HTTPS egress to AWS service endpoints."
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, { Name = "${var.name_prefix}-control-plane-${var.environment}-sg" })

  lifecycle {
    create_before_destroy = true
  }
}
