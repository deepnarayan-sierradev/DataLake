"""
AWS Lambda handler for the serving store load Step Functions task.

Reads analytics-layer Parquet written by the analytics publisher stage and
loads it into a tenant-scoped relational serving database for direct BI tool
access (Power BI, Tableau). Which tenant/entity pairs load, and into which
engine, is resolved from ServingStoreConfigRepositoryClient — an entity with
no config record is skipped, not failed, since most entities are not
onboarded to a serving store.

Step Functions input schema (Parameters block in LoadServingStore state):
  {
    "source_id":            str  — source_id from the triggering extraction run
    "entity_id":             str  — source-level entity_id from the triggering extraction run
                                    (logging/tracing context only — not used for the config lookup)
    "entity_type":           str  — analytics-layer entity type (e.g. "company"), the actual
                                    ServingStoreConfigRepositoryClient lookup key
    "environment":           str  — "dev" | "staging" | "prod"
    "run_id":                str  — run_id produced by the extraction stage
    "tenant_code":           str  — tenant identity for this run
    "analytics_s3_prefix":   str  — S3 prefix of analytics records (analytics publisher output)
  }

Step Functions output schema (stored at $.serving):
  {
    "skipped":         bool — true when no config exists or the entity is disabled
    "database_name":   str  — tenant-scoped physical database (present when not skipped)
    "table_name":       str  — physical table name (present when not skipped)
    "records_loaded":   int  — rows upserted this run (present when not skipped)
    "records_skipped":  int  — rows unchanged since last run (present when not skipped)
    "loaded_at":         str  — ISO-8601 UTC timestamp (present when not skipped)
  }

Required Lambda environment variables:
  AWS_REGION           — injected automatically by the Lambda runtime
  ANALYTICS_S3_BUCKET  — bucket where analytics Parquet is read from

Optional Lambda environment variables:
  GOVERNANCE_S3_BUCKET       — bucket for lineage records; lineage skipped if absent
  SERVING_STORE_CONFIG_TABLE — DynamoDB table override for config lookups

Security (OWASP A01, A03, A09):
  - tenant_code, source_id, entity_id, run_id validated against stable
    identifier regex before use; entity_type validated against ENTITY_TYPE_PATTERN.
  - S3 bucket name sourced exclusively from Lambda env vars — never event input.
  - Serving database credentials never appear in this handler; they are
    resolved from Secrets Manager inside ServingStoreLoader.
  - Lambda execution role is least-privilege serving_store_loader_runtime_role.
"""

from __future__ import annotations

import io
import os
import re
from collections.abc import Iterator
from typing import Any, Final

import boto3
import pyarrow.parquet as pq
import structlog

# Import every engine adapter module so its @serving_store_registry.register()
# decorator runs before resolve() is ever called — mirrors
# connector_runtime/extraction_pipeline_handler.py's adapter-import convention.
import serving_store.loaders.mysql_rds_loader
import serving_store.loaders.postgresql_loader
import serving_store.loaders.redshift_loader
import serving_store.loaders.sqlserver_loader  # noqa: F401
from contracts.identifier_policy import ENTITY_TYPE_PATTERN as _ENTITY_TYPE_PATTERN
from contracts.identifier_policy import STABLE_ID_PATTERN as _STABLE_ID_PATTERN
from contracts.identifier_policy import TENANT_CODE_PATTERN as _TENANT_CODE_PATTERN
from observability.lambda_utils import (
    check_lambda_timeout,
    check_lambda_timeout_periodic,
    configure_xray,
    require_env,
)
from observability.metrics_emitter import CloudWatchMetricsEmitter
from observability.structured_logger import get_platform_logger
from serving_store.interfaces.loader_interface import TransientServingError
from serving_store.registry import serving_store_registry
from serving_store.serving_store_config_repository import (
    ServingStoreConfigNotFoundError,
    ServingStoreConfigRepositoryClient,
)

_logger = get_platform_logger(__name__)

