"""
AWS Lambda handler for the analytics layer publisher Step Functions task.

Reads golden records written by the entity resolution stage, strips internal
system fields, writes BI-ready Parquet to the analytics S3 layer, and
registers (or updates) the Glue Data Catalog table so Athena and other
consumers can query it immediately.

Step Functions input schema (Parameters block in PublishAnalytics state):
  {
    "source_id":         str  — source_id from the triggering extraction run
    "entity_id":         str  — entity_id from the triggering extraction run
    "environment":       str  — "dev" | "staging" | "prod"
    "run_id":            str  — run_id produced by the extraction stage
    "tenant_code":       str  — tenant identity for this run (ARCH-4: required, fails closed)
    "canonical_prefix":  str  — S3 prefix of golden records (entity_resolution output)
    "curated_s3_prefix": str  — S3 prefix of curated records (transformation output)
  }

Step Functions output schema (stored at $.analytics):
  {
    "analytics_s3_prefix":   str  — S3 prefix where analytics Parquet was written
    "entity_type":           str  — resolved entity type
    "record_count":          int  — number of analytics records written
    "glue_table":            str  — "{database}.{table}" registered in Glue catalog
    "analytics_date":        str  — YYYY-MM-DD partition date
    "published_at":          str  — ISO-8601 UTC timestamp
  }

Required Lambda environment variables:
  AWS_REGION               — injected automatically by the Lambda runtime
  ANALYTICS_S3_BUCKET      — bucket where analytics Parquet is read and written
  GLUE_CATALOG_DATABASE    — Glue database name for analytics layer tables

Optional Lambda environment variables:
  GOVERNANCE_S3_BUCKET     — bucket for lineage records; lineage skipped if absent

Security (OWASP A03, A07, A09):
  - All event fields validated against stable identifier regex before use.
  - S3 bucket names and Glue database name sourced exclusively from Lambda
    env vars — never from event input (prevents path/name injection, OWASP A03).
  - Analytics output contains no raw record values in log output (OWASP A09).
  - Lambda execution role is least-privilege analytics_publisher_runtime_role.
"""

from __future__ import annotations

import io
import time
from datetime import UTC, datetime
from typing import Any, Final

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

from contracts.dlq_routing import DlqStage
from contracts.identifier_policy import SAFE_S3_PREFIX_PATTERN as _SAFE_S3_PREFIX_PATTERN
from contracts.identifier_policy import STABLE_ID_PATTERN as _STABLE_ID_PATTERN
from contracts.identifier_policy import TENANT_CODE_PATTERN
from contracts.observability_contract import PipelineStage
from entity_resolution.entity_type_registry import EntityTypeRegistryClient
from governance.data_catalog_registration import (
    CatalogDatasetSpec,
    DataCatalogRegistrationClient,
    DataLayer,
)
from observability.lambda_runtime import check_lambda_timeout, require_env
from observability.metrics_emitter import CloudWatchMetricsEmitter
from observability.s3_writer import S3ParquetWriter
from observability.stage_execution import (
    StageIdentity,
    derive_correlation_id,
    stage_execution,
)
from observability.structured_logger import get_platform_logger
from observability.usage_metering import (
    TenantUsageRepository,
    aggregate_usage,
    current_period,
    read_audit_records_for_period,
)

_logger = get_platform_logger(__name__)

# ARCH-2: module-level warm-invocation cache, mirroring the pattern already
# used for ResolutionConfigRegistry in entity_resolution_pipeline_handler.py.
_entity_type_registry: EntityTypeRegistryClient | None = None

# ---------------------------------------------------------------------------
# Fields removed from golden records before writing the BI analytics layer.
# These are internal entity resolution system fields that are useful for
# debugging/auditing but create noise in BI tools and Athena queries.
# golden_id is KEPT — it is the stable key for joins across entity types.
# ---------------------------------------------------------------------------

_INTERNAL_FIELDS_TO_DROP: Final[frozenset[str]] = frozenset(
    {
        "_record_id",  # cross-source surrogate key — internal to ER pipeline
        "_source_id",  # source tag injected by ER handler — internal
        "contributing_source_records",  # list of source record IDs — ER audit detail
        "survivorship_version",  # policy version applied — ER audit detail
        "match_run_id",  # ER run identifier — duplicated in partition path
        "field_provenance",  # JSON string — per-field winner metadata
    }
)

