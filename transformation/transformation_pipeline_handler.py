"""
AWS Lambda handler for the transformation pipeline Step Functions task.

This is the entry point that Step Functions invokes after a successful raw
extraction run.  It receives the extraction result forwarded by the state
machine, wires all platform dependencies, and delegates to
TransformationPipeline for the full field-mapping → quality-evaluation →
curated-layer-write pipeline.

Step Functions input schema (Parameters block in RunTransformation state):
  {
    "source_id":       str   — stable source identifier (e.g. "mysql-rds")
    "entity_id":       str   — stable entity identifier (e.g. "mysql-rds-contracts")
    "environment":     str   — "dev" | "staging" | "prod"
    "run_id":          str   — run_id produced by the extraction stage
    "raw_s3_prefix":   str   — S3 prefix where raw Parquet files were written
    "mapping_version": str   — "latest" or explicit version tag (e.g. "v1")
  }

Required Lambda environment variables:
  AWS_REGION                — injected automatically by the Lambda runtime
  PLATFORM_ENVIRONMENT      — deployment environment (dev / staging / prod)
  RAW_S3_BUCKET             — name of the raw layer S3 bucket
  CURATED_S3_BUCKET         — name of the curated layer S3 bucket
  FIELD_MAPPING_S3_BUCKET   — bucket that holds field mapping JSON files
                              (typically the same as CURATED_S3_BUCKET)

Optional Lambda environment variables:
  GOVERNANCE_S3_BUCKET      — bucket for lineage records; lineage disabled if absent
  GLUE_CATALOG_DATABASE     — Glue database for catalog registration; skipped if absent

Security (OWASP A03, A07, A09):
  - All event fields validated against stable identifier regex before use.
  - S3 bucket names sourced exclusively from Lambda env vars — never from event
    input — to prevent path injection (OWASP A03 / CWE-22).
  - Result returned to Step Functions contains only metadata; no record payloads
    are ever included (PII protection — OWASP A09).
  - Lambda execution role is least-privilege transformation_runtime_role.
  - domain is derived server-side from source_id; never accepted from the event.
"""

from __future__ import annotations

import boto3
import dataclasses
import os
import re
from datetime import UTC, datetime
from typing import Any, Final

from contracts.identifier_policy import STABLE_ID_PATTERN as _STABLE_ID_PATTERN
from connector_runtime.configuration_repository.configuration_repository import (
    ConfigurationNotFoundError,
    ConfigurationRepositoryClient,
)
from observability.metrics_emitter import CloudWatchMetricsEmitter
from observability.structured_logger import get_platform_logger
from observability.lambda_utils import require_env, check_lambda_timeout
from transformation.curated_accumulator import CuratedAccumulator
from transformation.curated_layer_writer import CuratedLayerWriter
from transformation.curated_utils import source_id_to_domain as _source_id_to_domain
from transformation.field_mapping.field_mapping_registry import FieldMappingRegistryClient
from transformation.quality_evaluation.quality_policy_evaluator import QualityPolicyEvaluator
from transformation.transformation_pipeline import TransformationContext, TransformationPipeline

_logger = get_platform_logger(__name__)

_REQUIRED_EVENT_FIELDS: Final[frozenset[str]] = frozenset(
    {"source_id", "entity_id", "environment", "run_id", "raw_s3_prefix"}
)
_KNOWN_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"dev", "staging", "prod"})

