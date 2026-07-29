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

import itertools
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final

import boto3

from contracts.platform_metrics import PlatformMetric
from data_quality.batch_quality_gate import (
    QualityGateBlockedError,
    persist_record_violations,
    run_batch_quality_gate,
)
from governance.data_catalog_registration import (
    CatalogDatasetSpec,
    DataCatalogRegistrationClient,
    DataLayer,
)
from governance.data_classification_policy import (
    EntityClassificationPolicy,
    FieldMaskingApplier,
    build_auto_classification_policy,
)
from governance.lineage_record import (
    LineageEmitter,
    build_transformation_lineage,
)
from observability.lambda_runtime import check_lambda_timeout_periodic as _check_timeout
from observability.metric_recorder import record_platform_metric
from observability.metrics_emitter import CloudWatchMetricsEmitter
from observability.structured_logger import get_platform_logger
from persistence.parquet_reader import iter_parquet_records
from tenancy.scope_attribution import ScopeAttributor
from tenancy.scope_contract import PartitionModel, TenantPartitionProfile
from tenancy.source_connection import SourceConnection
from transformation.curated_accumulator import CuratedAccumulator
from transformation.curated_layer_reader import SAFE_S3_PREFIX_PATTERN as _SAFE_S3_PREFIX_PATTERN
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

