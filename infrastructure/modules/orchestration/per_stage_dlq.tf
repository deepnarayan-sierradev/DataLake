
locals {
  pipeline_stages = {
    extraction         = { visibility_timeout = 960, latency_class = "critical_path" }
    transformation     = { visibility_timeout = 960, latency_class = "critical_path" }
    entity_resolution  = { visibility_timeout = 960, latency_class = "critical_path" }
    analytics_publish  = { visibility_timeout = 420, latency_class = "critical_path" }
    serving_store_load = { visibility_timeout = 960, latency_class = "critical_path" }
    twin_build         = { visibility_timeout = 960, latency_class = "additive" }
    workflow_action    = { visibility_timeout = 420, latency_class = "additive" }
    webhook_ingest     = { visibility_timeout = 120, latency_class = "realtime" }
    writeback          = { visibility_timeout = 420, latency_class = "realtime" }
  }

  dlq_alarm_defaults = {
    dev = {
      oldest_critical_path_seconds = 900
      oldest_additive_seconds      = 3600
      oldest_realtime_seconds      = 300
      arrival_spike_per_period     = 0
      backlog_depth                = 0
    }
    uat = {
      oldest_critical_path_seconds = 900
      oldest_additive_seconds      = 7200
      oldest_realtime_seconds      = 600
      arrival_spike_per_period     = 10
      backlog_depth                = 200
    }
    prod = {
      oldest_critical_path_seconds = 900
      oldest_additive_seconds      = 3600
      oldest_realtime_seconds      = 900
      arrival_spike_per_period     = 50
      backlog_depth                = 2000
    }
  }

  dlq_alarms = merge(
    local.dlq_alarm_defaults[var.environment],
    var.dlq_alarm_overrides,
  )

  dlq_oldest_seconds = {
    critical_path = local.dlq_alarms.oldest_critical_path_seconds
    additive      = local.dlq_alarms.oldest_additive_seconds
    realtime      = local.dlq_alarms.oldest_realtime_seconds
  }
}

resource "aws_sqs_queue" "stage_replay_exhausted" {
  name                              = "${var.name_prefix}-replay-exhausted-${var.environment}"
  message_retention_seconds         = 1209600
  kms_master_key_id                 = var.kms_key_arn
  kms_data_key_reuse_period_seconds = 300

  tags = merge(var.tags, {
    Name    = "${var.name_prefix}-replay-exhausted-${var.environment}"
    Purpose = "terminal-dlq"
  })
}

resource "aws_sqs_queue" "stage_dlq" {
  for_each = local.pipeline_stages

  name = "${var.name_prefix}-${replace(each.key, "_", "-")}-dlq-${var.environment}"

  message_retention_seconds = 1209600

  visibility_timeout_seconds = each.value.visibility_timeout

  kms_master_key_id                 = var.kms_key_arn
  kms_data_key_reuse_period_seconds = 300

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.stage_replay_exhausted.arn
    maxReceiveCount     = 3
  })

  tags = merge(var.tags, {
    Name    = "${var.name_prefix}-${replace(each.key, "_", "-")}-dlq-${var.environment}"
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


resource "aws_sqs_queue" "webhook_ingest" {
  name                        = "${var.name_prefix}-webhook-ingest-${var.environment}.fifo"
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
    Name    = "${var.name_prefix}-webhook-ingest-${var.environment}.fifo"
    Purpose = "webhook-ingest"
  })
}


resource "aws_sqs_queue" "report_distribution" {
  name                       = "${var.name_prefix}-report-distribution-${var.environment}"
  message_retention_seconds  = 345600
  visibility_timeout_seconds = 960

  kms_master_key_id                 = var.kms_key_arn
  kms_data_key_reuse_period_seconds = 300

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.stage_dlq["workflow_action"].arn
    maxReceiveCount     = 3
  })

  tags = merge(var.tags, {
    Name    = "${var.name_prefix}-report-distribution-${var.environment}"
    Purpose = "report-distribution"
  })
}


resource "aws_cloudwatch_metric_alarm" "stage_dlq_oldest_message" {
  for_each = var.alert_topic_arn == "" ? {} : local.pipeline_stages

  alarm_name          = "${var.name_prefix}-${replace(each.key, "_", "-")}-dlq-oldest-message-${var.environment}"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = local.dlq_oldest_seconds[each.value.latency_class]
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.stage_dlq[each.key].name
  }

  alarm_description = join(" ", [
    "A ${each.key} failure has sat in its DLQ past the",
    "${each.value.latency_class} neglect threshold",
    "(${local.dlq_oldest_seconds[each.value.latency_class]}s).",
    "Replay from this stage rather than re-running the whole pipeline.",
  ])

  alarm_actions = [var.alert_topic_arn]
  ok_actions    = [var.alert_topic_arn]

  tags = merge(var.tags, { Stage = each.key, Purpose = "stage-dlq-neglect" })
}

resource "aws_cloudwatch_metric_alarm" "stage_dlq_arrival_spike" {
  for_each = var.alert_topic_arn == "" ? {} : local.pipeline_stages

  alarm_name          = "${var.name_prefix}-${replace(each.key, "_", "-")}-dlq-arrival-spike-${var.environment}"
  namespace           = "AWS/SQS"
  metric_name         = "NumberOfMessagesSent"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = local.dlq_alarms.arrival_spike_per_period
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.stage_dlq[each.key].name
  }

  alarm_description = join(" ", [
    "More than ${local.dlq_alarms.arrival_spike_per_period} ${each.key} failures arrived in five",
    "minutes. This is a burst, not routine failure — suspect a bad deploy, an expired credential,",
    "or one tenant's source being down. Check the TenantCode dimension before treating it as a",
    "platform incident.",
  ])

  alarm_actions = [var.alert_topic_arn]

  tags = merge(var.tags, { Stage = each.key, Purpose = "stage-dlq-arrival-rate" })
}

resource "aws_cloudwatch_metric_alarm" "stage_dlq_backlog" {
  for_each = var.alert_topic_arn == "" ? {} : local.pipeline_stages

  alarm_name          = "${var.name_prefix}-${replace(each.key, "_", "-")}-dlq-backlog-${var.environment}"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = local.dlq_alarms.backlog_depth
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.stage_dlq[each.key].name
  }

  alarm_description = join(" ", [
    "The ${each.key} DLQ holds more than ${local.dlq_alarms.backlog_depth} messages. Capacity",
    "guard, not an SLO — either triage has stopped or failures are arriving faster than they are",
    "being cleared.",
  ])

  alarm_actions = [var.alert_topic_arn]

  tags = merge(var.tags, { Stage = each.key, Purpose = "stage-dlq-backlog" })
}

resource "aws_cloudwatch_metric_alarm" "replay_exhausted_depth" {
  count = var.alert_topic_arn == "" ? 0 : 1

  alarm_name          = "${var.name_prefix}-replay-exhausted-depth-${var.environment}"
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
