# ---------------------------------------------------------------------------
# Per-stage dead-letter queues and replay (DL-OPS-05, closing FR-F0.6).
#
# The gap being closed: there is one shared extraction-failure DLQ, so a transformation failure
# and an entity-resolution failure land in the same queue and a replay has to re-run the whole
# pipeline to retry either. Per-stage queues make a replay start at the stage that failed.
#
# Each queue has its own redrive target so a message that fails replay repeatedly stops being
# replayed rather than cycling forever.
# ---------------------------------------------------------------------------

locals {
  pipeline_stages = {
    extraction          = { visibility_timeout = 960 }
    transformation      = { visibility_timeout = 960 }
    entity_resolution   = { visibility_timeout = 960 }
    analytics_publish   = { visibility_timeout = 420 }
    serving_store_load  = { visibility_timeout = 960 }
    twin_build          = { visibility_timeout = 960 }
    webhook_ingest      = { visibility_timeout = 120 }
    writeback           = { visibility_timeout = 420 }
    workflow_action     = { visibility_timeout = 420 }
  }
}

# The terminal queue: a message that exhausts replay attempts lands here and is never
# automatically retried again. Without it, a poison message replays indefinitely.
resource "aws_sqs_queue" "stage_replay_exhausted" {
  name                              = "EdlStageReplayExhausted"
  message_retention_seconds         = 1209600
  kms_master_key_id                 = var.kms_key_arn
  kms_data_key_reuse_period_seconds = 300

  tags = merge(var.tags, {
    Name    = "EdlStageReplayExhausted"
    Purpose = "terminal-dlq"
  })
}

resource "aws_sqs_queue" "stage_dlq" {
  for_each = local.pipeline_stages

  name = "EdlStageDlq-${replace(title(replace(each.key, "_", " ")), " ", "")}"

  # 14 days: long enough for an operator to notice on Monday what failed on Friday.
  message_retention_seconds = 1209600

  # Must be at least the consuming Lambda's timeout, or CreateEventSourceMapping is rejected
  # (see infrastructure/CLAUDE.md).
  visibility_timeout_seconds = each.value.visibility_timeout

  kms_master_key_id                 = var.kms_key_arn
  kms_data_key_reuse_period_seconds = 300

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.stage_replay_exhausted.arn
    # Three replay attempts, then terminal. Idempotent replay is a property of every stage
    # (DL-OPS-09), so three attempts is safe; unbounded attempts are not.
    maxReceiveCount = 3
  })

  tags = merge(var.tags, {
    Name    = "EdlStageDlq-${each.key}"
    Stage   = each.key
    Purpose = "per-stage-dlq"
  })
}

resource "aws_sqs_queue_redrive_allow_policy" "stage_replay_exhausted" {
  queue_url = aws_sqs_queue.stage_replay_exhausted.id

  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [for queue in aws_sqs_queue.stage_dlq : queue.arn]
  })
}

# ---------------------------------------------------------------------------
# Webhook ingest queue (DL-CONN-14). FIFO with content-based dedup off: the receiver supplies
# an explicit MessageDeduplicationId derived from the provider event id, which is a stronger
# guarantee than a content hash (two genuinely distinct events can share a body).
# ---------------------------------------------------------------------------

resource "aws_sqs_queue" "webhook_ingest" {
  name                        = "EdlWebhookIngest.fifo"
  fifo_queue                  = true
  content_based_deduplication = false
  deduplication_scope         = "messageGroup"
  fifo_throughput_limit       = "perMessageGroupId"

  message_retention_seconds  = 345600
  visibility_timeout_seconds = 960

  kms_master_key_id                 = var.kms_key_arn
  kms_data_key_reuse_period_seconds = 300

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.stage_dlq["webhook_ingest"].arn
    maxReceiveCount     = 3
  })

  tags = merge(var.tags, {
    Name    = "EdlWebhookIngest"
    Purpose = "webhook-ingest"
  })
}

# ---------------------------------------------------------------------------
# Report distribution queue (DL-WF-04). The workflow engine enqueues a request; rendering and
# delivery live in the enterprise-platform, so this is a boundary, not a renderer.
# ---------------------------------------------------------------------------

resource "aws_sqs_queue" "report_distribution" {
  name                       = "EdlReportDistribution"
  message_retention_seconds  = 345600
  visibility_timeout_seconds = 960

  kms_master_key_id                 = var.kms_key_arn
  kms_data_key_reuse_period_seconds = 300

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.stage_dlq["workflow_action"].arn
    maxReceiveCount     = 3
  })

  tags = merge(var.tags, {
    Name    = "EdlReportDistribution"
    Purpose = "report-distribution"
  })
}

# ---------------------------------------------------------------------------
# Depth alarms. A non-empty stage DLQ is a real failure that has already happened, so the
# threshold is zero on every stage rather than a tolerance.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "stage_dlq_depth" {
  for_each = var.alert_topic_arn == "" ? {} : local.pipeline_stages

  alarm_name          = "EdlStageDlqDepth-${each.key}"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.stage_dlq[each.key].name
  }

  alarm_description = join(" ", [
    "The ${each.key} stage DLQ is non-empty. Replay from this stage rather than re-running the",
    "whole pipeline — that is what the per-stage queue exists for.",
  ])

  alarm_actions = [var.alert_topic_arn]

  tags = merge(var.tags, { Stage = each.key, Purpose = "stage-dlq-depth" })
}

resource "aws_cloudwatch_metric_alarm" "replay_exhausted_depth" {
  count = var.alert_topic_arn == "" ? 0 : 1

  alarm_name          = "EdlStageReplayExhaustedDepth"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.stage_replay_exhausted.name
  }

  alarm_description = join(" ", [
    "A message exhausted its replay attempts. This needs a human: three idempotent replays",
    "failing means the failure is deterministic, not transient.",
  ])

  alarm_actions = [var.alert_topic_arn]

  tags = merge(var.tags, { Purpose = "terminal-dlq-depth" })
}

output "stage_dlq_arns" {
  description = "Per-stage DLQ ARNs, keyed by stage."
  value       = { for stage, queue in aws_sqs_queue.stage_dlq : stage => queue.arn }
}

output "stage_dlq_urls" {
  description = "Per-stage DLQ URLs, keyed by stage."
  value       = { for stage, queue in aws_sqs_queue.stage_dlq : stage => queue.id }
}

output "webhook_ingest_queue_url" {
  description = "FIFO queue URL the webhook receiver enqueues to."
  value       = aws_sqs_queue.webhook_ingest.id
}

output "webhook_ingest_queue_arn" {
  description = "FIFO queue ARN the webhook receiver enqueues to."
  value       = aws_sqs_queue.webhook_ingest.arn
}

output "report_distribution_queue_url" {
  description = "Queue URL the workflow engine enqueues report requests to."
  value       = aws_sqs_queue.report_distribution.id
}

output "replay_exhausted_queue_arn" {
  description = "Terminal DLQ ARN; a message here is never automatically retried again."
  value       = aws_sqs_queue.stage_replay_exhausted.arn
}
