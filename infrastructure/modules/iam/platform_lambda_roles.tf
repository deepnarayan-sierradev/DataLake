
data "aws_iam_policy_document" "platform_lambda_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

locals {
  platform_lambda_log_actions = [
    "logs:CreateLogStream",
    "logs:PutLogEvents",
  ]
  platform_lambda_trace_actions = [
    "xray:PutTraceSegments",
    "xray:PutTelemetryRecords",
  ]
}


resource "aws_iam_role" "webhook_receiver" {
  name               = "${var.name_prefix}-webhook-receiver-${var.environment}-exec"
  assume_role_policy = data.aws_iam_policy_document.platform_lambda_assume_role.json
  description        = "Webhook receiver: verify signature, dedupe, enqueue. No tenant data access."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "webhook_receiver_permissions" {

  statement {
    sid       = "SendToStageDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = ["arn:aws:sqs:${local.region}:${local.account_id}:${var.name_prefix}-*-dlq-${var.environment}"]
  }
  statement {
    sid       = "WriteOwnLogs"
    effect    = "Allow"
    actions   = local.platform_lambda_log_actions
    resources = ["arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-webhook-receiver-${var.environment}:*"]
  }

  statement {
    sid       = "Trace"
    effect    = "Allow"
    actions   = local.platform_lambda_trace_actions
    resources = ["*"]
  }

  statement {
    sid       = "EnqueueVerifiedEvents"
    effect    = "Allow"
    actions   = ["sqs:SendMessage", "sqs:GetQueueAttributes"]
    resources = ["arn:aws:sqs:${local.region}:${local.account_id}:${var.name_prefix}-webhook-ingest-${var.environment}.fifo"]
  }

  statement {
    sid       = "DeduplicateProviderEvents"
    effect    = "Allow"
    actions   = ["dynamodb:PutItem"]
    resources = ["arn:aws:dynamodb:${local.region}:${local.account_id}:table/${var.name_prefix}-webhook-event-dedup-${var.environment}"]
  }

  statement {
    sid       = "ReadWebhookSigningSecretOnly"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = ["arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:datalake/<env>/tenants/*/connections/*/webhook-secret-*"]
  }
}

resource "aws_iam_role_policy" "webhook_receiver" {
  name   = "${var.name_prefix}-webhook-receiver-${var.environment}-exec-policy"
  role   = aws_iam_role.webhook_receiver.id
  policy = data.aws_iam_policy_document.webhook_receiver_permissions.json
}


resource "aws_iam_role" "writeback" {
  name               = "${var.name_prefix}-connector-writeback-${var.environment}-exec"
  assume_role_policy = data.aws_iam_policy_document.platform_lambda_assume_role.json
  description        = "Write-back to a source system. Reads the -writeback secret suffix only."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "writeback_permissions" {

  statement {
    sid       = "SendToStageDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = ["arn:aws:sqs:${local.region}:${local.account_id}:${var.name_prefix}-*-dlq-${var.environment}"]
  }
  statement {
    sid       = "WriteOwnLogs"
    effect    = "Allow"
    actions   = local.platform_lambda_log_actions
    resources = ["arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-connector-writeback-${var.environment}:*"]
  }

  statement {
    sid       = "Trace"
    effect    = "Allow"
    actions   = local.platform_lambda_trace_actions
    resources = ["*"]
  }

  statement {
    sid       = "ReadWritebackCredentialOnly"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = ["arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:datalake/<env>/tenants/*/connections/*/credentials-writeback-*"]
  }

  statement {
    sid       = "ReadEntityConfigForOptInFlag"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem"]
    resources = ["arn:aws:dynamodb:${local.region}:${local.account_id}:table/${var.name_prefix}-entity-extraction-config-${var.environment}"]
  }

  statement {
    sid       = "AuditTheWrite"
    effect    = "Allow"
    actions   = ["dynamodb:PutItem"]
    resources = ["arn:aws:dynamodb:${local.region}:${local.account_id}:table/${var.name_prefix}-run-audit-log-${var.environment}"]
  }
}

resource "aws_iam_role_policy" "writeback" {
  name   = "${var.name_prefix}-connector-writeback-${var.environment}-exec-policy"
  role   = aws_iam_role.writeback.id
  policy = data.aws_iam_policy_document.writeback_permissions.json
}


resource "aws_iam_role" "workflow_runner" {
  name               = "${var.name_prefix}-workflow-runner-${var.environment}-exec"
  assume_role_policy = data.aws_iam_policy_document.platform_lambda_assume_role.json
  description        = "Scheduled workflow evaluation and action execution."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "workflow_runner_permissions" {

  statement {
    sid       = "SendToStageDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = ["arn:aws:sqs:${local.region}:${local.account_id}:${var.name_prefix}-*-dlq-${var.environment}"]
  }
  statement {
    sid       = "WriteOwnLogs"
    effect    = "Allow"
    actions   = local.platform_lambda_log_actions
    resources = ["arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-workflow-runner-${var.environment}:*"]
  }

  statement {
    sid       = "Trace"
    effect    = "Allow"
    actions   = local.platform_lambda_trace_actions
    resources = ["*"]
  }

  statement {
    sid    = "ReadDefinitionsWriteExecutions"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
    ]
    resources = [
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/${var.name_prefix}-workflow-definitions-${var.environment}",
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/${var.name_prefix}-workflow-executions-${var.environment}",
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/${var.name_prefix}-workflow-idempotency-${var.environment}",
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/${var.name_prefix}-workflow-tasks-${var.environment}",
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/${var.name_prefix}-workflow-destinations-${var.environment}",
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/${var.name_prefix}-data-quality-exceptions-${var.environment}",
    ]
  }

  statement {
    sid       = "PublishNotifications"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = ["arn:aws:sns:${local.region}:${local.account_id}:${var.name_prefix}-*"]
  }

  statement {
    sid       = "RequestReportDistribution"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = ["arn:aws:sqs:${local.region}:${local.account_id}:${var.name_prefix}-report-distribution-${var.environment}"]
  }

  statement {
    sid       = "InvokeWritebackAndPipeline"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = ["arn:aws:lambda:${local.region}:${local.account_id}:function:${var.name_prefix}-connector-writeback-${var.environment}"]
  }

  statement {
    sid       = "StartPipelineRun"
    effect    = "Allow"
    actions   = ["states:StartExecution"]
    resources = ["arn:aws:states:${local.region}:${local.account_id}:stateMachine:${var.name_prefix}-extraction-workflow-${var.environment}"]
  }

  statement {
    sid       = "ReadOutboundDestinationSecrets"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:datalake/<env>/tenants/*/workflow-destinations/*"]
  }
}

resource "aws_iam_role_policy" "workflow_runner" {
  name   = "${var.name_prefix}-workflow-runner-${var.environment}-exec-policy"
  role   = aws_iam_role.workflow_runner.id
  policy = data.aws_iam_policy_document.workflow_runner_permissions.json
}


resource "aws_iam_role" "portability" {
  name               = "${var.name_prefix}-portability-${var.environment}-exec"
  assume_role_policy = data.aws_iam_policy_document.platform_lambda_assume_role.json
  description        = "Tenant export and deletion. The only role with bulk object deletion."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "portability_permissions" {

  statement {
    sid       = "AsyncInvocationDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = ["arn:aws:sqs:${local.region}:${local.account_id}:${var.name_prefix}-*-dlq-${var.environment}"]
  }
  statement {
    sid       = "WriteOwnLogs"
    effect    = "Allow"
    actions   = local.platform_lambda_log_actions
    resources = ["arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/${var.name_prefix}-portability-${var.environment}:*"]
  }

  statement {
    sid       = "Trace"
    effect    = "Allow"
    actions   = local.platform_lambda_trace_actions
    resources = ["*"]
  }

  statement {
    sid    = "ReadEveryDataLayerForExport"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:ListBucket",
    ]
    resources = concat(
      [for bucket in var.portability_data_bucket_arns : bucket],
      [for bucket in var.portability_data_bucket_arns : "${bucket}/*"],
    )
  }

  statement {
    sid    = "DeleteTenantDataOnCertifiedDeletion"
    effect = "Allow"
    actions = [
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
    ]
    resources = [for bucket in var.portability_data_bucket_arns : "${bucket}/*"]
  }

  statement {
    sid       = "WriteExportArtefact"
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${var.export_artefact_bucket_arn}/*"]
  }

  statement {
    sid    = "RecordJobsAndCertificates"
    effect = "Allow"
    actions = [
      "dynamodb:PutItem",
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:UpdateItem",
    ]
    resources = [
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/${var.name_prefix}-export-jobs-${var.environment}",
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/${var.name_prefix}-deletion-certificates-${var.environment}",
    ]
  }
}

resource "aws_iam_role_policy" "portability" {
  name   = "${var.name_prefix}-portability-${var.environment}-exec-policy"
  role   = aws_iam_role.portability.id
  policy = data.aws_iam_policy_document.portability_permissions.json
}
