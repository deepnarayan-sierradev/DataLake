"""
CloudWatch metrics emitter for the Enterprise Data Lake platform.

Emits the canonical metric set defined in the observability contract:
  - ExtractionDurationMs
  - RecordsExtracted
  - RecordsSkipped
  - RecordsFailed
  - RetryCount
  - SchemaDriftCount
  - WatermarkLagSeconds

Security:
  - Metric dimension values are scrubbed before emission.
  - CloudWatch API errors are logged as warnings but never propagate
    to the pipeline — metric emission must never fail an extraction run.
  - The CloudWatch client is constructed with explicit region_name; never
    relies on implicit region from environment variables in production code.

Usage:
    emitter = CloudWatchMetricsEmitter(region_name="us-east-1")
    emitter.emit_records_extracted(
        source_id="salesforce",
        entity_id="salesforce-account",
        environment="prod",
        count=45_000,
    )
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

import boto3
from botocore.exceptions import ClientError

from contracts.observability_contract import scrub_sensitive_values
from contracts.platform_metrics import PlatformMetric, metric_unit
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

PLATFORM_METRIC_NAMESPACE: Final[str] = "EnterpriseDatalake"

_ALLOWED_DIMENSION_NAMES: Final[frozenset[str]] = frozenset(
    {
        "TenantCode",
        "SourceId",
        "EntityId",
        "EntityType",
        "Environment",
        "Stage",
        "Capability",
        "Surface",
        "Severity",
        "Format",
        "ActionType",
        "Status",
        "StatusClass",
        "ConnectionId",
        "Rule",
        "Engine",
    }
)


class CloudWatchMetricsEmitter:
    """
    Emits CloudWatch metrics for all pipeline stages.

    Metrics are buffered locally and flushed in a single PutMetricData call
    (up to 1 000 data points per request).  This reduces CloudWatch API calls
    from O(metric_count) per run to O(1) — fixing F-15 (6 API calls per run).

    Call ``flush()`` at the end of each pipeline run to deliver buffered metrics.
    Unflushed metrics are discarded on process exit.

    One instance per service invocation. Thread-safe for read operations
    (boto3 CloudWatch client is thread-safe for PutMetricData).
    """

    _MAX_METRICS_PER_REQUEST: Final[int] = 1_000  # CloudWatch hard limit

    def __init__(
        self,
        region_name: str,
        namespace: str = PLATFORM_METRIC_NAMESPACE,
        tenant_code: str | None = None,
    ) -> None:
        self._namespace = namespace
        self._client = boto3.client("cloudwatch", region_name=region_name)
        self._pending: list[dict[str, object]] = []
        self._tenant_code: str | None = tenant_code

    def set_tenant_context(self, tenant_code: str) -> None:
        """
        Set the tenant code for all subsequent metric emissions.

        Call once per Lambda invocation after validating the event's tenant_code.
        All metrics emitted after this call will include the TenantCode dimension,
        enabling per-tenant CloudWatch dashboards and alarms.

        Args:
            tenant_code: Validated tenant slug (e.g. 'demo', 'acme-corp').
        """
        self._tenant_code = tenant_code

    def emit_extraction_duration(
        self,
        source_id: str,
        entity_id: str,
        environment: str,
        duration_ms: float,
        stage: str | None = None,
    ) -> None:
        """Emit the total extraction duration for one entity run."""
        self._put_metric(
            metric_name="ExtractionDurationMs",
            value=duration_ms,
            unit="Milliseconds",
            dimensions=self._build_dimensions(
                source_id, entity_id, environment, stage=stage, tenant_code=self._tenant_code
            ),
        )

    def emit_records_extracted(
        self,
        source_id: str,
        entity_id: str,
        environment: str,
        count: int,
        stage: str | None = None,
    ) -> None:
        """Emit the count of records successfully extracted and written to raw layer."""
        self._put_metric(
            metric_name="RecordsExtracted",
            value=float(count),
            unit="Count",
            dimensions=self._build_dimensions(
                source_id, entity_id, environment, stage=stage, tenant_code=self._tenant_code
            ),
        )

    def emit_records_failed(
        self,
        source_id: str,
        entity_id: str,
        environment: str,
        count: int,
        stage: str | None = None,
    ) -> None:
        """Emit the count of records that failed extraction or validation."""
        self._put_metric(
            metric_name="RecordsFailed",
            value=float(count),
            unit="Count",
            dimensions=self._build_dimensions(
                source_id, entity_id, environment, stage=stage, tenant_code=self._tenant_code
            ),
        )

    def emit_records_skipped(
        self,
        source_id: str,
        entity_id: str,
        environment: str,
        count: int,
        stage: str | None = None,
    ) -> None:
        """Emit the count of records left unchanged (skipped, not an error) in a run."""
        self._put_metric(
            metric_name="RecordsSkipped",
            value=float(count),
            unit="Count",
            dimensions=self._build_dimensions(
                source_id, entity_id, environment, stage=stage, tenant_code=self._tenant_code
            ),
        )

    def emit_retry_count(
        self,
        source_id: str,
        entity_id: str,
        environment: str,
        count: int,
        stage: str | None = None,
    ) -> None:
        """Emit the total retry attempts for a run stage."""
        self._put_metric(
            metric_name="RetryCount",
            value=float(count),
            unit="Count",
            dimensions=self._build_dimensions(
                source_id, entity_id, environment, stage=stage, tenant_code=self._tenant_code
            ),
        )

    def emit_schema_drift_count(
        self,
        source_id: str,
        entity_id: str,
        environment: str,
        count: int,
        stage: str | None = None,
    ) -> None:
        """Emit the number of schema drift events detected in a run."""
        self._put_metric(
            metric_name="SchemaDriftCount",
            value=float(count),
            unit="Count",
            dimensions=self._build_dimensions(
                source_id, entity_id, environment, stage=stage, tenant_code=self._tenant_code
            ),
        )

    def emit_watermark_lag_seconds(
        self,
        source_id: str,
        entity_id: str,
        environment: str,
        lag_seconds: float,
        stage: str | None = None,
    ) -> None:
        """Emit the lag between the current watermark and now (data freshness indicator)."""
        self._put_metric(
            metric_name="WatermarkLagSeconds",
            value=lag_seconds,
            unit="Seconds",
            dimensions=self._build_dimensions(
                source_id, entity_id, environment, stage=stage, tenant_code=self._tenant_code
            ),
        )

    def emit_stage_duration(
        self,
        source_id: str,
        entity_id: str,
        environment: str,
        stage: str,
        duration_ms: float,
    ) -> None:
        """Emit the total duration for a named pipeline stage."""
        self._put_metric(
            metric_name="StageDurationMs",
            value=duration_ms,
            unit="Milliseconds",
            dimensions=self._build_dimensions(
                source_id, entity_id, environment, stage=stage, tenant_code=self._tenant_code
            ),
        )

    def emit_golden_record_count(
        self,
        source_id: str,
        entity_id: str,
        environment: str,
        count: int,
    ) -> None:
        """Emit the count of golden records produced by entity resolution."""
        self._put_metric(
            metric_name="GoldenRecordCount",
            value=float(count),
            unit="Count",
            dimensions=self._build_dimensions(
                source_id, entity_id, environment, tenant_code=self._tenant_code
            ),
        )

    def emit_cluster_count(
        self,
        source_id: str,
        entity_id: str,
        environment: str,
        count: int,
    ) -> None:
        """Emit the count of entity clusters identified during entity resolution."""
        self._put_metric(
            metric_name="ClusterCount",
            value=float(count),
            unit="Count",
            dimensions=self._build_dimensions(
                source_id, entity_id, environment, tenant_code=self._tenant_code
            ),
        )

    def emit_circuit_breaker_opened(
        self, source_id: str, entity_id: str, environment: str, count: int = 1
    ) -> None:
        """Emit when a source circuit breaker transitions to open (OBS-01 alarm signal)."""
        self._put_metric(
            metric_name="CircuitBreakerOpened",
            value=float(count),
            unit="Count",
            dimensions=self._build_dimensions(
                source_id, entity_id, environment, tenant_code=self._tenant_code
            ),
        )

    def emit_circuit_breaker_ddb_fallback(
        self, source_id: str, entity_id: str, environment: str, count: int = 1
    ) -> None:
        """Emit when circuit-breaker state falls back to DynamoDB (OBS-01 alarm signal)."""
        self._put_metric(
            metric_name="CircuitBreakerDDBFallback",
            value=float(count),
            unit="Count",
            dimensions=self._build_dimensions(
                source_id, entity_id, environment, tenant_code=self._tenant_code
            ),
        )

    def emit_input_validation_failures(
        self, source_id: str, entity_id: str, environment: str, count: int = 1
    ) -> None:
        """Emit when a Lambda event fails input validation (OBS-01 alarm signal; OWASP A03)."""
        self._put_metric(
            metric_name="InputValidationFailures",
            value=float(count),
            unit="Count",
            dimensions=self._build_dimensions(
                source_id, entity_id, environment, tenant_code=self._tenant_code
            ),
        )

    def emit_credential_retrieval_failures(
        self, source_id: str, entity_id: str, environment: str, count: int = 1
    ) -> None:
        """Emit when source credential retrieval fails (OBS-01 alarm signal)."""
        self._put_metric(
            metric_name="CredentialRetrievalFailures",
            value=float(count),
            unit="Count",
            dimensions=self._build_dimensions(
                source_id, entity_id, environment, tenant_code=self._tenant_code
            ),
        )

    def emit_metric(
        self,
        metric: PlatformMetric,
        value: float = 1.0,
        *,
        environment: str | None = None,
        dimensions: dict[str, str] | None = None,
    ) -> None:
        """Emit any catalogued metric; the unit comes from the catalogue, never the caller."""
        dims: list[dict[str, str]] = []
        if self._tenant_code:
            dims.append({"Name": "TenantCode", "Value": self._tenant_code})
        if environment:
            dims.append({"Name": "Environment", "Value": environment})
        for name, dim_value in (dimensions or {}).items():
            if name not in _ALLOWED_DIMENSION_NAMES:
                raise ValueError(
                    f"Dimension {name!r} is not in the permitted dimension set. "
                    f"Permitted: {sorted(_ALLOWED_DIMENSION_NAMES)}."
                )
            dims.append({"Name": name, "Value": scrub_sensitive_values(str(dim_value))[:255]})
        self._pending.append(
            {
                "MetricName": metric.value,
                "Dimensions": dims,
                "Timestamp": datetime.now(UTC),
                "Value": float(value),
                "Unit": metric_unit(metric).value,
            }
        )

    def _put_metric(
        self,
        metric_name: str,
        value: float,
        unit: str,
        dimensions: list[dict[str, str]],
    ) -> None:
        """Buffer a single metric data point for deferred batch delivery."""
        self._pending.append(
            {
                "MetricName": metric_name,
                "Dimensions": dimensions,
                "Timestamp": datetime.now(UTC),
                "Value": value,
                "Unit": unit,
            }
        )

    def flush(self) -> None:
        """
        Deliver all buffered metric data points to CloudWatch.

        Sends in chunks of up to _MAX_METRICS_PER_REQUEST items.
        Errors are logged as warnings and swallowed — metric emission failure
        must never propagate to interrupt an extraction run.
        Call this once at the end of each pipeline run.
        """
        if not self._pending:
            return
        batch = self._pending
        self._pending = []
        for i in range(0, len(batch), self._MAX_METRICS_PER_REQUEST):
            chunk = batch[i : i + self._MAX_METRICS_PER_REQUEST]
            try:
                self._client.put_metric_data(
                    Namespace=self._namespace,
                    MetricData=chunk,  # type: ignore[arg-type]
                )
            except ClientError as exc:
                _logger.warning(
                    "cloudwatch_metric_emission_failed",
                    chunk_size=len(chunk),
                    error=scrub_sensitive_values(str(exc)),
                )

    @staticmethod
    def _build_dimensions(
        source_id: str,
        entity_id: str,
        environment: str,
        stage: str | None = None,
        tenant_code: str | None = None,
    ) -> list[dict[str, str]]:
        dims: list[dict[str, str]] = []
        if tenant_code:
            dims.append({"Name": "TenantCode", "Value": tenant_code})
        dims.extend(
            [
                {"Name": "SourceId", "Value": source_id},
                {"Name": "EntityId", "Value": entity_id},
                {"Name": "Environment", "Value": environment},
            ]
        )
        if stage:
            dims.append({"Name": "Stage", "Value": stage})
        return dims
