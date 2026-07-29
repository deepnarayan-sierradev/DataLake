"""
AWS Lambda handler for the entity resolution pipeline Step Functions task.

Receives the curated S3 prefix from the transformation stage, loads curated
records from ALL configured sources for the entity type (not just the
triggering source), merges them into a single candidate pool, runs match
clustering and survivorship, and writes golden records to the analytics layer.

Multi-source design: when a second source (e.g. NetSuite) is added for an
entity type, it is listed in _ENTITY_TYPE_SOURCES.  The handler discovers and
loads the latest curated partition for each source automatically.  Sources with
no data yet are skipped gracefully — the pipeline continues with the sources
that do have curated records.

Step Functions input schema (Parameters block in RunEntityResolution state):
  {
    "source_id":         str  — source_id from the triggering extraction run
    "entity_id":         str  — entity_id from the triggering extraction run
    "environment":       str  — "dev" | "staging" | "prod"
    "run_id":            str  — run_id produced by the extraction stage
    "tenant_code":       str  — tenant identity for this run (ARCH-4: required, fails closed)
    "curated_s3_prefix": str  — S3 prefix where curated Parquet was written
  }

Step Functions output schema (stored at $.entity_resolution):
  {
    "canonical_prefix":          str  — S3 prefix of written golden records
    "entity_type":               str  — resolved entity type
    "input_curated_record_count": int
    "golden_record_count":        int
    "cluster_count":              int
    "golden_date":                str  — YYYY-MM-DD
    "published_at":               str  — ISO-8601 UTC
  }

Required Lambda environment variables:
  AWS_REGION              — injected automatically by the Lambda runtime
  CURATED_S3_BUCKET       — bucket holding curated Parquet files and
                            entity resolution configs (entity-resolution/ prefix)
  ANALYTICS_S3_BUCKET     — bucket where golden records are written

Optional Lambda environment variables:
  GOVERNANCE_S3_BUCKET    — bucket for lineage records; lineage skipped if absent

Security (OWASP A03, A07, A09):
  - All event fields validated against stable identifier regex before use.
  - S3 bucket names sourced exclusively from Lambda env vars — never from the
    event — to prevent path injection (OWASP A03 / CWE-22).
  - _record_id is constructed server-side from validated source_id + pk values.
  - Golden record log output contains counts only — no field values (OWASP A09).
  - Lambda execution role is least-privilege entity_resolution_runtime_role.
"""

from __future__ import annotations

import os
import time
from typing import Any, Final

import boto3

from config_propagation.capability import ConfigCapability
from config_propagation.pin_consumption import consume_pinned_config
from config_propagation.pinned_versions import PinnedConfigVersions
from contracts.identifier_policy import SAFE_S3_PREFIX_PATTERN as _SAFE_S3_PREFIX_PATTERN
from contracts.identifier_policy import STABLE_ID_PATTERN as _STABLE_ID_PATTERN
from contracts.identifier_policy import TENANT_CODE_PATTERN
from contracts.observability_contract import PipelineStage
from entity_resolution.canonical_record_publisher.canonical_record_publisher import (
    GoldenRecordPublicationError,
    GoldenRecordPublisher,
)
from entity_resolution.entity_type_registry import EntityTypeRegistryClient
from entity_resolution.resolution_config.resolution_config_registry import (
    ResolutionConfigNotFoundError,
    ResolutionConfigRegistry,
)
from observability.lambda_runtime import check_lambda_timeout, require_env
from observability.metrics_emitter import CloudWatchMetricsEmitter
from observability.stage_execution import (
    StageIdentity,
    derive_correlation_id,
    stage_execution,
)
from observability.structured_logger import get_platform_logger
from tenancy.scope_unit_repository import ScopeUnitRepository
from transformation.curated_layer_reader import (
    find_latest_curated_prefix,
    load_curated_records_duckdb,
)
from transformation.curated_layer_reader import (
    source_id_to_domain as _source_id_to_domain,
)

_logger = get_platform_logger(__name__)

# ---------------------------------------------------------------------------
# Validation constants (OWASP A03)
# ---------------------------------------------------------------------------

_REQUIRED_EVENT_FIELDS: Final[frozenset[str]] = frozenset(
    {"source_id", "entity_id", "environment", "run_id", "curated_s3_prefix", "tenant_code"}
)
_KNOWN_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"dev", "staging", "prod"})
# Matches curated S3 prefixes produced by the transformation stage.

# ---------------------------------------------------------------------------
# Module-level singleton (warm invocation cache)
# ---------------------------------------------------------------------------

