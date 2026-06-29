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
import os
import re
from datetime import UTC, datetime
from typing import Any, Final

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

from contracts.identifier_policy import STABLE_ID_PATTERN as _STABLE_ID_PATTERN
from governance.data_catalog_registration import (
    CatalogDatasetSpec,
    DataCatalogRegistrationClient,
    DataLayer,
)
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# ---------------------------------------------------------------------------
# Entity type registry — mirrors entity_resolution_pipeline_handler.py
# ---------------------------------------------------------------------------

_ENTITY_ID_TO_TYPE: Final[dict[str, str]] = {
    "salesforce-account":  "company",
    "netsuite-customer":   "company",
    "salesforce-contact":  "person",
    "mysql-rds-contracts": "contract",
}

# ---------------------------------------------------------------------------
# Fields removed from golden records before writing the BI analytics layer.
# These are internal entity resolution system fields that are useful for
# debugging/auditing but create noise in BI tools and Athena queries.
# golden_id is KEPT — it is the stable key for joins across entity types.
# ---------------------------------------------------------------------------

_INTERNAL_FIELDS_TO_DROP: Final[frozenset[str]] = frozenset({
    "_record_id",          # cross-source surrogate key — internal to ER pipeline
    "_source_id",          # source tag injected by ER handler — internal
    "contributing_source_records",  # list of source record IDs — ER audit detail
    "survivorship_version",         # policy version applied — ER audit detail
    "match_run_id",                 # ER run identifier — duplicated in partition path
    "field_provenance",             # JSON string — per-field winner metadata
})

# ---------------------------------------------------------------------------
# Validation constants (OWASP A03)
# ---------------------------------------------------------------------------

_REQUIRED_EVENT_FIELDS: Final[frozenset[str]] = frozenset(
    {"source_id", "entity_id", "environment", "run_id", "canonical_prefix", "curated_s3_prefix"}
)
_KNOWN_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"dev", "staging", "prod"})
_SAFE_S3_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9\-_/=\.]{0,511}$"
)

# PyArrow type → Glue/Athena column type string
_ARROW_TO_GLUE_TYPE: Final[dict[str, str]] = {
    "int8":    "tinyint",
    "int16":   "smallint",
    "int32":   "int",
    "int64":   "bigint",
    "uint8":   "tinyint",
    "uint16":  "smallint",
    "uint32":  "int",
    "uint64":  "bigint",
    "float":   "float",
    "double":  "double",
    "decimal128": "double",
    "bool":    "boolean",
    "date32":  "date",
    "date64":  "date",
    "timestamp[s]":  "timestamp",
    "timestamp[ms]": "timestamp",
    "timestamp[us]": "timestamp",
    "timestamp[ns]": "timestamp",
    "string":  "string",
    "large_string": "string",
    "utf8":    "string",
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

    source_id: str = event["source_id"]
    entity_id: str = event["entity_id"]
    environment: str = event["environment"]
    run_id: str = event["run_id"]
    canonical_prefix: str = event["canonical_prefix"]

    # ── Env vars ─────────────────────────────────────────────────────────────
    region_name = _require_env("AWS_REGION")
    analytics_s3_bucket = _require_env("ANALYTICS_S3_BUCKET")
    glue_catalog_database = _require_env("GLUE_CATALOG_DATABASE")
    governance_s3_bucket: str | None = os.environ.get("GOVERNANCE_S3_BUCKET") or None

    # ── Resolve entity type ───────────────────────────────────────────────────
    entity_type = _ENTITY_ID_TO_TYPE.get(entity_id)
    if entity_type is None:
        raise ValueError(
            f"No entity type mapping found for entity_id={entity_id!r}. "
            "Add it to _ENTITY_ID_TO_TYPE in analytics_publisher_handler.py."
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

    # ── Write analytics Parquet ───────────────────────────────────────────────
    analytics_prefix = (
        f"analytics/{entity_type}"
        f"/analytics_date={analytics_date_str}/"
    )
    analytics_key = f"{analytics_prefix}data.parquet"

    parquet_bytes, arrow_schema = _to_parquet_with_schema(analytics_records)
    s3.put_object(
        Bucket=analytics_s3_bucket,
        Key=analytics_key,
        Body=parquet_bytes,
        ContentType="application/octet-stream",
    )

    _logger.info(
        "analytics_publisher_parquet_written",
        entity_type=entity_type,
        s3_key=analytics_key,
        record_count=len(analytics_records),
        size_bytes=len(parquet_bytes),
    )

    # ── Register / update Glue catalog table ─────────────────────────────────
    glue_table_name = entity_type  # e.g. "company", "person", "contract"
    glue_columns = _arrow_schema_to_glue_columns(arrow_schema, drop_partition_keys={"analytics_date"})
    s3_location = f"s3://{analytics_s3_bucket}/analytics/{entity_type}/"

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

    except Exception as exc:  # noqa: BLE001 — best-effort catalog registration
        # Catalog registration failure does not fail the pipeline — the Parquet
        # is already written and queryable via direct S3 path.  Log the error
        # for investigation and continue.
        _logger.warning(
            "analytics_publisher_catalog_registration_failed",
            entity_type=entity_type,
            error=str(exc),
        )

    published_at = datetime.now(UTC).isoformat()

    return {
        "analytics_s3_prefix": analytics_prefix,
        "entity_type":         entity_type,
        "record_count":        len(analytics_records),
        "glue_table":          f"{glue_catalog_database}.{glue_table_name}",
        "analytics_date":      analytics_date_str,
        "published_at":        published_at,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_parquet_records(
    s3: Any, bucket: str, prefix: str
) -> list[dict[str, Any]]:
    """Load all Parquet files from an S3 prefix into a list of dicts."""
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
            records.extend(table.to_pylist())
            del table

    return records


def _to_parquet_with_schema(
    records: list[dict[str, Any]],
) -> tuple[bytes, pa.Schema]:
    """Serialise records to Parquet bytes and return (bytes, schema)."""
    # Flatten any remaining list/dict fields to JSON strings for Parquet compat.
    import json as _json

    flat = [
        {
            k: _json.dumps(v) if isinstance(v, (list, dict)) else v
            for k, v in r.items()
        }
        for r in records
    ]
    table = pa.Table.from_pylist(flat)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")  # type: ignore[no-untyped-call]
    return buf.getvalue(), table.schema


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


def _require_env(name: str) -> str:
    """Return environment variable value or raise RuntimeError if absent."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            "Configure it in the Lambda function's environment variables."
        )
    return value


def _validate_event(event: dict[str, Any]) -> None:
    """Validate the Step Functions event payload (OWASP A03)."""
    missing = _REQUIRED_EVENT_FIELDS - set(event.keys())
    if missing:
        raise ValueError(f"Missing required event fields: {sorted(missing)}")

    for field in ("source_id", "entity_id", "run_id"):
        value = str(event[field])
        if not _STABLE_ID_PATTERN.match(value):
            raise ValueError(
                f"Event field {field}={value!r} contains disallowed characters."
            )

    environment = str(event["environment"])
    if environment not in _KNOWN_ENVIRONMENTS:
        raise ValueError(
            f"Unknown environment={environment!r}. "
            f"Expected one of {sorted(_KNOWN_ENVIRONMENTS)}."
        )

    for prefix_field in ("canonical_prefix", "curated_s3_prefix"):
        val = str(event[prefix_field])
        if not _SAFE_S3_PREFIX_PATTERN.match(val.rstrip("/")):
            raise ValueError(
                f"{prefix_field}={val!r} contains disallowed characters."
            )
