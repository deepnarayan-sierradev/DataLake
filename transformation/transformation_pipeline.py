"""
Transformation pipeline.

Triggered by Step Functions after a successful raw extraction run and watermark
update.  Reads raw Parquet files from S3, applies field mappings, evaluates
quality, and publishes canonical records to the curated layer.

Pipeline steps:
  1. Load raw records from S3 raw prefix
  2. Load field mapping rule set (identity pass-through if none registered)
  3. Apply field mappings to all records
  4. Evaluate quality policy (if configured)
  5. Write canonical records to curated layer (unless publication blocked)
  6. Write quality report to S3
  7. Return TransformationResult

Security (OWASP A03, A05, A09):
  - Raw records are read-only; originals are never modified.
  - Field names validated upstream in FieldMappingRule.__post_init__.
  - Quality violations logged without exposing record payloads (PII protection).
  - S3 prefix validated before listing to prevent path traversal.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final

import boto3
import pyarrow.parquet as pq

from governance.data_catalog_registration import (
    CatalogDatasetSpec,
    DataCatalogRegistrationClient,
    DataLayer,
)
from governance.data_classification_policy import (
    EntityClassificationPolicy,
    FieldMaskingApplier,
)
from governance.lineage_record import (
    LineageEmitter,
    build_transformation_lineage,
)
from observability.metrics_emitter import CloudWatchMetricsEmitter
from observability.structured_logger import get_platform_logger
from transformation.curated_layer_writer import CuratedLayerWriter
from transformation.field_mapping.field_mapping_registry import (
    FieldMappingApplicator,
    FieldMappingRegistryClient,
    FieldMappingRuleSet,
    MappingRuleSetNotFoundError,
)
from transformation.quality_evaluation.quality_policy_evaluator import (
    QualityPolicy,
    QualityPolicyEvaluator,
    QualityReport,
)

_logger = get_platform_logger(__name__)

# S3 prefix safety: no path traversal sequences, no leading slash, bounded length (OWASP A03)
# Hive-style partition paths (extraction_date=2026-06-29) require '=' in the allowed set.
_SAFE_S3_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9\-_/=]{0,511}$"
)
# Domain must be a lowercase safe identifier suitable for Glue table name construction (OWASP A03)
_SAFE_DOMAIN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Max prefix segment length to prevent S3 path traversal (OWASP A03)
_MAX_PREFIX_SEGMENT_LEN: Final[int] = 256


@dataclass(frozen=True)
class TransformationContext:
    """Input parameters for one transformation pipeline run."""

    run_id: str
    source_id: str
    entity_id: str
    domain: str
    raw_s3_bucket: str
    raw_s3_prefix: str
    mapping_bucket: str
    curated_s3_bucket: str
    region_name: str
    mapping_version: str = "latest"
    curated_date: date | None = None
    # Optional: governance bucket for lineage + catalog registration
    governance_s3_bucket: str | None = None
    glue_catalog_database: str | None = None
    environment: str = "dev"

    def __post_init__(self) -> None:
        # Validate domain before it is used in Glue table name construction (OWASP A03 / F06)
        if not _SAFE_DOMAIN_PATTERN.match(self.domain):
            raise ValueError(
                f"domain {self.domain!r} must match pattern '^[a-z][a-z0-9_]{{0,63}}$'; "
                "dots, hyphens, and uppercase are not permitted."
            )
        # Validate raw_s3_prefix to prevent path traversal (OWASP A03 / F05)
        if ".." in self.raw_s3_prefix or self.raw_s3_prefix.startswith("/"):
            raise ValueError(
                f"raw_s3_prefix {self.raw_s3_prefix!r} contains invalid path components."
            )
        if not _SAFE_S3_PREFIX_PATTERN.match(self.raw_s3_prefix):
            raise ValueError(
                f"raw_s3_prefix {self.raw_s3_prefix!r} contains characters not permitted "
                "in an S3 prefix."
            )


@dataclass(frozen=True)
class TransformationResult:
    """Immutable result of a transformation pipeline run."""

    run_id: str
    source_id: str
    entity_id: str
    raw_record_count: int
    canonical_record_count: int
    mapping_failures: int
    curated_s3_prefix: str | None
    quality_report_s3_key: str | None
    is_publication_blocked: bool
    mapping_version: str
    started_at: str  # ISO-8601 UTC
    completed_at: str  # ISO-8601 UTC


class TransformationPipeline:
    """
    Orchestrates the end-to-end transformation from raw Parquet to curated layer.

    One instance may be reused for multiple runs within the same Lambda warm
    invocation.
    """

    def __init__(
        self,
        mapping_registry_client: FieldMappingRegistryClient,
        quality_evaluator: QualityPolicyEvaluator,
        curated_writer: CuratedLayerWriter,
        quality_policy: QualityPolicy | None,
        classification_policy: EntityClassificationPolicy | None = None,
        metrics_emitter: CloudWatchMetricsEmitter | None = None,
    ) -> None:
        self._mapping_registry = mapping_registry_client
        self._quality_evaluator = quality_evaluator
        self._curated_writer = curated_writer
        self._quality_policy = quality_policy
        self._classification_policy = classification_policy
        self._metrics_emitter = metrics_emitter

    def execute(self, ctx: TransformationContext) -> TransformationResult:
        """Execute the full transformation pipeline for one extraction run."""
        started_at = datetime.now(UTC).isoformat()
        _logger.info(
            "transformation_pipeline_started",
            run_id=ctx.run_id,
            source_id=ctx.source_id,
            entity_id=ctx.entity_id,
        )

        s3: Any = boto3.client("s3", region_name=ctx.region_name)

        raw_records = _load_raw_records(s3, ctx.raw_s3_bucket, ctx.raw_s3_prefix)
        _logger.info("raw_records_loaded", count=len(raw_records), run_id=ctx.run_id)

        # Load mapping rule set (graceful degradation: identity if absent)
        rule_set: FieldMappingRuleSet | None = None
        try:
            rule_set = self._mapping_registry.load_rule_set(
                ctx.source_id, ctx.entity_id, ctx.mapping_version
            )
        except MappingRuleSetNotFoundError:
            _logger.warning(
                "no_mapping_rule_set_found_using_identity",
                source_id=ctx.source_id,
                entity_id=ctx.entity_id,
            )

        canonical_records, mapping_failures = _apply_mappings(
            raw_records, rule_set, FieldMappingApplicator()
        )
        mapping_version = rule_set.mapping_version if rule_set else "identity"

        # Apply data classification masking before any write (OWASP A04, spec §6.4)
        if self._classification_policy is not None and canonical_records:
            canonical_records = FieldMaskingApplier().apply(
                canonical_records, self._classification_policy
            )

        # Quality evaluation
        curated_prefix: str | None = None
        quality_report_key: str | None = None
        is_blocked = False
        quality_report: QualityReport | None = None

        if self._quality_policy is not None and canonical_records:
            quality_report = self._quality_evaluator.evaluate(
                canonical_records, self._quality_policy, ctx.run_id
            )
            quality_report_key = _write_quality_report(s3, ctx.mapping_bucket, ctx, quality_report)
            is_blocked = quality_report.is_publication_blocked

        # Write curated layer (only when not blocked and records exist)
        if not is_blocked and canonical_records:
            write_result = self._curated_writer.write(
                records=canonical_records,
                domain=ctx.domain,
                entity_id=ctx.entity_id,
                run_id=ctx.run_id,
                curated_date=ctx.curated_date,
            )
            curated_prefix = write_result.s3_prefix

            # Register curated dataset in Glue Data Catalog (spec §6.4 AC)
            if ctx.glue_catalog_database:
                _register_curated_catalog(
                    ctx=ctx,
                    s3_prefix=curated_prefix,
                    record_count=len(canonical_records),
                    raw_s3_prefix=ctx.raw_s3_prefix,
                )

        completed_at = datetime.now(UTC).isoformat()

        result = TransformationResult(
            run_id=ctx.run_id,
            source_id=ctx.source_id,
            entity_id=ctx.entity_id,
            raw_record_count=len(raw_records),
            canonical_record_count=len(canonical_records),
            mapping_failures=mapping_failures,
            curated_s3_prefix=curated_prefix,
            quality_report_s3_key=quality_report_key,
            is_publication_blocked=is_blocked,
            mapping_version=mapping_version,
            started_at=started_at,
            completed_at=completed_at,
        )

        _logger.info(
            "transformation_pipeline_complete",
            run_id=ctx.run_id,
            source_id=ctx.source_id,
            entity_id=ctx.entity_id,
            raw_records=len(raw_records),
            canonical_records=len(canonical_records),
            mapping_failures=mapping_failures,
            is_publication_blocked=is_blocked,
        )

        # Emit CloudWatch metrics (spec §6.3)
        if self._metrics_emitter is not None:
            _emit_transformation_metrics(
                emitter=self._metrics_emitter,
                ctx=ctx,
                result=result,
                quality_report=quality_report,
            )

        # Capture lineage record (spec §9.1)
        if ctx.governance_s3_bucket and curated_prefix:
            _emit_transformation_lineage(ctx=ctx, curated_prefix=curated_prefix)

        return result


# ---------------------------------------------------------------------------
# Module-level helpers (pure functions; no class state)
# ---------------------------------------------------------------------------


def _iter_raw_records(
    s3: Any, bucket: str, raw_s3_prefix: str
) -> Iterator[dict[str, Any]]:
    """Yield raw records one by one from all Parquet files under raw_s3_prefix.

    Files are read and yielded sequentially — only one file is held in memory
    at a time.  This avoids materialising the entire raw dataset into RAM (F-05).
    """
    # Enforce prefix safety at the point of use as a defence-in-depth measure;
    # TransformationContext.__post_init__ is the primary gate (OWASP A03 / F05).
    if ".." in raw_s3_prefix or raw_s3_prefix.startswith("/"):
        raise ValueError(f"Unsafe raw_s3_prefix rejected: {raw_s3_prefix!r}")
    if not _SAFE_S3_PREFIX_PATTERN.match(raw_s3_prefix):
        raise ValueError(f"raw_s3_prefix {raw_s3_prefix!r} contains disallowed characters.")
    prefix = raw_s3_prefix.rstrip("/") + "/"
    paginator = s3.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".parquet"):
                continue
            raw_data = s3.get_object(Bucket=bucket, Key=obj["Key"])
            buf = io.BytesIO(raw_data["Body"].read())
            table = pq.read_table(buf)  # type: ignore[no-untyped-call]
            yield from _table_to_records(table)
            del table  # release memory before reading next file


def _load_raw_records(s3: Any, bucket: str, raw_s3_prefix: str) -> list[dict[str, Any]]:
    """List all Parquet files under raw_s3_prefix and return flat record list."""
    return list(_iter_raw_records(s3, bucket, raw_s3_prefix))


def _apply_mappings(
    raw_records: list[dict[str, Any]],
    rule_set: FieldMappingRuleSet | None,
    applicator: FieldMappingApplicator,
) -> tuple[list[dict[str, Any]], int]:
    """Apply mapping rule set; returns (canonical_records, failure_count)."""
    if rule_set is None:
        return list(raw_records), 0

    canonical: list[dict[str, Any]] = []
    failures = 0

    for record in raw_records:
        result = applicator.apply(record, rule_set)
        if result is None:
            failures += 1
        else:
            canonical.append(result)

    return canonical, failures


def _write_quality_report(
    s3: Any,
    mapping_bucket: str,
    ctx: TransformationContext,
    report: QualityReport,
) -> str:
    """Persist quality report JSON to the mapping bucket; returns S3 key."""
    key = f"quality-reports/{ctx.source_id}/{ctx.entity_id}/{ctx.run_id}/quality-report.json"
    payload: dict[str, Any] = {
        "run_id": report.run_id,
        "source_id": report.source_id,
        "entity_id": report.entity_id,
        "total_records": report.total_records,
        "records_passed": report.records_passed,
        "records_with_warnings": report.records_with_warnings,
        "records_blocked": report.records_blocked,
        "is_publication_blocked": report.is_publication_blocked,
        "violation_count": len(report.violations),
        "violations": [
            {
                "field_name": v.field_name,
                "check_kind": v.check_kind.value,
                "severity": v.severity.value,
                "record_index": v.record_index,
            }
            for v in report.violations
        ],
    }
    s3.put_object(
        Bucket=mapping_bucket,
        Key=key,
        Body=json.dumps(payload).encode("utf-8"),
        ContentType="application/json",
    )
    return key


def _table_to_records(table: Any) -> list[dict[str, Any]]:
    """Convert a pyarrow Table to a list of row dicts."""
    py_dict: dict[str, list[Any]] = table.to_pydict()
    if not py_dict:
        return []
    columns = list(py_dict.keys())
    row_count = len(py_dict[columns[0]])
    return [{col: py_dict[col][i] for col in columns} for i in range(row_count)]


def _emit_transformation_metrics(
    emitter: CloudWatchMetricsEmitter,
    ctx: TransformationContext,
    result: TransformationResult,
    quality_report: QualityReport | None,
) -> None:
    """Emit canonical CloudWatch metrics for a transformation run (spec §6.3)."""
    emitter.emit_records_extracted(
        source_id=ctx.source_id,
        entity_id=ctx.entity_id,
        environment=ctx.environment,
        count=result.canonical_record_count,
    )
    emitter.emit_records_failed(
        source_id=ctx.source_id,
        entity_id=ctx.entity_id,
        environment=ctx.environment,
        count=result.mapping_failures,
    )
    if quality_report is not None:
        # Emit quality blocking violations as "failed" records
        emitter.emit_records_failed(
            source_id=ctx.source_id,
            entity_id=ctx.entity_id,
            environment=ctx.environment,
            count=quality_report.records_blocked,
        )


def _register_curated_catalog(
    ctx: TransformationContext,
    s3_prefix: str,
    record_count: int,
    raw_s3_prefix: str,
) -> None:
    """Register the curated dataset in Glue Data Catalog (spec §6.4 AC)."""
    if not ctx.glue_catalog_database:
        return
    table_name = f"{ctx.entity_id.replace('-', '_')}_{ctx.domain}_curated"
    # Truncate to Glue max table name length (255) and enforce safe chars
    table_name = table_name[:128]
    spec = CatalogDatasetSpec(
        database_name=ctx.glue_catalog_database,
        table_name=table_name,
        s3_location=f"s3://{ctx.curated_s3_bucket}/{s3_prefix}",
        data_layer=DataLayer.CURATED,
        owner=ctx.source_id,
        data_classification="internal",
        retention_days=365,
        source_lineage=(f"s3://{ctx.raw_s3_bucket}/{raw_s3_prefix}",),
        partition_keys=("curated_date",),
        description=f"Curated {ctx.entity_id} records from {ctx.source_id}",
    )
    try:
        client = DataCatalogRegistrationClient(region_name=ctx.region_name)
        client.register_dataset(spec)
        _logger.info(
            "curated_catalog_registered",
            run_id=ctx.run_id,
            table_name=table_name,
            database=ctx.glue_catalog_database,
        )
    except Exception as exc:
        # Catalog registration failure must not block curated write
        _logger.warning(
            "curated_catalog_registration_failed",
            run_id=ctx.run_id,
            error=str(exc),
        )


def _emit_transformation_lineage(
    ctx: TransformationContext,
    curated_prefix: str,
) -> None:
    """Persist a TRANSFORMATION lineage record to the governance bucket."""
    if not ctx.governance_s3_bucket:
        return
    try:
        record = build_transformation_lineage(
            run_id=ctx.run_id,
            source_id=ctx.source_id,
            entity_id=ctx.entity_id,
            raw_s3_bucket=ctx.raw_s3_bucket,
            raw_s3_prefix=ctx.raw_s3_prefix,
            curated_s3_bucket=ctx.curated_s3_bucket,
            curated_s3_prefix=curated_prefix,
            record_count=0,
            mapping_version=ctx.mapping_version,
        )
        emitter = LineageEmitter(
            governance_s3_bucket=ctx.governance_s3_bucket,
            region_name=ctx.region_name,
        )
        emitter.emit(record)
    except Exception as exc:
        # Lineage failure must never block pipeline output
        _logger.warning(
            "transformation_lineage_emission_failed",
            run_id=ctx.run_id,
            error=str(exc),
        )