_registry: ResolutionConfigRegistry | None = None
_entity_type_registry: EntityTypeRegistryClient | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    AWS Lambda entry point for the entity resolution pipeline Step Functions task.

    Args:
        event:   Step Functions Parameters block — see module docstring.
        context: Lambda runtime context (unused).

    Returns:
        Dict matching the Step Functions output schema (stored at $.entity_resolution).

    Raises:
        ValueError:    Input validation failure.
        RuntimeError:  Required environment variable absent at startup.
        Exception:     Any pipeline failure propagates to Step Functions for
                       retry / catch handling.
    """
    _validate_event(event)

    # Abort early if insufficient Lambda time remains.
    # Lambda timeout is 900 s; 60 s margin is sufficient for this stage.
    check_lambda_timeout(context, min_remaining_ms=60_000)

    source_id: str = event["source_id"]
    entity_id: str = event["entity_id"]
    environment: str = event["environment"]
    run_id: str = event["run_id"]
    curated_s3_prefix: str = event["curated_s3_prefix"]
    tenant_code: str = str(event["tenant_code"])

    _stage_start_ms = time.monotonic() * 1000

    # DL-OPS-05: one lifecycle, which also covers the hard-kill case no `finally` can.
    identity = StageIdentity(
        tenant_code=tenant_code,
        source_id=source_id,
        entity_id=entity_id,
        run_id=run_id,
        environment=environment,
        stage=PipelineStage.ENTITY_RESOLUTION.value,
        correlation_id=derive_correlation_id(run_id, event.get("replay_of_run_id")),
    )

    with stage_execution(identity, region_name=require_env("AWS_REGION"), lambda_context=context):
        return _run_entity_resolution(
            event=event,
            source_id=source_id,
            entity_id=entity_id,
            environment=environment,
            run_id=run_id,
            curated_s3_prefix=curated_s3_prefix,
            tenant_code=tenant_code,
            stage_start_ms=_stage_start_ms,
        )


def _run_entity_resolution(
    event: dict[str, Any],
    source_id: str,
    entity_id: str,
    environment: str,
    run_id: str,
    curated_s3_prefix: str,
    tenant_code: str,
    stage_start_ms: float,
) -> dict[str, Any]:
    """Business logic for the entity resolution stage, isolated from handler plumbing."""
    # ── Env vars ─────────────────────────────────────────────────────────────
    region_name = require_env("AWS_REGION")
    curated_s3_bucket = require_env("CURATED_S3_BUCKET")
    analytics_s3_bucket = require_env("ANALYTICS_S3_BUCKET")
    governance_s3_bucket: str | None = os.environ.get("GOVERNANCE_S3_BUCKET") or None

    # ── Resolve entity type (ARCH-2: tenant-scoped, DynamoDB-backed) ──────────
    global _entity_type_registry
    if _entity_type_registry is None:
        _entity_type_registry = EntityTypeRegistryClient(
            environment=environment, region_name=region_name
        )
    entity_type = _entity_type_registry.get_entity_type(entity_id, tenant_code=tenant_code)
    if entity_type is None:
        raise ValueError(
            f"No entity type mapping found for entity_id={entity_id!r} and "
            f"tenant_code={tenant_code!r}. Register it via "
            "EntityTypeRegistryClient.register_entity_type(), or add it to "
            "ENTITY_ID_TO_TYPE in entity_resolution/entity_type_registry.py "
            f"for the {tenant_code!r} tenant's default fallback."
        )
    pk_field = _entity_type_registry.get_pk_field(entity_type, tenant_code=tenant_code)
    if pk_field is None:
        raise ValueError(
            f"No primary-key field registered for entity_type={entity_type!r} and "
            f"tenant_code={tenant_code!r}."
        )

    _logger.info(
        "entity_resolution_handler_invoked",
        source_id=source_id,
        entity_id=entity_id,
        entity_type=entity_type,
        environment=environment,
        run_id=run_id,
        pk_field=pk_field,
    )

    s3 = boto3.client("s3", region_name=region_name)

    contributing_sources = _entity_type_registry.get_contributing_sources(
        entity_type, tenant_code=tenant_code
    )
    all_curated_records, loaded_prefixes = _load_all_contributing_records(
        s3=s3,
        curated_s3_bucket=curated_s3_bucket,
        source_id=source_id,
        entity_id=entity_id,
        curated_s3_prefix=curated_s3_prefix,
        pk_field=pk_field,
        contributing_sources=contributing_sources,
        tenant_code=tenant_code,
    )

    if not all_curated_records:
        raise GoldenRecordPublicationError(
            f"No curated records found for entity_type={entity_type!r}. "
            "Ensure the transformation stage completed successfully."
        )

    _warn_if_large_entity(all_curated_records)

    # ── Load resolution config + build publisher ──────────────────────────────
    global _registry
    if _registry is None:
        _registry = ResolutionConfigRegistry(
            s3_bucket=curated_s3_bucket,
            region_name=region_name,
        )

    try:
        publisher = GoldenRecordPublisher.from_registry(
            registry=_registry,
            entity_type=entity_type,
            tenant_code=tenant_code,
            analytics_s3_bucket=analytics_s3_bucket,
            region_name=region_name,
            governance_s3_bucket=governance_s3_bucket,
            curated_s3_bucket=curated_s3_bucket,
            curated_s3_prefixes=tuple(loaded_prefixes),
            # DL-SCOPE-08: the tenant's partition profile decides the resolution grain. A
            # partitioned tenant resolves per scope unit so two franchisees' identical customer
            # cannot become one golden record — no read-path filter can undo that merge.
            resolution_scope=ScopeUnitRepository(environment=environment, region_name=region_name)
            .get_partition_profile(tenant_code)
            .default_resolution_scope,
        )
    except ResolutionConfigNotFoundError as exc:
        raise ResolutionConfigNotFoundError(
            f"Resolution config not found for tenant_code={tenant_code!r} "
            f"entity_type={entity_type!r}. If this tenant was migrated from a "
            "pre-tenant-scoping deployment, re-run scripts/seed_entity_resolution_configs.py "
            f"--tenant-code {tenant_code} to publish its match-rules/survivorship config "
            "under the new tenant-prefixed S3 path, then retry."
        ) from exc

    # ── Reconcile the run's pinned config and record what became effective ────
    # Entity resolution is the capability where a mid-run definition change produces *wrong*
    # output rather than merely inconsistent provenance: two halves of one run would cluster
    # under different match rules. So this stage fails closed on a mismatch (DL-CFG-01).
    consume_pinned_config(
        pinned=PinnedConfigVersions.from_payload(event.get("pinned_config_versions")),
        capability=ConfigCapability.ENTITY_RESOLUTION,
        observed_version=publisher.match_rules_version,
        tenant_code=tenant_code,
        entity_key=entity_type,
        run_id=run_id,
        environment=environment,
        region_name=region_name,
        fail_on_mismatch=True,
    )

    # ── Run golden record publication ─────────────────────────────────────────
    result = publisher.publish(
        curated_records=all_curated_records,
        entity_type=entity_type,
        match_run_id=run_id,
        id_field="_record_id",  # unified cross-source identifier
        source_field="_source_id",
        tenant_code=tenant_code,
    )

    _logger.info(
        "entity_resolution_complete",
        entity_type=entity_type,
        run_id=run_id,
        input_record_count=result.input_curated_record_count,
        golden_record_count=result.golden_record_count,
        cluster_count=result.cluster_count,
        analytics_s3_prefix=result.analytics_s3_prefix,
    )

    # ── Emit CloudWatch metrics (§5.2) ────────────────────────────────────────
    try:
        _metrics_emitter = CloudWatchMetricsEmitter(region_name=region_name)
        _metrics_emitter.set_tenant_context(tenant_code)
        _stage_duration_ms = time.monotonic() * 1000 - stage_start_ms
        _metrics_emitter.emit_stage_duration(
            source_id=source_id,
            entity_id=entity_id,
            environment=environment,
            stage="entity_resolution",
            duration_ms=_stage_duration_ms,
        )
        _metrics_emitter.emit_golden_record_count(
            source_id=source_id,
            entity_id=entity_id,
            environment=environment,
            count=result.golden_record_count,
        )
        _metrics_emitter.emit_cluster_count(
            source_id=source_id,
            entity_id=entity_id,
            environment=environment,
            count=result.cluster_count,
        )
        _metrics_emitter.flush()
    except Exception as _exc:
        # Metric emission must never fail a pipeline run (OWASP A09 — graceful degradation).
        _logger.warning("entity_resolution_metrics_emission_failed", error=str(_exc))

    return {
        "canonical_prefix": result.analytics_s3_prefix,
        "entity_type": result.entity_type,
        "input_curated_record_count": result.input_curated_record_count,
        "golden_record_count": result.golden_record_count,
        "cluster_count": result.cluster_count,
        "golden_date": result.golden_date,
        "published_at": result.published_at,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LARGE_ENTITY_THRESHOLD: Final[int] = 500_000


def _load_all_contributing_records(
    s3: Any,
    curated_s3_bucket: str,
    source_id: str,
    entity_id: str,
    curated_s3_prefix: str,
    pk_field: str,
    contributing_sources: list[tuple[str, str]],
    tenant_code: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Load and tag curated records from every source contributing to an entity type.

    The triggering source's records come from the exact prefix Step Functions
    passed in. All other configured sources are located by scanning the
    curated bucket for their latest partition — skipped gracefully if absent.

    Returns (all_curated_records, loaded_prefixes).

    Performance (PERF-3): each source's curated Parquet is read via DuckDB's
    read_parquet() (load_curated_records_duckdb) rather than the hand-rolled
    Python S3 list + download + pq.read_table() loop (load_curated_records).
    DuckDB reads the Parquet objects directly from S3 — the full file no
    longer crosses into Python memory before a columnar engine touches it.
    AWS_REGION is read here (not threaded through the signature) so this
    function's public shape is unchanged for _run_entity_resolution.
    """
    region_name = require_env("AWS_REGION")
    all_curated_records: list[dict[str, Any]] = []
    loaded_prefixes: list[str] = []

    for contrib_source_id, contrib_entity_id in contributing_sources:
        contrib_domain = _source_id_to_domain(contrib_source_id)

        prefix: str | None
        if contrib_source_id == source_id and contrib_entity_id == entity_id:
            # Current run — load from the exact prefix passed in by Step Functions.
            prefix = curated_s3_prefix
            if prefix is None:
                # Transformation wrote no records (empty extract) — nothing to load.
                _logger.info(
                    "entity_resolution_source_skipped_no_records",
                    contrib_source_id=contrib_source_id,
                    contrib_entity_id=contrib_entity_id,
                )
                continue
        else:
            # Other source — find the latest curated partition in the bucket.
            prefix = find_latest_curated_prefix(
                s3, curated_s3_bucket, contrib_domain, contrib_entity_id, tenant_code
            )
            if prefix is None:
                _logger.info(
                    "entity_resolution_source_skipped_no_data",
                    contrib_source_id=contrib_source_id,
                    contrib_entity_id=contrib_entity_id,
                )
                continue

        # DuckDB reads this source's curated Parquet directly from S3
        # (read_parquet) rather than a Python-side list+download loop
        # (PERF-3). The result is still materialised into a tagged dict list
        # here because the match engine's public contract requires
        # list[dict[str, Any]] (record_blocker.py / match_rule_engine.py) —
        # but the S3 file read itself is no longer duplicated Python-side
        # work. For each source we tag then immediately extend the combined
        # pool — the per-source list is released after extend() so only one
        # source's untagged data is in memory at a time during loading.
        source_records = load_curated_records_duckdb(s3, curated_s3_bucket, prefix, region_name)
        _logger.info(
            "entity_resolution_source_loaded",
            contrib_source_id=contrib_source_id,
            contrib_entity_id=contrib_entity_id,
            record_count=len(source_records),
        )

        # Tag each record with a unified cross-source identifier and source label.
        # _record_id is constructed server-side; never derived from event input.
        for rec in source_records:
            pk_value = str(rec.get(pk_field, ""))
            rec["_record_id"] = f"{contrib_source_id}:{pk_value}"
            rec["_source_id"] = contrib_source_id

        all_curated_records.extend(source_records)
        loaded_prefixes.append(prefix)
        del source_records  # release per-source list after merging into combined pool

    return all_curated_records, loaded_prefixes


