
locals {
  programme_tables = {
    "source-connections" = {
      hash_key      = "tenant_code"
      range_key     = "connection_id"
      purpose       = "source-connections"
      ttl_attribute = null
      gsi           = null
    }
    "scope-units" = {
      hash_key      = "tenant_code"
      range_key     = "scope_unit_id"
      purpose       = "scope-units"
      ttl_attribute = null
      gsi           = null
    }
    "workflow-circuit-breaker" = {
      hash_key      = "tenant_code"
      range_key     = "destination"
      purpose       = "workflow-circuit-breaker"
      ttl_attribute = "expires_at"
      gsi           = null
    }
    "tenant-usage-metering" = {
      hash_key      = "tenant_code"
      range_key     = "usage_key"
      purpose       = "tenant-usage-metering"
      ttl_attribute = null
      gsi           = null
    }
    "effective-config" = {
      hash_key      = "tenant_code"
      range_key     = "capability_key"
      purpose       = "effective-config"
      ttl_attribute = null
      gsi           = null
    }
    "config-restatements" = {
      hash_key      = "tenant_code"
      range_key     = "restatement_key"
      purpose       = "config-restatements"
      ttl_attribute = null
      gsi           = null
    }
    "config-governance" = {
      hash_key      = "tenant_code"
      range_key     = "record_key"
      purpose       = "config-governance"
      ttl_attribute = null
      gsi           = null
    }
    "data-quality-exceptions" = {
      hash_key      = "tenant_code"
      range_key     = "exception_key"
      purpose       = "data-quality-exceptions"
      ttl_attribute = null
      gsi = {
        name      = "tenant-entity-detected-index"
        hash_key  = "tenant_entity_key"
        range_key = "detected_at"
      }
    }
    "reconciliation-reports" = {
      hash_key      = "tenant_code"
      range_key     = "report_key"
      purpose       = "reconciliation-reports"
      ttl_attribute = null
      gsi           = null
    }
    "backfill-jobs" = {
      hash_key      = "tenant_code"
      range_key     = "job_key"
      purpose       = "backfill-jobs"
      ttl_attribute = null
      gsi           = null
    }
    "quality-policy-attachments" = {
      hash_key      = "tenant_code"
      range_key     = "entity_id"
      purpose       = "quality-policy-attachments"
      ttl_attribute = null
      gsi           = null
    }
    "brand-registry" = {
      hash_key      = "tenant_code"
      range_key     = "brand_code"
      purpose       = "brand-registry"
      ttl_attribute = null
      gsi           = null
    }
    "semantic-approvals" = {
      hash_key      = "tenant_code"
      range_key     = "approval_key"
      purpose       = "semantic-approvals"
      ttl_attribute = null
      gsi           = null
    }
    "workflow-definitions" = {
      hash_key      = "tenant_code"
      range_key     = "workflow_key"
      purpose       = "workflow-definitions"
      ttl_attribute = null
      gsi           = null
    }
    # DL-WF-08: execution history, with a GSI for the status dashboard.
    "workflow-executions" = {
      hash_key      = "tenant_code"
      range_key     = "execution_key"
      purpose       = "workflow-executions"
      ttl_attribute = null
      gsi = {
        name      = "status-started-index"
        hash_key  = "status_started_at"
        range_key = "started_at"
      }
    }
    "workflow-tasks" = {
      hash_key      = "tenant_code"
      range_key     = "task_id"
      purpose       = "workflow-tasks"
      ttl_attribute = null
      gsi = {
        name      = "assignee-status-index"
        hash_key  = "assignee_status"
        range_key = "due_at"
      }
    }
    "workflow-idempotency" = {
      hash_key      = "tenant_code"
      range_key     = "idempotency_key"
      purpose       = "workflow-idempotency"
      ttl_attribute = "expires_at"
      gsi           = null
    }
    "workflow-destinations" = {
      hash_key      = "tenant_code"
      range_key     = "destination_id"
      purpose       = "workflow-destinations"
      ttl_attribute = null
      gsi           = null
    }
    "export-jobs" = {
      hash_key      = "tenant_code"
      range_key     = "job_id"
      purpose       = "export-jobs"
      ttl_attribute = null
      gsi           = null
    }
    "deletion-certificates" = {
      hash_key      = "tenant_code"
      range_key     = "certificate_id"
      purpose       = "deletion-certificates"
      ttl_attribute = null
      gsi           = null
    }
    "subprocessor-register" = {
      hash_key      = "register_scope"
      range_key     = "subprocessor_name"
      purpose       = "subprocessor-register"
      ttl_attribute = null
      gsi           = null
    }
    "serving-credential-claims" = {
      hash_key      = "tenant_code"
      range_key     = "claim_id"
      purpose       = "serving-credential-claims"
      ttl_attribute = "expires_epoch"
      gsi           = null
    }
    "webhook-event-dedup" = {
      hash_key      = "tenant_code"
      range_key     = "provider_event_id"
      purpose       = "webhook-event-dedup"
      ttl_attribute = "expires_at"
      gsi           = null
    }
  }
}

resource "aws_dynamodb_table" "programme" {
  for_each = local.programme_tables

  name         = "${var.name_prefix}-${each.value.purpose}-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"

  hash_key  = each.value.hash_key
  range_key = each.value.range_key

  attribute {
    name = each.value.hash_key
    type = "S"
  }

  attribute {
    name = each.value.range_key
    type = "S"
  }

  dynamic "attribute" {
    for_each = each.value.gsi == null ? [] : [each.value.gsi.hash_key]
    content {
      name = attribute.value
      type = "S"
    }
  }

  dynamic "attribute" {
    for_each = each.value.gsi == null ? [] : [each.value.gsi.range_key]
    content {
      name = attribute.value
      type = "S"
    }
  }

  dynamic "global_secondary_index" {
    for_each = each.value.gsi == null ? [] : [each.value.gsi]
    content {
      name            = global_secondary_index.value.name
      hash_key        = global_secondary_index.value.hash_key
      range_key       = global_secondary_index.value.range_key
      projection_type = "ALL"
    }
  }

  dynamic "ttl" {
    for_each = each.value.ttl_attribute == null ? [] : [each.value.ttl_attribute]
    content {
      attribute_name = ttl.value
      enabled        = true
    }
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.database_kms_key_arn
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name    = "${var.name_prefix}-${each.value.purpose}-${var.environment}"
    Purpose = each.value.purpose
  })
}

output "programme_table_names" {
  description = "Physical name of every programme table, keyed by purpose."
  value       = { for k, table in aws_dynamodb_table.programme : k => table.name }
}

output "programme_table_arns" {
  description = "ARNs of every programme table, keyed by purpose."
  value       = { for name, table in aws_dynamodb_table.programme : name => table.arn }
}
