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
    "environment":     str   — "dev" | "uat" | "prod"
    "run_id":          str   — run_id produced by the extraction stage
    "tenant_code":     str   — tenant identity for this run (ARCH-4: required, fails closed)
    "raw_s3_prefix":   str   — S3 prefix where raw Parquet files were written
    "mapping_version": str   — "latest" or explicit version tag (e.g. "v1")
  }

Required Lambda environment variables:
  AWS_REGION                — injected automatically by the Lambda runtime
  PLATFORM_ENVIRONMENT      — deployment environment (dev / uat / prod)
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

import boto3

from connector_runtime.configuration_repository.configuration_repository import (
    ConfigurationNotFoundError,
    ConfigurationRepositoryClient,
    ConfigurationValidationError,
)
from contracts.dlq_routing import DlqStage
from contracts.identifier_policy import STABLE_ID_PATTERN as _STABLE_ID_PATTERN
from contracts.identifier_policy import TENANT_CODE_PATTERN as _TENANT_CODE_PATTERN
from contracts.observability_contract import PipelineStage
from observability.lambda_runtime import (
    check_lambda_timeout,
    require_env,
)
from observability.metrics_emitter import CloudWatchMetricsEmitter
from observability.stage_execution import (
    StageIdentity,
    derive_correlation_id,
    stage_execution,
)
from observability.structured_logger import get_platform_logger
from tenancy.connection_keys import resolve_connection_id
from tenancy.scope_unit_repository import ScopeUnitRepository
from tenancy.source_connection import SourceConnection
from tenancy.source_connection_repository import SourceConnectionRepository
from transformation.curated_accumulator import CuratedAccumulator
from transformation.curated_layer_reader import source_id_to_domain as _source_id_to_domain
from transformation.curated_layer_writer import CuratedLayerWriter
from transformation.field_mapping.field_mapping_registry import FieldMappingRegistryClient
from transformation.quality_evaluation.quality_policy_evaluator import QualityPolicyEvaluator
from transformation.transformation_pipeline import TransformationContext, TransformationPipeline

_logger = get_platform_logger(__name__)

_REQUIRED_EVENT_FIELDS: Final[frozenset[str]] = frozenset(
    {"source_id", "entity_id", "environment", "run_id", "raw_s3_prefix", "tenant_code"}
)
_KNOWN_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"dev", "uat", "prod"})

_MAPPING_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9\-_\.]{0,31}$")


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

    check_lambda_timeout(context, min_remaining_ms=60_000)

    source_id: str = event["source_id"]
    entity_id: str = event["entity_id"]
    environment: str = event["environment"]
    run_id: str = event["run_id"]
    raw_s3_prefix: str = event["raw_s3_prefix"]
    mapping_version: str = str(event.get("mapping_version") or "latest")
    connection_id: str | None = str(event["connection_id"]) if event.get("connection_id") else None
    tenant_code: str = str(event["tenant_code"])

    stage_identity = StageIdentity(
        tenant_code=tenant_code,
        source_id=source_id,
        entity_id=entity_id,
        run_id=run_id,
        environment=environment,
        stage=PipelineStage.TRANSFORMATION.value,
        dlq_stage=DlqStage.TRANSFORMATION,
        correlation_id=derive_correlation_id(run_id, event.get("replay_of_run_id")),
        connection_id=connection_id,
    )

    if not _MAPPING_VERSION_PATTERN.match(mapping_version):
        raise ValueError(
            f"mapping_version={mapping_version!r} contains disallowed characters. "
            "Expected 'latest' or a version tag like 'v1'."
        )

    region_name = require_env("AWS_REGION")
    raw_s3_bucket = require_env("RAW_S3_BUCKET")
    curated_s3_bucket = require_env("CURATED_S3_BUCKET")
    field_mapping_s3_bucket = require_env("FIELD_MAPPING_S3_BUCKET")

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

    domain = _source_id_to_domain(source_id)

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
    metrics_emitter.set_tenant_context(tenant_code)

    curated_accumulator: CuratedAccumulator | None = None
    try:
        config_repo = ConfigurationRepositoryClient(
            environment=environment,
            region_name=region_name,
        )
        entity_config = config_repo.load_config(
            source_id=source_id,
            entity_id=entity_id,
            tenant_code=tenant_code,
        )
        if entity_config.primary_key_field is not None:
            curated_accumulator = CuratedAccumulator(
                s3=boto3.client("s3", region_name=region_name),
                curated_s3_bucket=curated_s3_bucket,
                primary_key_field=entity_config.primary_key_field,
                tenant_code=tenant_code,
                soft_delete_field=entity_config.soft_delete_field,
                region_name=region_name,
            )
            _logger.info(
                "curated_accumulator_wired",
                source_id=source_id,
                entity_id=entity_id,
                primary_key_field=entity_config.primary_key_field,
                soft_delete_field=entity_config.soft_delete_field,
            )
    except ConfigurationNotFoundError:
        _logger.warning(
            "entity_config_not_found_accumulator_disabled",
            source_id=source_id,
            entity_id=entity_id,
        )
    except ConfigurationValidationError as exc:
        _logger.warning(
            "entity_config_invalid_accumulator_disabled",
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

    scope_units = ScopeUnitRepository(environment=environment, region_name=region_name)

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
        tenant_code=tenant_code,
        lambda_context=context,  # for mid-execution timeout checks (§3.5)
        partition_profile=scope_units.get_partition_profile(tenant_code),
        source_connection=_scope_connection(
            environment=environment,
            region_name=region_name,
            tenant_code=tenant_code,
            source_id=source_id,
            connection_id=connection_id,
        ),
        known_scope_unit_ids=scope_units.known_unit_ids(tenant_code),
    )

    with stage_execution(
        stage_identity,
        region_name=region_name,
        lambda_context=context,
        metrics=metrics_emitter,
    ):
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
        raise ValueError(f"run_id={run_id!r} does not conform to the stable identifier format.")
    if environment not in _KNOWN_ENVIRONMENTS:
        raise ValueError(
            f"environment={environment!r} is not a known deployment environment. "
            f"Expected one of {sorted(_KNOWN_ENVIRONMENTS)}."
        )

    tenant_code = str(event["tenant_code"])
    if not _TENANT_CODE_PATTERN.match(tenant_code):
        raise ValueError(f"tenant_code={tenant_code!r} does not conform to the tenant code format.")


def _scope_connection(
    *,
    environment: str,
    region_name: str,
    tenant_code: str,
    source_id: str,
    connection_id: str | None,
) -> SourceConnection | None:
    """
    The connection that owns these rows, or None when the environment predates the connection
    model.

    None is safe rather than convenient: `ScopeAttributor` treats it as unattributable, which a
    `single`-partition tenant absorbs structurally and a partitioned tenant fails on — the
    correct outcome, because rows nobody can attribute are invisible to every unit-scoped caller.
    """
    resolved = resolve_connection_id(source_id, connection_id)
    try:
        return SourceConnectionRepository(
            environment=environment, region_name=region_name
        ).resolve_connection(tenant_code, resolved)
    except Exception as exc:
        _logger.warning(
            "scope_connection_unavailable",
            tenant_code=tenant_code,
            connection_id=resolved,
            error=str(exc),
        )
        return None
