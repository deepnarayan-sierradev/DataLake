# ---------------------------------------------------------------------------
# Tables introduced by the SOW requirements programme (requirements/DL-01…DL-12).
#
# Kept in a separate file from main.tf so the pre-existing table set stays readable and a
# `terraform plan` diff on this programme's additions is easy to review in isolation.
#
# None of these carry `prevent_destroy`: they are all rebuildable from their source of truth
# (config service publishes, pipeline runs, or the semantic model), unlike the five tables in
# main.tf that hold the only copy of their data. Marking a rebuildable table
# `prevent_destroy` would make a legitimate environment teardown impossible for no gain.
# ---------------------------------------------------------------------------

locals {
  # Table set driven from one map so a new table is one entry, not a copied 30-line block.
  # `gsi` is optional; `ttl_attribute` enables DynamoDB TTL where the requirement declares one.
  programme_tables = {
    # DL-SCOPE-03: one connection per connector instance under a tenant.
    EdlSourceConnection = {
      hash_key      = "tenant_code"
      range_key     = "connection_id"
      purpose       = "source-connections"
      ttl_attribute = null
      gsi           = null
    }
    # DL-SCOPE-01: the scope-unit dimension below tenant_code.
    EdlScopeUnit = {
      hash_key      = "tenant_code"
      range_key     = "scope_unit_id"
      purpose       = "scope-units"
      ttl_attribute = null
      gsi           = null
    }
    # DL-WF-09: fleet-wide circuit-breaker state. The in-memory breaker cannot open under
    # Lambda concurrency — five failures across five containers never trip a five-failure
    # threshold — so the counter has to be shared. TTL'd so a recovered destination is not
    # penalised forever.
    EdlWorkflowCircuitBreaker = {
      hash_key      = "tenant_code"
      range_key     = "destination"
      purpose       = "workflow-circuit-breaker"
      ttl_attribute = "expires_at"
      gsi           = null
    }
    # L17: per-tenant usage per period, recomputed from the audit log rather than incremented, so
    # re-running the metering job cannot double-count an invoice. No TTL: usage history is billing
    # evidence.
    EdlTenantUsage = {
      hash_key      = "tenant_code"
      range_key     = "usage_key"
      purpose       = "tenant-usage-metering"
      ttl_attribute = null
      gsi           = null
    }
    # DL-CFG-08: turns "published" into "in effect".
    EdlEffectiveConfig = {
      hash_key      = "tenant_code"
      range_key     = "capability_key"
      purpose       = "effective-config"
      ttl_attribute = null
      gsi           = null
    }
    # DL-CFG-13: restatement events for definition changes that alter historical figures.
    EdlConfigRestatement = {
      hash_key      = "tenant_code"
      range_key     = "restatement_key"
      purpose       = "config-restatements"
      ttl_attribute = null
      gsi           = null
    }
    # DL-CFG-07/09: publish coordination and audited rollbacks.
    EdlConfigGovernance = {
      hash_key      = "tenant_code"
      range_key     = "record_key"
      purpose       = "config-governance"
      ttl_attribute = null
      gsi           = null
    }
    # DL-DQ-14: the structured exception store, with a GSI for the per-entity triage view.
    #
    # No TTL: an exception is the evidence for a data-quality decision and is referenced by
    # reconciliation verdicts and SOC 2 evidence (DL-SEC-17). Expiring it would let an audit
    # window outlive the records it depends on. Retention is an archival decision, not a
    # table setting — see `data_quality/exception_repository.py`.
    #
    # The GSI is hash-keyed on `tenant_entity_key` (`{tenant_code}#{entity_id}`), never on a
    # bare `entity_id`: a GSI spans every item in the table, so an `entity_id`-keyed index
    # would return every tenant's exceptions for that entity to whoever queried it.
    EdlDataQualityException = {
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
    # DL-DQ-04: signed reconciliation verdicts.
    EdlReconciliationReport = {
      hash_key      = "tenant_code"
      range_key     = "report_key"
      purpose       = "reconciliation-reports"
      ttl_attribute = null
      gsi           = null
    }
    # DL-DQ-01 / DL-CFG-11: backfill and reprocess jobs share one store.
    EdlBackfillJob = {
      hash_key      = "tenant_code"
      range_key     = "job_key"
      purpose       = "backfill-jobs"
      ttl_attribute = null
      gsi           = null
    }
    # DL-DQ-05: quality policy attachment per entity.
    EdlQualityPolicyAttachment = {
      hash_key      = "tenant_code"
      range_key     = "entity_id"
      purpose       = "quality-policy-attachments"
      ttl_attribute = null
      gsi           = null
    }
    # DL-DQ-09: brand as a first-class dimension.
    EdlBrandRegistry = {
      hash_key      = "tenant_code"
      range_key     = "brand_code"
      purpose       = "brand-registry"
      ttl_attribute = null
      gsi           = null
    }
    # DL-SEM-11: approval records for the maker-checker publish.
    EdlSemanticApproval = {
      hash_key      = "tenant_code"
      range_key     = "approval_key"
      purpose       = "semantic-approvals"
      ttl_attribute = null
      gsi           = null
    }
    # DL-WF-01: versioned workflow definitions.
    EdlWorkflowDefinition = {
      hash_key      = "tenant_code"
      range_key     = "workflow_key"
      purpose       = "workflow-definitions"
      ttl_attribute = null
      gsi           = null
    }
    # DL-WF-08: execution history, with a GSI for the status dashboard.
    EdlWorkflowExecution = {
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
    # DL-WF-05/06: approval and triage tasks, with a GSI for the assignee inbox.
    EdlWorkflowTask = {
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
    # DL-WF-07: exactly-once guard; TTL keeps it bounded.
    EdlWorkflowIdempotency = {
      hash_key      = "tenant_code"
      range_key     = "idempotency_key"
      purpose       = "workflow-idempotency"
      ttl_attribute = "expires_at"
      gsi           = null
    }
    # DL-WF-04: the outbound destination allowlist (OWASP A10).
    EdlWorkflowDestination = {
      hash_key      = "tenant_code"
      range_key     = "destination_id"
      purpose       = "workflow-destinations"
      ttl_attribute = null
      gsi           = null
    }
    # DL-PORT-01: export jobs.
    EdlExportJob = {
      hash_key      = "tenant_code"
      range_key     = "job_id"
      purpose       = "export-jobs"
      ttl_attribute = null
      gsi           = null
    }
    # DL-PORT-04: deletion certificates. Retained deliberately — the audit trail must
    # survive deletion of the data it describes.
    EdlDeletionCertificate = {
      hash_key      = "tenant_code"
      range_key     = "certificate_id"
      purpose       = "deletion-certificates"
      ttl_attribute = null
      gsi           = null
    }
    # DL-PORT-07: the subprocessor register.
    EdlSubprocessorRegister = {
      hash_key      = "register_scope"
      range_key     = "subprocessor_name"
      purpose       = "subprocessor-register"
      ttl_attribute = null
      gsi           = null
    }
    # DL-SERV-02: one-time credential claims; TTL bounds an unredeemed claim.
    EdlServingCredentialClaim = {
      hash_key      = "tenant_code"
      range_key     = "claim_id"
      purpose       = "serving-credential-claims"
      ttl_attribute = "expires_epoch"
      gsi           = null
    }
    # DL-CONN-14: webhook replay protection, 48h TTL.
    EdlWebhookEventDedup = {
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

  name         = each.key
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

  # GSI key attributes must also be declared; the dynamic block keeps the map-driven shape.
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
    Name    = each.key
    Purpose = each.value.purpose
  })
}

output "programme_table_names" {
  description = "Names of every table added by the SOW requirements programme."
  value       = [for name, _ in local.programme_tables : name]
}

output "programme_table_arns" {
  description = "ARNs of every table added by the SOW requirements programme, keyed by name."
  value       = { for name, table in aws_dynamodb_table.programme : name => table.arn }
}
