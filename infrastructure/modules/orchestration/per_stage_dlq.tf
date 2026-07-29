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
  # `latency_class` selects the neglect threshold below. Cut around the **critical path to the
  # serving store**, because that is what the freshness commitment measures — not around which
  # stages block which others:
  #
  #   critical_path — on the path to fresh curated/serving data; a delay here breaches the SLA
  #   additive      — enriches but does not gate freshness (twin_build's own Catch routes straight
  #                   to LoadServingStore, so its failure cannot delay the serving store)
  #   realtime      — near-real-time ingress, where lag is visible to the source system
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

  # ---------------------------------------------------------------------------
  # DLQ alarm thresholds, per environment.
  #
  # Sized for the 12-month production target agreed 2026-07-29: 10-20 tenants, 5-12 sources per
  # tenant, 100+ entities per source. At 20 tenants that is 10,000-24,000 runs/day and
  # 60,000-144,000 stage executions/day; at a 0.5% transient failure rate, ~120 DLQ arrivals/day,
  # or roughly 20 per stage per day. See docs/SCALE_AND_DLQ_THRESHOLDS.md for the derivation.
  #
  # Why this is keyed by environment rather than a constant: a `depth > 0` alarm is *correct* in
  # dev, where volume is near zero and any DLQ message is genuinely news, and *wrong* in prod,
  # where it would sit in permanent ALARM at ~120 arrivals/day and stop carrying information. An
  # alarm that never clears is as uninformative as one that never fires.
  #
  # Depth is deliberately the weakest of the three signals. It cannot distinguish "1,000 messages
  # arriving and draining fine" from "one message stuck for three days", so it is used only as a
  # capacity guard. The primary signal is age: is anything being neglected?
  # ---------------------------------------------------------------------------
  # Detection budget is derived from the freshness commitment, not chosen. With a 2-hour tight-end
  # SLA and a happy-path pipeline of roughly one hour, the remaining hour must cover detect +
  # acknowledge + triage + replay:
  #
  #     replay from the failed stage onward   ~30 min
  #     acknowledge and triage                ~15 min
  #     ---------------------------------------------
  #     detection budget                      ~15 min   -> oldest_critical_path_seconds = 900
  #
  # An hour of detection (the first cut of this file) would have consumed the entire recovery
  # budget on its own, leaving a breach unavoidable the moment a critical-path stage failed.
  dlq_alarm_defaults = {
    dev = {
      oldest_critical_path_seconds = 900
      oldest_additive_seconds      = 3600
      oldest_realtime_seconds      = 300
      arrival_spike_per_period     = 0
      backlog_depth                = 0
    }
    staging = {
      oldest_critical_path_seconds = 900
      oldest_additive_seconds      = 7200
      oldest_realtime_seconds      = 600
      arrival_spike_per_period     = 10
      backlog_depth                = 200
    }
    prod = {
      # 15 min on the critical path so a failed run can still land inside the 2-hour commitment.
      # Additive stages get an hour: a missing twin does not make curated data stale.
      oldest_critical_path_seconds = 900
      oldest_additive_seconds      = 3600
      oldest_realtime_seconds      = 900
      # ~20 arrivals/stage/day is ~0.07 per 5-minute period, so 50 is a burst rather than routine
      # failure. Replace with a CloudWatch anomaly-detection band once ~2 weeks of real baseline
      # exists — a static number will drift as tenants onboard.
      arrival_spike_per_period = 50
      # ~100 days of normal accumulation: means triage has stopped, or something systemic.
      backlog_depth = 2000
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
# Three-tier DLQ alarms. Each answers a different operational question, and only the first is an
# SLO: depth alone conflates arrival rate with drain rate.
#
#   1. neglect  — ApproximateAgeOfOldestMessage. "Is anything being ignored?" Self-clears on
#                 drain, which a depth alarm does not.
#   2. spike    — NumberOfMessagesSent. "Is the failure rate abnormal right now?"
#   3. backlog  — ApproximateNumberOfMessagesVisible. "Are we failing to keep up?" Capacity guard.
#
# All three use treat_missing_data = "notBreaching": an empty queue publishes no data, and that is
# the healthy state. This is the opposite of the G6 absence alarms, where silence means a control
# stopped running and therefore breaches.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "stage_dlq_oldest_message" {
  for_each = var.alert_topic_arn == "" ? {} : local.pipeline_stages

  alarm_name          = "EdlStageDlqOldestMessage-${each.key}"
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

  alarm_name          = "EdlStageDlqArrivalSpike-${each.key}"
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

  alarm_name          = "EdlStageDlqBacklog-${each.key}"
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