# mapping_version must be "latest" or a safe version tag like "v1", "v2-beta"
# Rejects path traversal characters and excessively long strings (OWASP A03).
_MAPPING_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-z][a-z0-9\-_\.]{0,31}$"
)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    AWS Lambda entry point for the transformation pipeline Step Functions task.

    Args:
        event:   Step Functions Parameters block output — see module docstring.
        context: Lambda runtime context (unused; typed Any to avoid aws_lambda
                 dependency in pyproject.toml).

    Returns:
        A dict representation of TransformationResult, serialised for
        Step Functions task output (stored at $.transformation in execution state).

    Raises:
        ValueError:  Input validation failure (missing/invalid fields or env vars).
        RuntimeError: Required environment variable absent at Lambda startup.
        Exception:   Any pipeline stage failure propagates to Step Functions,
                     which records the execution as FAILED and applies the
                     configured retry / catch policy.
    """
    _validate_event(event)

    # Abort early if insufficient Lambda time remains to run the full pipeline.
    # Lambda timeout is 900 s; 60 s margin prevents a wasted invocation.
    check_lambda_timeout(context, min_remaining_ms=60_000)

    source_id: str = event["source_id"]
    entity_id: str = event["entity_id"]
    environment: str = event["environment"]
    run_id: str = event["run_id"]
    raw_s3_prefix: str = event["raw_s3_prefix"]
    mapping_version: str = str(event.get("mapping_version") or "latest")

    if not _MAPPING_VERSION_PATTERN.match(mapping_version):
        raise ValueError(
            f"mapping_version={mapping_version!r} contains disallowed characters. "
            "Expected 'latest' or a version tag like 'v1'."
        )

    # ── Env vars ─────────────────────────────────────────────────────────────
    region_name = require_env("AWS_REGION")
    raw_s3_bucket = require_env("RAW_S3_BUCKET")
    curated_s3_bucket = require_env("CURATED_S3_BUCKET")
    field_mapping_s3_bucket = require_env("FIELD_MAPPING_S3_BUCKET")

    # Optional governance / catalog wiring — disabled when not configured.
    governance_s3_bucket: str | None = os.environ.get("GOVERNANCE_S3_BUCKET") or None
    glue_catalog_database: str | None = os.environ.get("GLUE_CATALOG_DATABASE") or None

    _logger.info(
        "transformation_pipeline_handler_invoked",
        source_id=source_id,
        entity_id=entity_id,
        environment=environment,
        run_id=run_id,
        mapping_version=mapping_version,
        region_name=region_name,
        glue_catalog_enabled=glue_catalog_database is not None,
        lineage_enabled=governance_s3_bucket is not None,
    )

    # ── Derive domain ─────────────────────────────────────────────────────────
    # domain is used for Glue table name construction and curated S3 path
    # partitioning. Derived server-side to prevent injection (OWASP A03).
    # "mysql-rds" → "mysql_rds", "salesforce" → "salesforce", etc.
    domain = _source_id_to_domain(source_id)

    # ── Wire dependencies ─────────────────────────────────────────────────────
    mapping_registry = FieldMappingRegistryClient(
        s3_bucket=field_mapping_s3_bucket,
        region_name=region_name,
    )

    quality_evaluator = QualityPolicyEvaluator()

    curated_writer = CuratedLayerWriter(
        s3_bucket=curated_s3_bucket,
        region_name=region_name,
    )

    metrics_emitter = CloudWatchMetricsEmitter(region_name=region_name)

    # ── Load entity config (for incremental merge settings) ───────────────────
    # Reads primary_key_field and soft_delete_field from the entity config record
    # stored in DynamoDB.  These fields drive SCD Type 1 merge behaviour.
    # For entities without primary_key_field set (all full-load entities and
    # incremental entities not yet migrated), config loading succeeds but returns
    # None for both fields — accumulator is not created and pipeline is unchanged.
    #
    # Security: table name is constructed server-side from the validated
    # environment string; never interpolated from user event input (OWASP A03).
    curated_accumulator: CuratedAccumulator | None = None
    try:
        config_table = f"{environment}-entity-extraction-config"
        config_repo = ConfigurationRepositoryClient(
            table_name=config_table,
            region_name=region_name,
        )
        entity_config = config_repo.load_config(
            source_id=source_id,
            entity_id=entity_id,
        )
        if entity_config.primary_key_field is not None:
            curated_accumulator = CuratedAccumulator(
                s3=boto3.client("s3", region_name=region_name),
                curated_s3_bucket=curated_s3_bucket,
                primary_key_field=entity_config.primary_key_field,
                soft_delete_field=entity_config.soft_delete_field,
            )
            _logger.info(
                "curated_accumulator_wired",
                source_id=source_id,
                entity_id=entity_id,
                primary_key_field=entity_config.primary_key_field,
                soft_delete_field=entity_config.soft_delete_field,
            )
    except ConfigurationNotFoundError:
        # Entity config not found — not a blocker for transformation.
        # Pipeline runs in append-only mode (accumulator remains None).
        _logger.warning(
            "entity_config_not_found_accumulator_disabled",
            source_id=source_id,
            entity_id=entity_id,
        )
    except Exception as exc:  # noqa: BLE001
        # Config load failure must never block transformation — append-only
        # fallback is safe (data is written, merge is simply skipped).
        _logger.warning(
            "entity_config_load_failed_accumulator_disabled",
            source_id=source_id,
            entity_id=entity_id,
            error=str(exc),
        )

    pipeline = TransformationPipeline(
        mapping_registry_client=mapping_registry,
        quality_evaluator=quality_evaluator,
        curated_writer=curated_writer,
        quality_policy=None,
        classification_policy=None,
        metrics_emitter=metrics_emitter,
        curated_accumulator=curated_accumulator,
    )

    # ── Build context ─────────────────────────────────────────────────────────
    ctx = TransformationContext(
        run_id=run_id,
        source_id=source_id,
        entity_id=entity_id,
        domain=domain,
        raw_s3_bucket=raw_s3_bucket,
        raw_s3_prefix=raw_s3_prefix,
        mapping_bucket=field_mapping_s3_bucket,
        curated_s3_bucket=curated_s3_bucket,
        region_name=region_name,
        mapping_version=mapping_version,
        curated_date=datetime.now(UTC).date(),
        governance_s3_bucket=governance_s3_bucket,
        glue_catalog_database=glue_catalog_database,
        environment=environment,
    )

    # ── Execute pipeline ──────────────────────────────────────────────────────
    try:
        result = pipeline.execute(ctx)
    finally:
        # Flush buffered CloudWatch metrics regardless of success or failure.
        # flush() is designed to never raise — it swallows ClientError internally.
        metrics_emitter.flush()

    _logger.info(
        "transformation_pipeline_handler_completed",
        run_id=result.run_id,
        source_id=result.source_id,
        entity_id=result.entity_id,
        raw_record_count=result.raw_record_count,
        canonical_record_count=result.canonical_record_count,
        mapping_failures=result.mapping_failures,
        is_publication_blocked=result.is_publication_blocked,
        mapping_version=result.mapping_version,
    )

    return dataclasses.asdict(result)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _validate_event(event: dict[str, Any]) -> None:
    """
    Validate the Step Functions input before any processing.

    Raises:
        ValueError: Missing required fields, invalid stable IDs, or unknown environment.
    """
    missing = _REQUIRED_EVENT_FIELDS - event.keys()
    if missing:
        raise ValueError(
            f"Step Functions transformation input is missing required fields: {sorted(missing)}"
        )

    source_id = str(event["source_id"])
    entity_id = str(event["entity_id"])
    environment = str(event["environment"])
    run_id = str(event["run_id"])

    if not _STABLE_ID_PATTERN.match(source_id):
        raise ValueError(
            f"source_id={source_id!r} does not conform to the stable identifier format."
        )
    if not _STABLE_ID_PATTERN.match(entity_id):
        raise ValueError(
            f"entity_id={entity_id!r} does not conform to the stable identifier format."
        )
    if not _STABLE_ID_PATTERN.match(run_id):
        raise ValueError(
            f"run_id={run_id!r} does not conform to the stable identifier format."
        )
    if environment not in _KNOWN_ENVIRONMENTS:
        raise ValueError(
            f"environment={environment!r} is not a known deployment environment. "
            f"Expected one of {sorted(_KNOWN_ENVIRONMENTS)}."
        )
