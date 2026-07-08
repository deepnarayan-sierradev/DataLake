"""
Golden record publisher.

Generates mastered entity records from matched curated records, applies
survivorship rules, and publishes the results to the analytics layer S3 path.

Published records include:
  golden_id               — deterministic ID stable across re-runs
  contributing_source_records — IDs of source records that formed this golden record
  survivorship_version    — policy version applied
  match_run_id            — run_id of the matching run
  <canonical_fields>      — merged field values from survivorship

Partition scheme:
  s3://analytics/{tenant_code}/canonical/{entity_type}/golden_date={YYYY-MM-DD}/run_id={run_id}/

Security (OWASP A09):
  - Match statistics emitted without PII field values.
  - Record payloads not included in log output.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import boto3

from contracts.identifier_policy import validate_tenant_code
from entity_resolution.matching_engine.match_rule_engine import (
    MatchRuleEngine,
    MatchRuleSet,
    stable_cluster_id,
)
from entity_resolution.publishing_shared import (
    emit_golden_record_lineage,
    flatten_list_fields,
    serialise_decisions,
)
from entity_resolution.survivorship_policy import (
    GoldenRecordSurvivorshipPolicy,
    SurvivorshipPolicy,
    SurvivorshipResult,
)
from observability.s3_writer import S3ParquetWriter
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)


@dataclass(frozen=True)
class GoldenRecordPublicationResult:
    """Summary of one golden record publication run."""

    match_run_id: str
    entity_type: str
    input_curated_record_count: int
    golden_record_count: int
    cluster_count: int
    analytics_s3_prefix: str
    decisions_s3_key: str
    golden_date: str  # YYYY-MM-DD
    published_at: str  # ISO-8601 UTC


class GoldenRecordPublisher:
    """
    End-to-end golden record generation pipeline.

    1. Load curated records from S3
    2. Cluster records via MatchRuleEngine
    3. Apply survivorship policy to each cluster
    4. Write golden records to analytics S3 layer
    5. Write match decision audit trail
    """

    def __init__(
        self,
        rule_set: MatchRuleSet,
        survivorship_policy: SurvivorshipPolicy,
        analytics_s3_bucket: str,
        region_name: str,
        governance_s3_bucket: str | None = None,
        curated_s3_bucket: str | None = None,
        curated_s3_prefixes: tuple[str, ...] | None = None,
    ) -> None:
        self._match_engine = MatchRuleEngine(rule_set)
        self._survivorship = GoldenRecordSurvivorshipPolicy(survivorship_policy)
        self._analytics_s3_bucket = analytics_s3_bucket
        self._region_name = region_name
        self._governance_s3_bucket = governance_s3_bucket
        self._curated_s3_bucket = curated_s3_bucket
        self._curated_s3_prefixes = curated_s3_prefixes
        self._s3: Any = boto3.client("s3", region_name=region_name)
        self._parquet_writer = S3ParquetWriter(self._s3)

    def publish(
        self,
        curated_records: list[dict[str, Any]],
        entity_type: str,
        match_run_id: str,
        id_field: str,
        source_field: str,
        tenant_code: str,
        golden_date: date | None = None,
    ) -> GoldenRecordPublicationResult:
        """
        Run the full golden record pipeline for the given curated records.

        Args:
            curated_records: Canonical post-transformation records.
            entity_type:     Entity type string (e.g., "customer").
            match_run_id:    Unique run ID for this matching run.
            id_field:        Field containing the source record identifier.
            source_field:    Field containing the source_id.
            tenant_code:     Tenant identity for this run — prefixes the analytics
                             S3 output path (ARCH-1) so two tenants publishing the
                             same entity_type never share a partition.
            golden_date:     Partition date; defaults to today UTC.

        Returns:
            GoldenRecordPublicationResult.
        """
        if not curated_records:
            raise GoldenRecordPublicationError("Cannot publish from zero curated records")

        tenant_code = validate_tenant_code(tenant_code)

        partition_date = golden_date or datetime.now(UTC).date()

        _logger.info(
            "golden_record_publish_started",
            match_run_id=match_run_id,
            entity_type=entity_type,
            input_record_count=len(curated_records),
        )

        # Step 1: Cluster
        clusters, all_decisions = self._match_engine.cluster(curated_records, id_field)

        # Step 2: Survivorship per cluster → golden records
        golden_records: list[dict[str, Any]] = []
        for cluster_ids in clusters:
            cluster_records = [
                r for r in curated_records if str(r.get(id_field, "")) in cluster_ids
            ]
            result: SurvivorshipResult = self._survivorship.resolve(
                cluster_records, id_field, source_field
            )
            golden = dict(result.canonical_record)
            # Build a stable cluster key from sorted contributing IDs
            cluster_key = "|".join(sorted(cluster_ids))
            golden["golden_id"] = stable_cluster_id(source_field, entity_type, cluster_key)
            golden["contributing_source_records"] = list(sorted(cluster_ids))
            golden["survivorship_version"] = self._survivorship._policy.policy_version
            golden["match_run_id"] = match_run_id
            # Serialise field_provenance as JSON so it lands in Parquet and is
            # directly queryable in Athena via json_extract_scalar(field_provenance, '$.full_name')
            golden["field_provenance"] = result.field_provenance
            golden_records.append(golden)

        # Step 3: Write golden records to analytics layer
        prefix = (
            f"{tenant_code}/canonical/{entity_type}"
            f"/golden_date={partition_date.isoformat()}"
            f"/run_id={match_run_id}/"
        )
        analytics_key = f"{prefix}golden.parquet"
        # Use the shared, multipart-capable S3ParquetWriter (PERF-4): streams
        # batches instead of building full Parquet bytes in memory, and lifts
        # the 5 GB single-PUT ceiling for large golden-record sets.
        self._parquet_writer.write(
            records_iter=flatten_list_fields(golden_records),
            bucket=self._analytics_s3_bucket,
            key=analytics_key,
            compression="snappy",
        )

        # Step 4: Write match decision audit trail (no PII values)
        decisions_key = (
            f"{tenant_code}/canonical/{entity_type}/match-decisions/{match_run_id}/decisions.json"
        )
        self._s3.put_object(
            Bucket=self._analytics_s3_bucket,
            Key=decisions_key,
            Body=serialise_decisions(all_decisions).encode("utf-8"),
            ContentType="application/json",
        )

        published_at = datetime.now(UTC).isoformat()

        _logger.info(
            "golden_record_publish_complete",
            match_run_id=match_run_id,
            entity_type=entity_type,
            golden_record_count=len(golden_records),
            cluster_count=len(clusters),
        )

        # Emit lineage record (spec §9.1 — lineage at publication boundaries)
        if self._governance_s3_bucket and self._curated_s3_bucket:
            emit_golden_record_lineage(
                s3_governance_bucket=self._governance_s3_bucket,
                curated_s3_bucket=self._curated_s3_bucket,
                curated_s3_prefixes=self._curated_s3_prefixes or (),
                analytics_s3_bucket=self._analytics_s3_bucket,
                analytics_s3_prefix=prefix,
                match_run_id=match_run_id,
                entity_type=entity_type,
                golden_record_count=len(golden_records),
                rule_set_version=self._match_engine._rule_set.rule_set_version,
                survivorship_version=self._survivorship._policy.policy_version,
                region_name=self._region_name,
            )

        return GoldenRecordPublicationResult(
            match_run_id=match_run_id,
            entity_type=entity_type,
            input_curated_record_count=len(curated_records),
            golden_record_count=len(golden_records),
            cluster_count=len(clusters),
            analytics_s3_prefix=prefix,
            decisions_s3_key=decisions_key,
            golden_date=partition_date.isoformat(),
            published_at=published_at,
        )


class GoldenRecordPublicationError(Exception):
    """Raised when golden record publication encounters an unrecoverable error."""