# ---------------------------------------------------------------------------
# Validation constants (OWASP A03)
# ---------------------------------------------------------------------------

_REQUIRED_EVENT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "source_id",
        "entity_id",
        "environment",
        "run_id",
        "canonical_prefix",
        "curated_s3_prefix",
        "tenant_code",
    }
)
_KNOWN_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"dev", "staging", "prod"})

# PyArrow type → Glue/Athena column type string
_ARROW_TO_GLUE_TYPE: Final[dict[str, str]] = {
    "int8": "tinyint",
    "int16": "smallint",
    "int32": "int",
    "int64": "bigint",
    "uint8": "tinyint",
    "uint16": "smallint",
    "uint32": "int",
    "uint64": "bigint",
    "float": "float",
    "double": "double",
    "decimal128": "double",
    "bool": "boolean",
    "date32": "date",
    "date64": "date",
    "timestamp[s]": "timestamp",
    "timestamp[ms]": "timestamp",
    "timestamp[us]": "timestamp",
    "timestamp[ns]": "timestamp",
    "string": "string",
    "large_string": "string",
    "utf8": "string",
    "large_utf8": "string",
}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    AWS Lambda entry point for the analytics publisher Step Functions task.

    Args:
        event:   Step Functions Parameters block — see module docstring.
        context: Lambda runtime context (unused).

    Returns:
        Dict matching the Step Functions output schema (stored at $.analytics).
    """
    _validate_event(event)

    # Abort early if insufficient Lambda time remains.
    # Lambda timeout is 300 s; 60 s margin prevents a wasted invocation.
    check_lambda_timeout(context, min_remaining_ms=60_000)

    source_id: str = event["source_id"]
    entity_id: str = event["entity_id"]
    environment: str = event["environment"]
    run_id: str = event["run_id"]
    canonical_prefix: str = event["canonical_prefix"]
    tenant_code: str = str(event["tenant_code"])
    # Optional — set by the extraction stage and threaded through Step
    # Functions Parameters at each stage boundary (§5.7 / OBS-4). Absent on
    # manually-triggered or older-format executions; e2e metric is skipped then.
    run_started_at: str | None = event.get("run_started_at")

    _stage_start_ms = time.monotonic() * 1000

    # DL-OPS-05: the shared lifecycle replaces the hand-rolled bind/try/finally. It also covers
    # the case this handler could not: a hard Lambda kill, where no `finally` runs at all and the
    # run previously left no failure record behind.
    identity = StageIdentity(
        tenant_code=tenant_code,
        source_id=source_id,
        entity_id=entity_id,
        run_id=run_id,
        environment=environment,
        stage=PipelineStage.ANALYTICS_PUBLISH.value,
        dlq_stage=DlqStage.ANALYTICS_PUBLISH,
        correlation_id=derive_correlation_id(run_id, event.get("replay_of_run_id")),
    )

    with stage_execution(identity, region_name=require_env("AWS_REGION"), lambda_context=context):
        result = _run_analytics_publication(
            source_id=source_id,
            entity_id=entity_id,
            environment=environment,
            run_id=run_id,
            canonical_prefix=canonical_prefix,
            tenant_code=tenant_code,
            run_started_at=run_started_at,
            stage_start_ms=_stage_start_ms,
        )
        # L17: the analytics publish is where a run finishes, so it is where the period's usage
        # can be recomputed from the audit log. Recomputed rather than incremented — see
        # `TenantUsageRepository.save` for why an increment would double-count on a retry.
        _record_tenant_usage(
            tenant_code=tenant_code,
            environment=environment,
            region_name=require_env("AWS_REGION"),
        )
        return result


def _run_analytics_publication(
    source_id: str,
    entity_id: str,
    environment: str,
    run_id: str,
    canonical_prefix: str,
    tenant_code: str,
    run_started_at: str | None,
    stage_start_ms: float,
) -> dict[str, Any]:
    """Business logic for the analytics publication stage, isolated from handler plumbing."""
    # ── Env vars ─────────────────────────────────────────────────────────────
    region_name = require_env("AWS_REGION")
    analytics_s3_bucket = require_env("ANALYTICS_S3_BUCKET")
    glue_catalog_database = require_env("GLUE_CATALOG_DATABASE")
    # GOVERNANCE_S3_BUCKET (see module docstring) is read by future lineage-recording
    # logic once it lands; not consumed yet, so it is intentionally not read here.

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

    analytics_date = datetime.now(UTC).date()
    analytics_date_str = analytics_date.isoformat()

    _logger.info(
        "analytics_publisher_handler_invoked",
        source_id=source_id,
        entity_id=entity_id,
        entity_type=entity_type,
        environment=environment,
        run_id=run_id,
        canonical_prefix=canonical_prefix,
        analytics_date=analytics_date_str,
    )

    s3 = boto3.client("s3", region_name=region_name)

    # ── Load golden records from the analytics layer (written by ER stage) ────
    golden_records = _load_parquet_records(s3, analytics_s3_bucket, canonical_prefix)
    if not golden_records:
        raise ValueError(
            f"No golden records found at s3://{analytics_s3_bucket}/{canonical_prefix}. "
            "Ensure the entity resolution stage completed successfully."
        )

    _logger.info(
        "analytics_publisher_golden_records_loaded",
        entity_type=entity_type,
        golden_record_count=len(golden_records),
    )

    # ── Strip internal ER system fields, keep golden_id + all business fields ─
    analytics_records = [
        {k: v for k, v in rec.items() if k not in _INTERNAL_FIELDS_TO_DROP}
        for rec in golden_records
    ]

    # ── Write analytics Parquet (§3.3 — multipart upload for large files) ─────
    # Tenant-scoped root prefix, matching the {tenant_code}/... convention
    # already used by the raw and curated layers — without it, two tenants
    # publishing the same entity_type on the same day overwrite each other's
    # entire daily analytics dataset (no run_id in this key to disambiguate).
    analytics_prefix = f"{tenant_code}/analytics/{entity_type}/analytics_date={analytics_date_str}/"
    analytics_key = f"{analytics_prefix}data.parquet"

    s3_writer = S3ParquetWriter(s3)
    record_count = s3_writer.write(
        records_iter=iter(analytics_records),
        bucket=analytics_s3_bucket,
        key=analytics_key,
        compression="snappy",
    )

    # PERF-3: reuse the schema S3ParquetWriter already inferred while writing
    # the Parquet file, instead of a second full pa.Table.from_pylist(...)
    # materialisation of analytics_records purely to recompute the same
    # schema for Glue registration.
    arrow_schema = s3_writer.last_written_schema
    if arrow_schema is None:
        arrow_schema = pa.schema([])

    _logger.info(
        "analytics_publisher_parquet_written",
        entity_type=entity_type,
        s3_key=analytics_key,
        record_count=record_count,
    )

    # ── Register / update Glue catalog table ─────────────────────────────────
    # One Glue table per (tenant, entity_type) — Glue/Athena table names only
    # allow [a-z0-9_] (governance/data_catalog_registration.py's
    # _SAFE_NAME_PATTERN), so tenant_code's hyphens are normalised to
    # underscores. Without this, two tenants' analytics for the same
    # entity_type would register the same table pointing at the same S3
    # location/partition, one clobbering the other's catalog entry.
    glue_table_name = f"{tenant_code.replace('-', '_')}_{entity_type}"
    glue_columns = _arrow_schema_to_glue_columns(
        arrow_schema, drop_partition_keys={"analytics_date"}
    )
    s3_location = f"s3://{analytics_s3_bucket}/{tenant_code}/analytics/{entity_type}/"

    catalog_client = DataCatalogRegistrationClient(region_name=region_name)
    spec = CatalogDatasetSpec(
        database_name=glue_catalog_database,
        table_name=glue_table_name,
        s3_location=s3_location,
        data_layer=DataLayer.ANALYTICS,
        owner="enterprise-data-lake",
        data_classification="internal",
        retention_days=365,
        source_lineage=(canonical_prefix,),
        partition_keys=("analytics_date",),
        schema=tuple(glue_columns),
        description=(
            f"Analytics-ready golden records for entity type '{entity_type}'. "
            f"Produced by entity resolution survivorship pipeline. "
            f"Partitioned by analytics_date."
        ),
    )

    try:
        catalog_result = catalog_client.register_dataset(spec)
        _logger.info(
            "analytics_publisher_catalog_registered",
            database=catalog_result.database_name,
            table=catalog_result.table_name,
            operation=catalog_result.operation,
        )

        # ── Register the partition for today so Athena can query it ──────────
        # The table uses Hive-style partitions; we register the value explicitly
        # so MSCK REPAIR TABLE is not needed after every run.
        glue_client = boto3.client("glue", region_name=region_name)
        glue_table_meta = glue_client.get_table(
            DatabaseName=glue_catalog_database, Name=glue_table_name
        )["Table"]
        part_sd = glue_table_meta["StorageDescriptor"].copy()
        part_sd["Location"] = f"s3://{analytics_s3_bucket}/{analytics_prefix}"
        try:
            glue_client.create_partition(
                DatabaseName=glue_catalog_database,
                TableName=glue_table_name,
                PartitionInput={"Values": [analytics_date_str], "StorageDescriptor": part_sd},
            )
        except glue_client.exceptions.AlreadyExistsException:
            glue_client.update_partition(
                DatabaseName=glue_catalog_database,
                TableName=glue_table_name,
                PartitionValueList=[analytics_date_str],
                PartitionInput={"Values": [analytics_date_str], "StorageDescriptor": part_sd},
            )
        _logger.info("analytics_publisher_partition_registered", analytics_date=analytics_date_str)

    except Exception as exc:
        # Catalog registration failure does not fail the pipeline — the Parquet
        # is already written and queryable via direct S3 path.  Log the error
        # for investigation and continue.
        _logger.warning(
            "analytics_publisher_catalog_registration_failed",
            entity_type=entity_type,
            error=str(exc),
        )

    published_at = datetime.now(UTC).isoformat()

    _emit_metrics_and_e2e_sla(
        region_name=region_name,
        tenant_code=tenant_code,
        source_id=source_id,
        entity_id=entity_id,
        environment=environment,
        stage_start_ms=stage_start_ms,
        record_count=record_count,
        run_started_at=run_started_at,
    )

    return {
        "analytics_s3_prefix": analytics_prefix,
        "entity_type": entity_type,
        "record_count": record_count,
        "glue_table": f"{glue_catalog_database}.{glue_table_name}",
        "analytics_date": analytics_date_str,
        "published_at": published_at,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emit_metrics_and_e2e_sla(
    region_name: str,
    tenant_code: str,
    source_id: str,
    entity_id: str,
    environment: str,
    stage_start_ms: float,
    record_count: int,
    run_started_at: str | None,
) -> None:
    """
    Emit CloudWatch metrics (§5.2) plus the end-to-end pipeline SLA metric
    (§5.7 / OBS-4) when `run_started_at` was threaded through Step Functions.

    Metric emission must never fail the pipeline run — all failures are
    logged as warnings, never raised (OWASP A09 — graceful degradation).
    """
    try:
        _metrics_emitter = CloudWatchMetricsEmitter(region_name=region_name)
        _metrics_emitter.set_tenant_context(tenant_code)
        _stage_duration_ms = time.monotonic() * 1000 - stage_start_ms
        _metrics_emitter.emit_stage_duration(
            source_id=source_id,
            entity_id=entity_id,
            environment=environment,
            stage="analytics_publication",
            duration_ms=_stage_duration_ms,
        )
        _metrics_emitter.emit_records_extracted(
            source_id=source_id,
            entity_id=entity_id,
            environment=environment,
            count=record_count,
            stage="analytics_publication",
        )
        if run_started_at is not None:
            # Skipped gracefully when run_started_at was not threaded through
            # Step Functions Parameters for this run (e.g. older executions).
            try:
                e2e_duration_ms = (
                    datetime.now(UTC) - datetime.fromisoformat(run_started_at)
                ).total_seconds() * 1000
                _metrics_emitter.emit_stage_duration(
                    source_id=source_id,
                    entity_id=entity_id,
                    environment=environment,
                    stage="e2e_pipeline",
                    duration_ms=e2e_duration_ms,
                )
            except ValueError:
                _logger.warning(
                    "analytics_publisher_invalid_run_started_at",
                    run_started_at=run_started_at,
                )
        _metrics_emitter.flush()
    except Exception as _exc:
        _logger.warning("analytics_publisher_metrics_emission_failed", error=str(_exc))


def _load_parquet_records(s3: Any, bucket: str, prefix: str) -> list[dict[str, Any]]:
    """Load all Parquet files from an S3 prefix into a list of dicts.

    Uses RecordBatch iteration (§2.3) — 10K rows materialised at a time.
    """
    clean = prefix.strip().rstrip("/") + "/"
    if ".." in clean or clean.startswith("/"):
        raise ValueError(f"Unsafe S3 prefix rejected: {clean!r}")

    paginator = s3.get_paginator("list_objects_v2")
    records: list[dict[str, Any]] = []

    for page in paginator.paginate(Bucket=bucket, Prefix=clean):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".parquet"):
                continue
            raw = s3.get_object(Bucket=bucket, Key=obj["Key"])
            buf = io.BytesIO(raw["Body"].read())
            table = pq.read_table(buf)  # type: ignore[no-untyped-call]
            for batch in table.to_batches(max_chunksize=10_000):
                batch_dict = batch.to_pydict()
                n = batch.num_rows
                cols = list(batch_dict.keys())
                records.extend({col: batch_dict[col][i] for col in cols} for i in range(n))
            del table

    return records


def _arrow_schema_to_glue_columns(
    schema: pa.Schema,
    drop_partition_keys: set[str],
) -> list[dict[str, str]]:
    """Convert a PyArrow schema to the Glue StorageDescriptor Columns format."""
    columns: list[dict[str, str]] = []
    for field in schema:
        if field.name in drop_partition_keys:
            continue
        glue_type = _arrow_type_to_glue(field.type)
        columns.append({"Name": field.name, "Type": glue_type})
    return columns


def _arrow_type_to_glue(arrow_type: pa.DataType) -> str:
    """Map a PyArrow DataType to the nearest Glue/Athena type string."""
    type_str = str(arrow_type)
    # Normalise timestamp variants: timestamp[us, tz=UTC] → "timestamp[us]"
    if type_str.startswith("timestamp"):
        return "timestamp"
    return _ARROW_TO_GLUE_TYPE.get(type_str, "string")


def _validate_event(event: dict[str, Any]) -> None:
    """Validate the Step Functions event payload (OWASP A03)."""
    missing = _REQUIRED_EVENT_FIELDS - set(event.keys())
    if missing:
        raise ValueError(f"Missing required event fields: {sorted(missing)}")

    for field in ("source_id", "entity_id", "run_id"):
        value = str(event[field])
        if not _STABLE_ID_PATTERN.match(value):
            raise ValueError(f"Event field {field}={value!r} contains disallowed characters.")

    environment = str(event["environment"])
    if environment not in _KNOWN_ENVIRONMENTS:
        raise ValueError(
            f"Unknown environment={environment!r}. Expected one of {sorted(_KNOWN_ENVIRONMENTS)}."
        )

    for prefix_field in ("canonical_prefix", "curated_s3_prefix"):
        val = str(event[prefix_field])
        if not _SAFE_S3_PREFIX_PATTERN.match(val.rstrip("/")):
            raise ValueError(f"{prefix_field}={val!r} contains disallowed characters.")

    # tenant_code is required (ARCH-4) and must always be well-formed
    # (OWASP A03 / SEC-5) — a missing or malformed tenant_code must fail
    # closed rather than silently default to another tenant's identity.
    tenant_code = str(event["tenant_code"])
    if not TENANT_CODE_PATTERN.match(tenant_code):
        raise ValueError(f"tenant_code={tenant_code!r} does not conform to the tenant code format.")


def _record_tenant_usage(*, tenant_code: str, environment: str, region_name: str) -> None:
    """
    Recompute this tenant's usage for the current period from the run audit log.

    Never raises: metering is billing *input*, and a metering failure must not fail a pipeline run
    that has already published its data. A missed period is recomputable from the audit log, which
    is exactly why the audit log is the source of truth rather than a counter.
    """
    try:
        period = current_period()
        records = read_audit_records_for_period(
            tenant_code=tenant_code, period=period, region_name=region_name
        )
        usage = aggregate_usage(records, tenant_code, period)
        TenantUsageRepository(environment=environment, region_name=region_name).save(usage)
    except Exception as exc:
        _logger.warning(
            "tenant_usage_metering_failed",
            tenant_code=tenant_code,
            error=str(exc),
            error_type=type(exc).__name__,
        )
