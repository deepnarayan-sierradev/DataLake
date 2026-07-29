"""
Canonical catalogue of every CloudWatch metric the platform emits.

One declaration site for metric name + unit, consumed by `CloudWatchMetricsEmitter`
and reconciled against the Terraform alarm definitions by
`observability/tests/test_alarm_emitter_reconciliation.py`. A metric without an
alarm, or an alarm without a catalogue entry, fails CI.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class MetricUnit(StrEnum):
    COUNT = "Count"
    MILLISECONDS = "Milliseconds"
    SECONDS = "Seconds"
    PERCENT = "Percent"
    BYTES = "Bytes"
    COUNT_PER_SECOND = "Count/Second"
    NONE = "None"


class PlatformMetric(StrEnum):
    """Every metric name emitted into `PLATFORM_METRIC_NAMESPACE`."""

    # ── Extraction / pipeline core (pre-existing) ─────────────────────────────
    EXTRACTION_DURATION_MS = "ExtractionDurationMs"
    RECORDS_EXTRACTED = "RecordsExtracted"
    RECORDS_FAILED = "RecordsFailed"
    RECORDS_SKIPPED = "RecordsSkipped"
    RETRY_COUNT = "RetryCount"
    SCHEMA_DRIFT_COUNT = "SchemaDriftCount"
    WATERMARK_LAG_SECONDS = "WatermarkLagSeconds"
    STAGE_DURATION_MS = "StageDurationMs"
    GOLDEN_RECORD_COUNT = "GoldenRecordCount"
    CLUSTER_COUNT = "ClusterCount"
    CIRCUIT_BREAKER_OPENED = "CircuitBreakerOpened"
    CIRCUIT_BREAKER_DDB_FALLBACK = "CircuitBreakerDDBFallback"
    INPUT_VALIDATION_FAILURES = "InputValidationFailures"
    CREDENTIAL_RETRIEVAL_FAILURES = "CredentialRetrievalFailures"

    # ── DL-01 connectors ──────────────────────────────────────────────────────
    RATE_LIMIT_HITS = "RateLimitHits"
    RATE_LIMIT_BACKOFF_MS = "RateLimitBackoffMs"
    PAGES_FETCHED = "PagesFetched"
    WEBHOOK_EVENTS_RECEIVED = "WebhookEventsReceived"
    WEBHOOK_SIGNATURE_FAILURES = "WebhookSignatureFailures"
    WRITEBACK_RECORDS = "WritebackRecords"
    WRITEBACK_FAILURES = "WritebackFailures"
    SOURCE_API_ERRORS = "SourceApiErrors"
    CHECKPOINTED_RUNS = "CheckpointedRuns"

    # ── DL-02 quality and reconciliation ──────────────────────────────────────
    QUALITY_VIOLATIONS = "QualityViolations"
    QUALITY_GATE_BLOCKS = "QualityGateBlocks"
    COMPLETENESS_RATE = "CompletenessRate"
    DUPLICATE_RATE = "DuplicateRate"
    ORPHAN_RATE = "OrphanRate"
    RECONCILIATION_VARIANCE_PCT = "ReconciliationVariancePct"
    RECONCILIATION_FAILURES = "ReconciliationFailures"
    BACKFILL_CHUNKS_COMPLETED = "BackfillChunksCompleted"
    BACKFILL_CHUNKS_FAILED = "BackfillChunksFailed"
    BACKFILL_ROWS_PER_SECOND = "BackfillRowsPerSecond"

    # ── DL-03 semantic ────────────────────────────────────────────────────────
    SEMANTIC_QUERIES_COMPILED = "SemanticQueriesCompiled"
    SEMANTIC_QUERY_LATENCY_MS = "SemanticQueryLatencyMs"
    SEMANTIC_ACCESS_DENIED = "SemanticAccessDenied"
    SEMANTIC_CACHE_HIT_RATE = "SemanticCacheHitRate"
    MODEL_PUBLISHES = "ModelPublishes"
    MODEL_VALIDATION_FAILURES = "ModelValidationFailures"
    KPI_VALIDATION_FAILURES = "KpiValidationFailures"

    # ── DL-06 workflow ────────────────────────────────────────────────────────
    WORKFLOW_EXECUTIONS = "WorkflowExecutions"
    WORKFLOW_CONDITION_EVALUATIONS = "WorkflowConditionEvaluations"
    WORKFLOW_ACTIONS_EXECUTED = "WorkflowActionsExecuted"
    WORKFLOW_ACTION_FAILURES = "WorkflowActionFailures"
    WORKFLOW_TASKS_OPEN = "WorkflowTasksOpen"
    WORKFLOW_TASK_AGE_HOURS = "WorkflowTaskAgeHours"
    WORKFLOW_ESCALATIONS = "WorkflowEscalations"
    WORKFLOW_CIRCUIT_BREAKER_OPEN = "WorkflowCircuitBreakerOpen"
    WORKFLOW_DLQ_DEPTH = "WorkflowDlqDepth"

    # ── DL-07 serving ─────────────────────────────────────────────────────────
    SERVING_STORE_LOAD_ROWS = "ServingStoreLoadRows"
    SERVING_STORE_LOAD_DURATION_MS = "ServingStoreLoadDurationMs"
    SERVING_STORE_LOAD_FAILURES = "ServingStoreLoadFailures"
    SERVING_STORE_SKIPPED_NO_CONFIG = "ServingStoreSkippedNoConfig"
    SERVING_STORE_CONNECTION_ERRORS = "ServingStoreConnectionErrors"
    VPN_CLIENT_CONNECTIONS = "VpnClientConnections"
    VPN_CERTIFICATE_DAYS_TO_EXPIRY = "VpnCertificateDaysToExpiry"
    SERVING_QUERY_LATENCY_MS = "ServingQueryLatencyMs"
    SERVING_CONCURRENT_CONNECTIONS = "ServingConcurrentConnections"

    # ── DL-08 security ────────────────────────────────────────────────────────
    AUTHENTICATION_FAILURES = "AuthenticationFailures"
    AUTHORIZATION_DENIALS = "AuthorizationDenials"
    ADMIN_ACTIONS = "AdminActions"
    CROSS_TENANT_ACCESS_ATTEMPTS = "CrossTenantAccessAttempts"
    # IAM's own verdict, from the CloudTrail metric filter — distinct from the line above, which
    # counts application-level claim mismatches. They were one metric, so the `enforce` gate's
    # "sustained zero" could be satisfied by an unused API and prove nothing about the boundary.
    IAM_BOUNDARY_ACCESS_DENIED = "IamBoundaryAccessDenied"
    WAF_BLOCKED_REQUESTS = "WafBlockedRequests"
    CREDENTIAL_ROTATION_AGE = "CredentialRotationAge"
    ROW_LEVEL_PREDICATE_APPLIED = "RowLevelPredicateApplied"
    SECRET_RETRIEVAL_FAILURES = "SecretRetrievalFailures"  # noqa: S105 — metric name  # nosec B105, not a secret

    # ── DL-09 operations ──────────────────────────────────────────────────────
    PIPELINE_FRESHNESS_SECONDS = "PipelineFreshnessSeconds"
    STAGE_RETRIES = "StageRetries"
    DLQ_DEPTH = "DlqDepth"
    # Counts messages this platform *enqueues*, dimensioned by stage — distinct from DlqDepth,
    # which is SQS's own gauge. Absence-alarmable: a stage that never enqueues is either
    # perfectly healthy or has no producer at all, and for five of six stages it was the latter.
    DLQ_MESSAGES_ENQUEUED = "DlqMessagesEnqueued"
    REPLAY_SUCCESS_RATE = "ReplaySuccessRate"
    COST_PER_TENANT_USD = "CostPerTenantUsd"
    LAMBDA_MEMORY_UTILIZATION = "LambdaMemoryUtilization"
    DEPLOYMENT_DURATION_MS = "DeploymentDurationMs"
    POST_DEPLOY_SMOKE_FAILURES = "PostDeploySmokeFailures"

    # ── DL-10 portability and compliance ──────────────────────────────────────
    EXPORT_JOBS_REQUESTED = "ExportJobsRequested"
    EXPORT_JOBS_COMPLETED = "ExportJobsCompleted"
    EXPORT_JOBS_FAILED = "ExportJobsFailed"
    EXPORT_BYTES = "ExportBytes"
    EXPORT_DURATION_MS = "ExportDurationMs"
    RETENTION_RECORDS_EXPIRED = "RetentionRecordsExpired"
    LEGAL_HOLDS_ACTIVE = "LegalHoldsActive"
    DELETION_STEPS_COMPLETED = "DeletionStepsCompleted"
    PHI_GATE_BLOCKS = "PhiGateBlocks"

    # ── DL-11 configuration propagation ───────────────────────────────────────
    CONFIG_PROPAGATION_LAG_SECONDS = "ConfigPropagationLagSeconds"
    CONFIG_VERSION_PIN_FAILURES = "ConfigVersionPinFailures"
    CONFIG_VERSION_MISMATCH_WITHIN_RUN = "ConfigVersionMismatchWithinRun"
    CONFIG_CACHE_STALE_SERVED = "ConfigCacheStaleServed"
    EFFECTIVE_VERSION_TRANSITIONS = "EffectiveVersionTransitions"
    CONFIG_ROLLBACKS = "ConfigRollbacks"
    REPROCESS_JOBS_STARTED = "ReprocessJobsStarted"
    REPROCESS_JOBS_COMPLETED = "ReprocessJobsCompleted"
    REPROCESS_JOBS_FAILED = "ReprocessJobsFailed"
    REPROCESS_ROWS_RECOMPUTED = "ReprocessRowsRecomputed"
    RESTATEMENT_EVENTS_EMITTED = "RestatementEventsEmitted"
    CONFIG_SCHEMA_INCOMPATIBILITY_REJECTIONS = "ConfigSchemaIncompatibilityRejections"
    CREDENTIAL_CACHE_PROPAGATION_LAG_SECONDS = "CredentialCachePropagationLagSeconds"
    PUBLISHES_NOT_YET_EFFECTIVE = "PublishesNotYetEffective"

    # ── DL-12 connections and scope isolation ─────────────────────────────────
    CROSS_SCOPE_ACCESS_ATTEMPTS = "CrossScopeAccessAttempts"
    SCOPE_PREDICATE_APPLIED = "ScopePredicateApplied"
    UNATTRIBUTED_ROW_RATE = "UnattributedRowRate"
    # Counts attribution passes so absence can be alarmed on: a rate of 0 is healthy, no rate
    # at all means the control never ran (G6).
    SCOPE_ATTRIBUTION_APPLIED = "ScopeAttributionApplied"
    # L17: billable throughput per tenant per period, derived from the audit log.
    TENANT_RECORDS_PROCESSED = "TenantRecordsProcessed"
    SCOPE_GRANT_EXPANSIONS = "ScopeGrantExpansions"
    EMPTY_SCOPE_DENIALS = "EmptyScopeDenials"
    # An unscoped read is now an affirmative, named choice rather than a `None` predicate, so it
    # is countable. A rise here without a matching rise in definition-validation runs means a
    # request path has started reading unfiltered (DL-SCOPE-14).
    UNRESTRICTED_SCOPE_READS = "UnrestrictedScopeReads"
    CONNECTION_HEALTH = "ConnectionHealth"
    CONNECTION_CREDENTIAL_FAILURES = "ConnectionCredentialFailures"
    CONNECTIONS_PER_TENANT = "ConnectionsPerTenant"
    RESOLUTION_SCOPE_VIOLATIONS = "ResolutionScopeViolations"
    AGGREGATE_SUPPRESSIONS = "AggregateSuppressions"
    BENCHMARK_COHORT_SIZE = "BenchmarkCohortSize"


_DURATION_UNITS: Final[dict[PlatformMetric, MetricUnit]] = {
    PlatformMetric.EXTRACTION_DURATION_MS: MetricUnit.MILLISECONDS,
    PlatformMetric.STAGE_DURATION_MS: MetricUnit.MILLISECONDS,
    PlatformMetric.RATE_LIMIT_BACKOFF_MS: MetricUnit.MILLISECONDS,
    PlatformMetric.SEMANTIC_QUERY_LATENCY_MS: MetricUnit.MILLISECONDS,
    PlatformMetric.SERVING_STORE_LOAD_DURATION_MS: MetricUnit.MILLISECONDS,
    PlatformMetric.SERVING_QUERY_LATENCY_MS: MetricUnit.MILLISECONDS,
    PlatformMetric.EXPORT_DURATION_MS: MetricUnit.MILLISECONDS,
    PlatformMetric.DEPLOYMENT_DURATION_MS: MetricUnit.MILLISECONDS,
    PlatformMetric.WATERMARK_LAG_SECONDS: MetricUnit.SECONDS,
    PlatformMetric.PIPELINE_FRESHNESS_SECONDS: MetricUnit.SECONDS,
    PlatformMetric.CONFIG_PROPAGATION_LAG_SECONDS: MetricUnit.SECONDS,
    PlatformMetric.CREDENTIAL_CACHE_PROPAGATION_LAG_SECONDS: MetricUnit.SECONDS,
    PlatformMetric.CREDENTIAL_ROTATION_AGE: MetricUnit.SECONDS,
    PlatformMetric.WORKFLOW_TASK_AGE_HOURS: MetricUnit.NONE,
    PlatformMetric.COMPLETENESS_RATE: MetricUnit.PERCENT,
    PlatformMetric.DUPLICATE_RATE: MetricUnit.PERCENT,
    PlatformMetric.ORPHAN_RATE: MetricUnit.PERCENT,
    PlatformMetric.UNATTRIBUTED_ROW_RATE: MetricUnit.PERCENT,
    PlatformMetric.SCOPE_ATTRIBUTION_APPLIED: MetricUnit.COUNT,
    PlatformMetric.TENANT_RECORDS_PROCESSED: MetricUnit.COUNT,
    PlatformMetric.RECONCILIATION_VARIANCE_PCT: MetricUnit.PERCENT,
    PlatformMetric.SEMANTIC_CACHE_HIT_RATE: MetricUnit.PERCENT,
    PlatformMetric.REPLAY_SUCCESS_RATE: MetricUnit.PERCENT,
    PlatformMetric.LAMBDA_MEMORY_UTILIZATION: MetricUnit.PERCENT,
    PlatformMetric.EXPORT_BYTES: MetricUnit.BYTES,
    PlatformMetric.BACKFILL_ROWS_PER_SECOND: MetricUnit.COUNT_PER_SECOND,
    PlatformMetric.COST_PER_TENANT_USD: MetricUnit.NONE,
    PlatformMetric.VPN_CERTIFICATE_DAYS_TO_EXPIRY: MetricUnit.NONE,
}


def metric_unit(metric: PlatformMetric) -> MetricUnit:
    """Unit for a catalogued metric; everything not explicitly typed is a Count."""
    return _DURATION_UNITS.get(metric, MetricUnit.COUNT)


ALL_PLATFORM_METRIC_NAMES: Final[frozenset[str]] = frozenset(m.value for m in PlatformMetric)
