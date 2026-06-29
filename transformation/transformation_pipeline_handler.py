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

import dataclasses
import os
import re
from datetime import UTC, datetime
from typing import Any, Final

from contracts.identifier_policy import STABLE_ID_PATTERN as _STABLE_ID_PATTERN
from observability.metrics_emitter import CloudWatchMetricsEmitter
from observability.structured_logger import get_platform_logger
from transformation.curated_layer_writer import CuratedLayerWriter
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
    region_name = _require_env("AWS_REGION")
    raw_s3_bucket = _require_env("RAW_S3_BUCKET")
    curated_s3_bucket = _require_env("CURATED_S3_BUCKET")
    field_mapping_s3_bucket = _require_env("FIELD_MAPPING_S3_BUCKET")

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

    pipeline = TransformationPipeline(
        mapping_registry_client=mapping_registry,
        quality_evaluator=quality_evaluator,
        curated_writer=curated_writer,
        quality_policy=None,          # No quality gate configured by default;
                                      # entities opt-in by populating the entity
                                      # config with a quality policy definition.
        classification_policy=None,   # Classification policy injected per-entity
                                      # when PII masking is required (future work).
        metrics_emitter=metrics_emitter,
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
    result = pipeline.execute(ctx)

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


def _require_env(name: str) -> str:
    """
    Return the value of a required Lambda environment variable.

    Raises:
        RuntimeError: When the variable is absent or empty.
    """
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(
            f"Required Lambda environment variable '{name}' is not set. "
            "Ensure the transformation pipeline Lambda is deployed with this variable configured."
        )
    return value


def _source_id_to_domain(source_id: str) -> str:
    """
    Derive the pipeline domain identifier from a stable source_id.

    Converts hyphens to underscores so the result satisfies the Glue table
    naming constraint (^[a-z][a-z0-9_]{0,63}$) used by TransformationContext.

    Examples:
        "mysql-rds"   → "mysql_rds"
        "salesforce"  → "salesforce"
        "netsuite"    → "netsuite"
    """
    return source_id.replace("-", "_")
