# ---------------------------------------------------------------------------
# Execution roles for the four functions added in S8/S9.
#
# One role per function, never a shared one. The reason is concrete rather than doctrinal:
#
#   - the write-back role reads the `-writeback` secret suffix ONLY, so a compromised ingestion
#     path cannot mutate a source system (DL-CONN-02);
#   - the portability role is the only role in the platform with bulk `s3:DeleteObject`, so
#     nothing on the pipeline can delete a tenant's data even by defect;
#   - the webhook role can enqueue but not read tenant data at all, because the endpoint it
#     serves is unauthenticated by necessity (the provider signature is the credential).
#
# Sharing one role across the four would grant every one of those to all of them.
# ---------------------------------------------------------------------------

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

# ─── Webhook receiver ────────────────────────────────────────────────────────

resource "aws_iam_role" "webhook_receiver" {
  name               = "EdlWebhookReceiverRole"
  assume_role_policy = data.aws_iam_policy_document.platform_lambda_assume_role.json
  description        = "Webhook receiver: verify signature, dedupe, enqueue. No tenant data access."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "webhook_receiver_permissions" {

  # Every stage now enqueues its own failures (gap item 20), so each producing role needs
  # SendMessage on the per-stage queues. Scoped by name prefix, never `Resource = "*"`.
  statement {
    sid       = "SendToStageDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = ["arn:aws:sqs:${local.region}:${local.account_id}:EdlStageDlq-*"]
  }
  statement {
    sid       = "WriteOwnLogs"
    effect    = "Allow"
    actions   = local.platform_lambda_log_actions
    resources = ["arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlWebhookReceiver:*"]
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
    resources = ["arn:aws:sqs:${local.region}:${local.account_id}:EdlWebhookIngest.fifo"]
  }

  statement {
    sid    = "DeduplicateProviderEvents"
    effect = "Allow"
    # PutItem only: the receiver's conditional write is the dedupe. It has no read need, and
    # GetItem would let a compromised receiver enumerate which events a tenant has received.
    actions   = ["dynamodb:PutItem"]
    resources = ["arn:aws:dynamodb:${local.region}:${local.account_id}:table/EdlWebhookEventDedup"]
  }

  statement {
    sid    = "ReadWebhookSigningSecretOnly"
    effect = "Allow"
    # Scoped to the `webhook-secret` suffix: the receiver must not be able to read the
    # extraction or write-back credentials for the same connection.
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = ["arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:edl/tenants/*/connections/*/webhook-secret-*"]
  }
}

resource "aws_iam_role_policy" "webhook_receiver" {
  name   = "EdlWebhookReceiverPermissions"
  role   = aws_iam_role.webhook_receiver.id
  policy = data.aws_iam_policy_document.webhook_receiver_permissions.json
}

# ─── Write-back ──────────────────────────────────────────────────────────────

resource "aws_iam_role" "writeback" {
  name               = "EdlConnectorWritebackRole"
  assume_role_policy = data.aws_iam_policy_document.platform_lambda_assume_role.json
  description        = "Write-back to a source system. Reads the -writeback secret suffix only."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "writeback_permissions" {

  # Every stage now enqueues its own failures (gap item 20), so each producing role needs
  # SendMessage on the per-stage queues. Scoped by name prefix, never `Resource = "*"`.
  statement {
    sid       = "SendToStageDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = ["arn:aws:sqs:${local.region}:${local.account_id}:EdlStageDlq-*"]
  }
  statement {
    sid       = "WriteOwnLogs"
    effect    = "Allow"
    actions   = local.platform_lambda_log_actions
    resources = ["arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlConnectorWriteback:*"]
  }

  statement {
    sid       = "Trace"
    effect    = "Allow"
    actions   = local.platform_lambda_trace_actions
    resources = ["*"]
  }

  statement {
    sid    = "ReadWritebackCredentialOnly"
    effect = "Allow"
    # The whole point of the separate secret: a read-only deployment cannot mutate a source, and
    # this role cannot read the read-path credential either.
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = ["arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:edl/tenants/*/connections/*/credentials-writeback-*"]
  }

  statement {
    sid       = "ReadEntityConfigForOptInFlag"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem"]
    resources = ["arn:aws:dynamodb:${local.region}:${local.account_id}:table/EdlEntityExtractionConfig"]
  }

  statement {
    sid       = "AuditTheWrite"
    effect    = "Allow"
    actions   = ["dynamodb:PutItem"]
    resources = ["arn:aws:dynamodb:${local.region}:${local.account_id}:table/EdlRunAuditLog"]
  }
}

resource "aws_iam_role_policy" "writeback" {
  name   = "EdlConnectorWritebackPermissions"
  role   = aws_iam_role.writeback.id
  policy = data.aws_iam_policy_document.writeback_permissions.json
}

# ─── Workflow runner ─────────────────────────────────────────────────────────

resource "aws_iam_role" "workflow_runner" {
  name               = "EdlWorkflowRunnerRole"
  assume_role_policy = data.aws_iam_policy_document.platform_lambda_assume_role.json
  description        = "Scheduled workflow evaluation and action execution."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "workflow_runner_permissions" {

  # Every stage now enqueues its own failures (gap item 20), so each producing role needs
  # SendMessage on the per-stage queues. Scoped by name prefix, never `Resource = "*"`.
  statement {
    sid       = "SendToStageDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = ["arn:aws:sqs:${local.region}:${local.account_id}:EdlStageDlq-*"]
  }
  statement {
    sid       = "WriteOwnLogs"
    effect    = "Allow"
    actions   = local.platform_lambda_log_actions
    resources = ["arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlWorkflowRunner:*"]
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
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/EdlWorkflowDefinition",
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/EdlWorkflowExecution",
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/EdlWorkflowIdempotency",
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/EdlWorkflowTask",
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/EdlWorkflowDestination",
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/EdlDataQualityException",
    ]
  }

  statement {
    sid    = "PublishNotifications"
    effect = "Allow"
    # Restricted to the platform's own topics: a workflow must not be able to publish to an
    # arbitrary topic in the account even if a definition names one.
    actions   = ["sns:Publish"]
    resources = ["arn:aws:sns:${local.region}:${local.account_id}:edl-*"]
  }

  statement {
    sid       = "RequestReportDistribution"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = ["arn:aws:sqs:${local.region}:${local.account_id}:EdlReportDistribution"]
  }

  statement {
    sid       = "InvokeWritebackAndPipeline"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = ["arn:aws:lambda:${local.region}:${local.account_id}:function:EdlConnectorWriteback"]
  }

  statement {
    sid       = "StartPipelineRun"
    effect    = "Allow"
    actions   = ["states:StartExecution"]
    resources = ["arn:aws:states:${local.region}:${local.account_id}:stateMachine:EdlExtractionPipeline"]
  }

  statement {
    sid    = "ReadOutboundDestinationSecrets"
    effect = "Allow"
    # Signing secrets for allowlisted webhook destinations only.
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:edl/tenants/*/workflow-destinations/*"]
  }
}

resource "aws_iam_role_policy" "workflow_runner" {
  name   = "EdlWorkflowRunnerPermissions"
  role   = aws_iam_role.workflow_runner.id
  policy = data.aws_iam_policy_document.workflow_runner_permissions.json
}

# ─── Portability ─────────────────────────────────────────────────────────────

resource "aws_iam_role" "portability" {
  name               = "EdlPortabilityRole"
  assume_role_policy = data.aws_iam_policy_document.platform_lambda_assume_role.json
  description        = "Tenant export and deletion. The only role with bulk object deletion."
  tags               = local.common_tags
}

data "aws_iam_policy_document" "portability_permissions" {

  # Async invocation failures land on this function's own DLQ (CKV_AWS_116). A DLQ the role
  # cannot write to is inert, which is the failure mode this repo keeps finding.
  statement {
    sid       = "AsyncInvocationDlq"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = ["arn:aws:sqs:${local.region}:${local.account_id}:EdlStageDlq-*"]
  }
  statement {
    sid       = "WriteOwnLogs"
    effect    = "Allow"
    actions   = local.platform_lambda_log_actions
    resources = ["arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/lambda/EdlPortability:*"]
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
    # Bulk delete lives here and nowhere else in the platform. The deletion saga verifies each
    # prefix is empty afterwards by re-listing, so a partial delete cannot be certified complete.
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
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/EdlExportJob",
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/EdlDeletionCertificate",
    ]
  }
}

resource "aws_iam_role_policy" "portability" {
  name   = "EdlPortabilityPermissions"
  role   = aws_iam_role.portability.id
  policy = data.aws_iam_policy_document.portability_permissions.json
}