# _SAFE_S3_PREFIX_PATTERN imported from curated_layer_reader — single definition, no duplication.
# Domain must be a lowercase safe identifier suitable for Glue table name construction (OWASP A03)
_SAFE_DOMAIN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Max prefix segment length to prevent S3 path traversal (OWASP A03)
_MAX_PREFIX_SEGMENT_LEN: Final[int] = 256
# Extracts the curated_date=YYYY-MM-DD partition value from a curated S3
# prefix, so Glue partition registration always reflects the exact date the
# curated writer used rather than re-deriving it independently.
_CURATED_DATE_PARTITION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"curated_date=(\d{4}-\d{2}-\d{2})"
)


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
    # Multi-tenancy (§1.1): tenant slug prefixed to all curated S3 paths.
    # Default "demo" preserves backward-compat with single-tenant dev pipelines.
    tenant_code: str = "demo"
    # Optional Lambda context for mid-execution timeout checks (§3.5).
    # When set, the pipeline checks remaining time before the curated write.
    # None = no periodic checks (safe; pre-execution check still applies).
    lambda_context: Any | None = None
    # Scope attribution (DL-SCOPE-07). Both must be present for a partitioned tenant: the
    # connection says which unit owns the rows, the profile says whether units exist at all.
    # Absent for a `single` tenant, where every row belongs to the one implicit unit.
    source_connection: SourceConnection | None = None
    partition_profile: TenantPartitionProfile | None = None
    known_scope_unit_ids: frozenset[str] | None = None

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
        # Validate tenant_code (OWASP A03 / §1.1)
        from contracts.identifier_policy import (
            TENANT_CODE_PATTERN as _TC_PATTERN,  # local import to avoid circular
        )

        if not _TC_PATTERN.match(self.tenant_code):
            raise ValueError(
                f"tenant_code {self.tenant_code!r} does not conform to the tenant code format."
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


class TransformationPipelineError(Exception):
    """Raised when a transformation run cannot proceed without producing unusable output."""


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
        curated_accumulator: CuratedAccumulator | None = None,
    ) -> None:
        self._mapping_registry = mapping_registry_client
        self._quality_evaluator = quality_evaluator
        self._curated_writer = curated_writer
        self._quality_policy = quality_policy
        self._classification_policy = classification_policy
        self._metrics_emitter = metrics_emitter
        # Optional — injected only for incremental entities with primary_key_field set.
        # When None, pipeline behaviour is identical to the original (no merge).
        self._curated_accumulator = curated_accumulator

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

        # Load mapping rule set before streaming so the same rule_set instance
        # is reused for every record without re-fetching from S3 (graceful
        # degradation: identity pass-through when absent).
        rule_set: FieldMappingRuleSet | None = None
        try:
            rule_set = self._mapping_registry.load_rule_set(
                ctx.source_id, ctx.entity_id, ctx.tenant_code, ctx.mapping_version
            )
        except MappingRuleSetNotFoundError as exc:
            _logger.warning(
                "no_mapping_rule_set_found_using_identity",
                source_id=ctx.source_id,
                entity_id=ctx.entity_id,
                tenant_code=ctx.tenant_code,
                detail=str(exc),
            )

        mapping_version = rule_set.mapping_version if rule_set else "identity"
        applicator = FieldMappingApplicator()

        # Single lazy iterator over raw Parquet records, created once so both
        # the classification peek below and the actual read (streaming or
        # list) consume the same underlying S3 reads exactly once.
        raw_records_iter: Iterator[dict[str, Any]] = _iter_raw_records(
            s3, ctx.raw_s3_bucket, ctx.raw_s3_prefix
        )

        # ── Data classification (OWASP A01 / spec §6.4) ──────────────────────
        # An explicit, data-steward-reviewed policy always wins. Absent one,
        # auto-classify from the mapped (canonical) field names using
        # name-pattern heuristics — this is a best-effort safety net, not a
        # substitute for a reviewed policy, but it ensures PII/SENSITIVE_PII
        # fields are never written unmasked purely because no policy exists
        # yet for this entity. Computed per-run (never cached on `self`) since
        # one pipeline instance may be reused across entities in a warm Lambda.
        effective_classification_policy = self._classification_policy
        if effective_classification_policy is None:
            if rule_set is not None:
                candidate_fields = [rule.canonical_field for rule in rule_set.rules]
                if candidate_fields:
                    effective_classification_policy = build_auto_classification_policy(
                        source_id=ctx.source_id,
                        entity_id=ctx.entity_id,
                        field_names=candidate_fields,
                    )
            else:
                # No mapping rule set (identity pass-through) means canonical
                # field names equal raw field names, which are unknown until
                # raw records are read. Peek the first raw record's field
                # names and auto-classify from those — pass-through entities
                # must never bypass PII protection purely because no rule set
                # was registered (OWASP A01). The peeked record is restored to
                # the front of the iterator so no data is lost.
                effective_classification_policy, raw_records_iter = _classify_pass_through_entity(
                    raw_records_iter=raw_records_iter,
                    source_id=ctx.source_id,
                    entity_id=ctx.entity_id,
                )
                _logger.info(
                    "classification_auto_detect_from_raw_fields",
                    source_id=ctx.source_id,
                    entity_id=ctx.entity_id,
                    masking_required=effective_classification_policy is not None,
                )

        # ── Fast path: no quality policy, no masking, no SCD merge ───────────
        # When all features that require an in-memory list are absent, stream
        # records directly from raw Parquet → mapping → curated writer.
        # Peak memory is O(write_batch_size) regardless of total record count.
        can_stream = (
            self._quality_policy is None
            and effective_classification_policy is None
            and self._curated_accumulator is None
        )

        if can_stream:
            return self._execute_streaming(
                ctx=ctx,
                raw_records_iter=raw_records_iter,
                rule_set=rule_set,
                applicator=applicator,
                mapping_version=mapping_version,
                started_at=started_at,
            )

        # ── Standard path: quality / masking / SCD merge requires full list ──
        return self._execute_with_list(
            ctx=ctx,
            s3=s3,
            raw_records_iter=raw_records_iter,
            rule_set=rule_set,
            applicator=applicator,
            mapping_version=mapping_version,
            started_at=started_at,
            classification_policy=effective_classification_policy,
        )

    def _execute_streaming(
        self,
        ctx: TransformationContext,
        raw_records_iter: Iterator[dict[str, Any]],
        rule_set: FieldMappingRuleSet | None,
        applicator: FieldMappingApplicator,
        mapping_version: str,
        started_at: str,
    ) -> TransformationResult:
        """
        Streaming execution path (§3.2): no list materialisation.

        Used when no quality policy, no masking, and no SCD accumulator are
        configured.  Peak memory is O(writer_batch_size) = ~20 MB.
        """
        _streaming_failures = 0

        def _mapped_iter() -> Iterator[dict[str, Any]]:
            for raw_record in raw_records_iter:
                if rule_set is None:
                    yield raw_record
                else:
                    mapped = applicator.apply(raw_record, rule_set)
                    if mapped is not None:
                        yield mapped
                    else:
                        nonlocal _streaming_failures
                        _streaming_failures += 1

        # Pre-write timeout guard: if Lambda has < 120s remaining, abort before
        # starting the potentially large S3 multipart write (§3.5 / graceful shutdown).
        _check_timeout(
            ctx.lambda_context, min_remaining_ms=120_000, operation_name="curated_streaming_write"
        )

        write_result = self._curated_writer.write_streaming(
            records_iter=_mapped_iter(),
            domain=ctx.domain,
            entity_id=ctx.entity_id,
            run_id=ctx.run_id,
            curated_date=ctx.curated_date,
            tenant_code=ctx.tenant_code,
        )

        curated_prefix = write_result.s3_prefix if write_result.record_count > 0 else None
        canonical_count = write_result.record_count
        mapping_failures = _streaming_failures

        _logger.info(
            "transformation_streaming_complete",
            run_id=ctx.run_id,
            source_id=ctx.source_id,
            entity_id=ctx.entity_id,
            canonical_records=canonical_count,
            curated_prefix=curated_prefix,
        )

        if ctx.glue_catalog_database and curated_prefix:
            _register_curated_catalog(
                ctx=ctx,
                s3_prefix=curated_prefix,
                record_count=canonical_count,
                raw_s3_prefix=ctx.raw_s3_prefix,
            )

        completed_at = datetime.now(UTC).isoformat()

        result = TransformationResult(
            run_id=ctx.run_id,
            source_id=ctx.source_id,
            entity_id=ctx.entity_id,
            raw_record_count=canonical_count,  # approximation in streaming mode
            canonical_record_count=canonical_count,
            mapping_failures=mapping_failures,
            curated_s3_prefix=curated_prefix,
            quality_report_s3_key=None,
            is_publication_blocked=False,
            mapping_version=mapping_version,
            started_at=started_at,
            completed_at=completed_at,
        )

        if self._metrics_emitter is not None:
            _emit_transformation_metrics(
                emitter=self._metrics_emitter,
                ctx=ctx,
                result=result,
                quality_report=None,
            )

        return result

    @staticmethod
    def _map_raw_records(
        raw_records_iter: Iterator[dict[str, Any]],
        rule_set: FieldMappingRuleSet | None,
        applicator: FieldMappingApplicator,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """
        Stream raw records through the mapping applicator.

        Returns (canonical_records, mapping_failures, raw_record_count). Only
        canonical records (post-mapping) are accumulated — peak memory is
        O(canonical) rather than O(raw + canonical).
        """
        canonical_records: list[dict[str, Any]] = []
        mapping_failures = 0
        raw_record_count = 0

        for raw_record in raw_records_iter:
            raw_record_count += 1
            if rule_set is None:
                canonical_records.append(raw_record)
            else:
                mapped = applicator.apply(raw_record, rule_set)
                if mapped is None:
                    mapping_failures += 1
                else:
                    canonical_records.append(mapped)

        return canonical_records, mapping_failures, raw_record_count

    def _batch_gate_blocks(
        self, ctx: TransformationContext, canonical_records: list[dict[str, Any]]
    ) -> bool:
        """
        Run the batch quality gate; True when its attachment blocks publication.

        Blocking here matches how a field-level block behaves — publication stops, the stage
        succeeds, and the evidence is in the exception store — rather than failing the stage,
        because a quality decision is not an infrastructure failure.
        """
        try:
            batch_result = run_batch_quality_gate(
                records=canonical_records,
                tenant_code=ctx.tenant_code,
                entity_id=ctx.entity_id,
                run_id=ctx.run_id,
                correlation_id=ctx.run_id,
                environment=ctx.environment,
                region_name=ctx.region_name,
                source_id=ctx.source_id,
            )
        except QualityGateBlockedError as exc:
            _logger.warning(
                "transformation_blocked_by_batch_quality_gate",
                run_id=ctx.run_id,
                entity_id=ctx.entity_id,
                reason=str(exc),
            )
            return True
        if batch_result is not None and not batch_result.all_passed:
            _logger.info(
                "batch_quality_gate_observed_failures",
                run_id=ctx.run_id,
                entity_id=ctx.entity_id,
                failed_rules=[o.rule_id for o in batch_result.outcomes if not o.passed],
            )
        return False

    def _attribute_scope(
        self, ctx: TransformationContext, records: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        Stamp `scope_unit_id` on every curated record (DL-SCOPE-07).

        This is the only place the column is written, and every downstream row filter — semantic
        queries, serving-store views, exports, the twin — filters on it. An unstamped curated
        layer makes all of them inert, which is why a partitioned tenant with no connection
        context fails the stage rather than writing rows nobody can scope.
        """
        if ctx.partition_profile is None:
            # The profile is published configuration; its absence is a wiring defect, not a data
            # condition. Defaulting to `single` here would be the fail-open this exists to
            # prevent: a partitioned tenant whose profile failed to load would stamp every row
            # with the implicit unit, which the predicate treats as match-all.
            raise TransformationPipelineError(
                f"Entity {ctx.entity_id!r} of tenant {ctx.tenant_code!r} has no partition "
                "profile, so `scope_unit_id` cannot be attributed. Curated rows without it "
                "cannot be filtered by any consumption surface (DL-SCOPE-07)."
            )

        attributor = ScopeAttributor(
            connection=ctx.source_connection,
            profile=ctx.partition_profile,
            known_scope_unit_ids=ctx.known_scope_unit_ids,
        )
        stamped = list(attributor.stamp_all(records))
        attributor.log_outcome(ctx.entity_id)
        record_platform_metric(
            PlatformMetric.SCOPE_ATTRIBUTION_APPLIED, 1.0, EntityId=ctx.entity_id
        )

        if (
            ctx.partition_profile.partition_model is PartitionModel.PARTITIONED
            and attributor.outcome.exceeds()
        ):
            # Above the declared threshold the rows are not merely imperfect: a unit-scoped
            # caller sees none of them (NULL fails closed), so the entity silently disappears
            # from every franchisee's view. Fail the run instead.
            raise TransformationPipelineError(
                f"Entity {ctx.entity_id!r}: "
                f"{attributor.outcome.unattributed_rate_pct:.1f}% of rows could not be "
                "attributed to a scope unit, above the declared threshold. Unattributed rows "
                "are invisible to every unit-scoped caller (DL-SCOPE-02 fails closed)."
            )
        return stamped

    def _write_curated_and_register(
        self,
        ctx: TransformationContext,
        records_to_write: list[dict[str, Any]],
        canonical_record_count: int,
    ) -> str | None:
        """
        Write the curated layer and register the Glue catalog dataset (spec §6.4 AC).

        Returns the curated S3 prefix, or None if there was nothing to write.
        """
        # Pre-write timeout guard (§3.5): abort before large S3 write if < 120s remain.
        _check_timeout(ctx.lambda_context, min_remaining_ms=120_000, operation_name="curated_write")
        attributed = self._attribute_scope(ctx, records_to_write)
        write_result = self._curated_writer.write(
            records=attributed,
            domain=ctx.domain,
            entity_id=ctx.entity_id,
            run_id=ctx.run_id,
            curated_date=ctx.curated_date,
            tenant_code=ctx.tenant_code,
        )
        curated_prefix = write_result.s3_prefix

        if ctx.glue_catalog_database:
            _register_curated_catalog(
                ctx=ctx,
                s3_prefix=curated_prefix,
                record_count=canonical_record_count,
                raw_s3_prefix=ctx.raw_s3_prefix,
            )

        return curated_prefix

    def _execute_with_list(
        self,
        ctx: TransformationContext,
        s3: Any,
        raw_records_iter: Iterator[dict[str, Any]],
        rule_set: FieldMappingRuleSet | None,
        applicator: FieldMappingApplicator,
        mapping_version: str,
        started_at: str,
        classification_policy: EntityClassificationPolicy | None = None,
    ) -> TransformationResult:
        """
        Standard execution path: accumulates canonical records for quality/masking/merge.

        Used when quality policy, masking, or SCD accumulator is configured.
        Peak memory is O(delta_records) — acceptable for incremental deltas;
        full-load entities with these features require sufficient Lambda memory.
        """
        # Stream raw Parquet records through the mapping applicator without
        # materialising the full raw dataset in memory.
        canonical_records, mapping_failures, raw_record_count = self._map_raw_records(
            raw_records_iter, rule_set, applicator
        )

        _logger.info("raw_records_streamed", count=raw_record_count, run_id=ctx.run_id)

        # Apply data classification masking before any write (OWASP A04, spec §6.4)
        if classification_policy is not None and canonical_records:
            canonical_records = FieldMaskingApplier().apply(
                canonical_records, classification_policy
            )

        # Quality evaluation — runs on the delta (today's extracted records only).
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
            # DL-DQ-14: the report is an artefact in S3; the exception store is what an operator
            # triages from. Writing only the report left every violation invisible to the console.
            persist_record_violations(
                violations=quality_report.violations,
                tenant_code=ctx.tenant_code,
                entity_id=ctx.entity_id,
                run_id=ctx.run_id,
                correlation_id=ctx.run_id,
                environment=ctx.environment,
                region_name=ctx.region_name,
            )

        # Batch-level gate (DL-DQ-01..04): field checks pass record by record, but a batch can
        # still be 40% incomplete or 5% duplicated.
        if canonical_records:
            is_blocked = self._batch_gate_blocks(ctx, canonical_records) or is_blocked

        # SCD Type 1 merge — only active when a CuratedAccumulator is injected.
        records_to_write = canonical_records
        if not is_blocked and canonical_records and self._curated_accumulator is not None:
            acc_result = self._curated_accumulator.accumulate(
                delta_records=canonical_records,
                domain=ctx.domain,
                entity_id=ctx.entity_id,
                run_id=ctx.run_id,
            )
            records_to_write = acc_result.merged_records

        # Write curated layer (only when not blocked and records exist)
        if not is_blocked and records_to_write:
            curated_prefix = self._write_curated_and_register(
                ctx, records_to_write, len(canonical_records)
            )

        completed_at = datetime.now(UTC).isoformat()

        result = TransformationResult(
            run_id=ctx.run_id,
            source_id=ctx.source_id,
            entity_id=ctx.entity_id,
            raw_record_count=raw_record_count,
            canonical_record_count=len(records_to_write),
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
            raw_records=raw_record_count,
            delta_records=len(canonical_records),
            canonical_records=len(records_to_write),
            mapping_failures=mapping_failures,
            is_publication_blocked=is_blocked,
            accumulator_active=self._curated_accumulator is not None,
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


def _iter_raw_records_batched(
    s3: Any, bucket: str, raw_s3_prefix: str, batch_size: int = 10_000
) -> Iterator[dict[str, Any]]:
    """
    Yield raw records one at a time from every Parquet file under `raw_s3_prefix`.

    Delegates the read to `persistence.parquet_reader`, which is the one implementation — this was
    the third near-identical copy (`analytics_publisher` materialised, `serving_store` batched,
    this one streamed), and only two of the three bounded their memory. The prefix validation stays
    here because it encodes this layer's own naming rules.

    Peak memory is one row group, not the file. Note that `_load_raw_records` below still
    materialises for the batch quality gate — see `requirements/WAIVERS.md`: completeness and
    duplicate *rates* are properties of the whole batch, so that path cannot stream without giving
    up the gate.
    """
    if ".." in raw_s3_prefix or raw_s3_prefix.startswith("/"):
        raise ValueError(f"Unsafe raw_s3_prefix rejected: {raw_s3_prefix!r}")
    if not _SAFE_S3_PREFIX_PATTERN.match(raw_s3_prefix):
        raise ValueError(f"raw_s3_prefix {raw_s3_prefix!r} contains disallowed characters.")
    return iter_parquet_records(s3, bucket, raw_s3_prefix, batch_size=batch_size)


def _iter_raw_records(s3: Any, bucket: str, raw_s3_prefix: str) -> Iterator[dict[str, Any]]:
    """Backward-compatible alias for _iter_raw_records_batched."""
    return _iter_raw_records_batched(s3, bucket, raw_s3_prefix)


def _classify_pass_through_entity(
    raw_records_iter: Iterator[dict[str, Any]],
    source_id: str,
    entity_id: str,
) -> tuple[EntityClassificationPolicy | None, Iterator[dict[str, Any]]]:
    """
    Auto-classify a pass-through entity (no field-mapping rule set) from the
    field names of its own first raw record.

    Without a registered rule set, canonical field names equal raw field
    names, and those are unknown until raw records are read — so the
    heuristic classification build_auto_classification_policy() normally
    runs on cannot be computed ahead of time. This peeks exactly one record
    off `raw_records_iter` to enumerate its fields, then restores that record
    to the front of the returned iterator so the caller sees every record
    exactly once (OWASP A01 — a pass-through entity must never skip PII
    masking purely because no mapping rule set was registered).

    Returns (None, raw_records_iter unchanged) when the raw prefix is empty —
    there is nothing to classify and nothing to mask.
    """
    try:
        first_record = next(raw_records_iter)
    except StopIteration:
        return None, raw_records_iter

    policy = build_auto_classification_policy(
        source_id=source_id,
        entity_id=entity_id,
        field_names=list(first_record.keys()),
    )
    restored_iter = itertools.chain([first_record], raw_records_iter)
    return policy, restored_iter


def _load_raw_records(s3: Any, bucket: str, raw_s3_prefix: str) -> list[dict[str, Any]]:
    """
    Materialise the raw prefix. Required by the batch quality gate, not an oversight.

    Completeness and duplicate *rates* are properties of the whole batch (DL-DQ-01..04), so this
    path cannot stream without giving up the gate — the trade is recorded in
    `requirements/WAIVERS.md` rather than left implicit. Callers that do not need the batch gate
    should use `_iter_raw_records_batched`, which never holds more than one row group.
    """
    return list(_iter_raw_records_batched(s3, bucket, raw_s3_prefix))


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
    # DL-SEC-04 (gap 10): tenant-prefixed so the key is IAM-enforceable like every other layer.
    key = (
        f"{ctx.tenant_code}/quality-reports/{ctx.source_id}/{ctx.entity_id}/"
        f"{ctx.run_id}/quality-report.json"
    )
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
    """Convert a pyarrow Table to a list of row dicts.

    Uses RecordBatch iteration (max_chunksize=10_000) so at most 10K rows
    are materialised in Python heap at a time rather than the full table.
    Peak memory: O(max_chunksize) per file, not O(total_rows).
    """
    records: list[dict[str, Any]] = []
    batch_size = 10_000
    for batch in table.to_batches(max_chunksize=batch_size):
        batch_dict: dict[str, list[Any]] = batch.to_pydict()
        n = batch.num_rows
        cols = list(batch_dict.keys())
        records.extend({col: batch_dict[col][i] for col in cols} for i in range(n))
    return records


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
        stage="transformation",
    )
    emitter.emit_records_failed(
        source_id=ctx.source_id,
        entity_id=ctx.entity_id,
        environment=ctx.environment,
        count=result.mapping_failures,
        stage="transformation",
    )
    if quality_report is not None:
        # Emit quality blocking violations as "failed" records
        emitter.emit_records_failed(
            source_id=ctx.source_id,
            entity_id=ctx.entity_id,
            environment=ctx.environment,
            count=quality_report.records_blocked,
            stage="transformation",
        )


def _extract_curated_date_partition(s3_prefix: str) -> str:
    """
    Extract the curated_date=YYYY-MM-DD partition value from a curated S3
    prefix produced by CuratedLayerWriter.

    Parsing it from the actual written prefix (rather than recomputing
    `ctx.curated_date or datetime.now(UTC).date()` independently) guarantees
    the registered Glue partition always matches the date the data was
    actually written under, even if this function runs a moment after
    midnight UTC relative to the write.  Falls back to today's UTC date if
    the prefix is unexpectedly malformed; catalog registration failures are
    swallowed by the caller and must never block the curated write itself.
    """
    match = _CURATED_DATE_PARTITION_PATTERN.search(s3_prefix)
    if match:
        return match.group(1)
    return datetime.now(UTC).date().isoformat()


def _register_curated_catalog(
    ctx: TransformationContext,
    s3_prefix: str,
    record_count: int,
    raw_s3_prefix: str,
) -> None:
    """Register the curated dataset in Glue Data Catalog (spec §6.4 AC)."""
    if not ctx.glue_catalog_database:
        return
    # Tenant-scoped Glue table name (OWASP A01 — broken access control). The
    # curated S3 *location* is already tenant-scoped (tenant_code prefix from
    # CuratedLayerWriter), but without a matching tenant_code prefix on the
    # *table name*, two tenants running the same entity_id/domain register
    # the SAME table in the shared edl_curated database — the second
    # tenant's register_dataset() call silently overwrites the first
    # tenant's table Location, so an Athena query against that table then
    # returns the other tenant's rows. Mirrors the analytics publisher's
    # tenant-scoped table naming (analytics_publisher_handler.py:322).
    table_name = (
        f"{ctx.tenant_code.replace('-', '_')}_"
        f"{ctx.entity_id.replace('-', '_')}_{ctx.domain}_curated"
    )
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

        # Register this run's curated_date partition so Athena can query the
        # newly-written data immediately, without a manual MSCK REPAIR TABLE.
        # Mirrors the analytics publisher's per-run create_partition call
        # (analytics_publisher_handler.py:357-375). Without this, the table
        # declared partition_keys=("curated_date",) but no partition value
        # was ever registered against it, so any partitioned Athena query
        # (including the implicit ones most BI tools issue) returns zero
        # rows even though the curated Parquet data exists in S3.
        partition_value = _extract_curated_date_partition(s3_prefix)
        glue_client = boto3.client("glue", region_name=ctx.region_name)
        glue_table_meta = glue_client.get_table(
            DatabaseName=ctx.glue_catalog_database, Name=table_name
        )["Table"]
        part_sd = glue_table_meta["StorageDescriptor"].copy()
        part_sd["Location"] = f"s3://{ctx.curated_s3_bucket}/{s3_prefix}"
        try:
            glue_client.create_partition(
                DatabaseName=ctx.glue_catalog_database,
                TableName=table_name,
                PartitionInput={"Values": [partition_value], "StorageDescriptor": part_sd},
            )
        except glue_client.exceptions.AlreadyExistsException:
            glue_client.update_partition(
                DatabaseName=ctx.glue_catalog_database,
                TableName=table_name,
                PartitionValueList=[partition_value],
                PartitionInput={"Values": [partition_value], "StorageDescriptor": part_sd},
            )
        _logger.info(
            "curated_catalog_partition_registered",
            run_id=ctx.run_id,
            table_name=table_name,
            curated_date=partition_value,
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
            tenant_code=ctx.tenant_code,
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