_REQUIRED_EVENT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "source_id",
        "entity_id",
        "entity_type",
        "environment",
        "run_id",
        "tenant_code",
        "analytics_s3_prefix",
    }
)
_KNOWN_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"dev", "staging", "prod"})
_SAFE_S3_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9\-_/=\.]{0,511}$"
)
_PARQUET_BATCH_SIZE: Final[int] = 2_000


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entry point for the serving store load Step Functions task."""
    _validate_event(event)
    check_lambda_timeout(context, min_remaining_ms=60_000)

    source_id: str = event["source_id"]
    entity_id: str = event["entity_id"]
    entity_type: str = event["entity_type"]
    environment: str = event["environment"]
    run_id: str = event["run_id"]
    tenant_code: str = str(event["tenant_code"])
    analytics_s3_prefix: str = event["analytics_s3_prefix"]

    configure_xray(tenant_code=tenant_code, source_id=source_id, entity_id=entity_id, run_id=run_id)
    structlog.contextvars.bind_contextvars(
        run_id=run_id,
        source_id=source_id,
        entity_id=entity_id,
        entity_type=entity_type,
        tenant_code=tenant_code,
    )

    try:
        return _run_serving_store_load(
            entity_id=entity_id,
            entity_type=entity_type,
            environment=environment,
            run_id=run_id,
            tenant_code=tenant_code,
            analytics_s3_prefix=analytics_s3_prefix,
            context=context,
        )
    except Exception as exc:
        _logger.error(
            "serving_store_load_stage_failed",
            entity_id=entity_id,
            entity_type=entity_type,
            run_id=run_id,
            environment=environment,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise
    finally:
        structlog.contextvars.clear_contextvars()


def _run_serving_store_load(
    entity_id: str,
    entity_type: str,
    environment: str,
    run_id: str,
    tenant_code: str,
    analytics_s3_prefix: str,
    context: Any,
) -> dict[str, Any]:
    """Business logic for the serving store load stage, isolated from handler plumbing."""
    region_name = require_env("AWS_REGION")
    analytics_s3_bucket = require_env("ANALYTICS_S3_BUCKET")
    governance_s3_bucket = os.environ.get("GOVERNANCE_S3_BUCKET") or None

    config_repo = ServingStoreConfigRepositoryClient(
        environment=environment, region_name=region_name
    )
    try:
        config = config_repo.load_config(tenant_code, entity_type)
    except ServingStoreConfigNotFoundError:
        _logger.info(
            "serving_store_load_skipped_no_config",
            entity_id=entity_id,
            entity_type=entity_type,
            tenant_code=tenant_code,
        )
        return {"skipped": True, "reason": "no_config"}

    if not config.enabled:
        _logger.info(
            "serving_store_load_skipped_disabled",
            entity_id=entity_id,
            entity_type=entity_type,
            tenant_code=tenant_code,
        )
        return {"skipped": True, "reason": "disabled"}

    s3 = boto3.client("s3", region_name=region_name)
    metrics_emitter = CloudWatchMetricsEmitter(region_name=region_name, tenant_code=tenant_code)
    loader = serving_store_registry.resolve(
        config.target_engine.value,
        secret_arn=config.secret_arn,
        region_name=config.region_name,
        metrics_emitter=metrics_emitter,
        environment=environment,
        governance_s3_bucket=governance_s3_bucket,
    )

    if loader.supports_s3_bulk_load:
        # Columnar/MPP engines (Redshift) load set-based via COPY straight from S3 —
        # row batches are never materialised in the Lambda.
        result = loader.load_from_s3(
            analytics_s3_bucket,
            analytics_s3_prefix,
            config.table_name,
            config.primary_keys,
            tenant_code,
            run_id=run_id,
            connection_database=config.connection_database,
        )
    else:

        def _batches() -> Iterator[list[dict[str, Any]]]:
            for batch in _iter_parquet_batches(s3, analytics_s3_bucket, analytics_s3_prefix):
                try:
                    check_lambda_timeout_periodic(
                        context, min_remaining_ms=30_000, operation_name="serving_store_load"
                    )
                except RuntimeError as exc:
                    raise TransientServingError(str(exc)) from exc
                yield batch

        result = loader.load_batches(
            _batches(),
            config.table_name,
            config.primary_keys,
            tenant_code,
            run_id=run_id,
            analytics_s3_bucket=analytics_s3_bucket,
            analytics_s3_prefix=analytics_s3_prefix,
            connection_database=config.connection_database,
        )
    metrics_emitter.flush()

    return {
        "skipped": False,
        "database_name": result.database_name,
        "table_name": result.table_name,
        "records_loaded": result.records_loaded,
        "records_skipped": result.records_skipped,
        "loaded_at": result.completed_at,
    }


def _iter_parquet_batches(
    s3: Any, bucket: str, prefix: str, batch_size: int = _PARQUET_BATCH_SIZE
) -> Iterator[list[dict[str, Any]]]:
    """Yield Parquet row-group batches from an S3 prefix, bounding peak memory."""
    clean = prefix.strip().rstrip("/") + "/"
    if ".." in clean or clean.startswith("/"):
        raise ValueError(f"Unsafe S3 prefix rejected: {clean!r}")

    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=clean):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".parquet"):
                continue
            raw = s3.get_object(Bucket=bucket, Key=obj["Key"])
            buf = io.BytesIO(raw["Body"].read())
            table = pq.read_table(buf)  # type: ignore[no-untyped-call]
            for record_batch in table.to_batches(max_chunksize=batch_size):
                batch_dict = record_batch.to_pydict()
                n = record_batch.num_rows
                cols = list(batch_dict.keys())
                yield [{col: batch_dict[col][i] for col in cols} for i in range(n)]
            del table


def _validate_event(event: dict[str, Any]) -> None:
    """Validate the Step Functions event payload (OWASP A03)."""
    missing = _REQUIRED_EVENT_FIELDS - set(event.keys())
    if missing:
        raise ValueError(f"Missing required event fields: {sorted(missing)}")

    for field in ("source_id", "entity_id", "run_id"):
        value = str(event[field])
        if not _STABLE_ID_PATTERN.match(value):
            raise ValueError(f"Event field {field}={value!r} contains disallowed characters.")

    entity_type = str(event["entity_type"])
    if not _ENTITY_TYPE_PATTERN.match(entity_type):
        raise ValueError(f"Event field entity_type={entity_type!r} contains disallowed characters.")

    environment = str(event["environment"])
    if environment not in _KNOWN_ENVIRONMENTS:
        raise ValueError(
            f"Unknown environment={environment!r}. Expected one of {sorted(_KNOWN_ENVIRONMENTS)}."
        )

    analytics_s3_prefix = str(event["analytics_s3_prefix"])
    if not _SAFE_S3_PREFIX_PATTERN.match(analytics_s3_prefix.rstrip("/")):
        raise ValueError(
            f"analytics_s3_prefix={analytics_s3_prefix!r} contains disallowed characters."
        )

    tenant_code = str(event["tenant_code"])
    if not _TENANT_CODE_PATTERN.match(tenant_code):
        raise ValueError(f"tenant_code={tenant_code!r} does not conform to the tenant code format.")