def _warn_if_large_entity(all_curated_records: list[dict[str, Any]]) -> None:
    """Warn for large datasets where in-memory matching may exhaust Lambda memory."""
    if len(all_curated_records) > _LARGE_ENTITY_THRESHOLD:
        _logger.warning(
            "entity_resolution_large_record_count",
            total_records=len(all_curated_records),
            threshold=_LARGE_ENTITY_THRESHOLD,
            message="Consider merge_strategy=glue_merge for entities exceeding 500K records.",
        )


def _validate_event(event: dict[str, Any]) -> None:
    """
    Validate the Step Functions event payload.

    Raises ValueError for missing fields, unknown environments, or field values
    that fail the stable-identifier pattern (OWASP A03).
    """
    missing = _REQUIRED_EVENT_FIELDS - set(event.keys())
    if missing:
        raise ValueError(f"Missing required event fields: {sorted(missing)}")

    for field in ("source_id", "entity_id", "run_id"):
        value = str(event[field])
        if not _STABLE_ID_PATTERN.match(value):
            raise ValueError(
                f"Event field {field}={value!r} contains disallowed characters. "
                "Expected lowercase alphanumeric with hyphens (max 64 chars)."
            )

    environment = str(event["environment"])
    if environment not in _KNOWN_ENVIRONMENTS:
        raise ValueError(
            f"Unknown environment={environment!r}. Expected one of {sorted(_KNOWN_ENVIRONMENTS)}."
        )

    curated_prefix = str(event["curated_s3_prefix"])
    if not _SAFE_S3_PREFIX_PATTERN.match(curated_prefix.rstrip("/")):
        raise ValueError(f"curated_s3_prefix={curated_prefix!r} contains disallowed characters.")

    # tenant_code is required (ARCH-4) and must always be well-formed
    # (OWASP A03 / SEC-5) — a missing or malformed tenant_code must fail
    # closed rather than silently default to another tenant's identity.
    tenant_code = str(event["tenant_code"])
    if not TENANT_CODE_PATTERN.match(tenant_code):
        raise ValueError(f"tenant_code={tenant_code!r} does not conform to the tenant code format.")
