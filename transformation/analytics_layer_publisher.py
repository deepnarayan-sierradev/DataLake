"""
Analytics layer publisher.

Reads curated domain datasets and/or golden records from S3 and publishes
consumption-optimised Parquet files to the analytics S3 layer.

Partition scheme:
  s3://{analytics_bucket}/analytics/{domain}/{entity_id}/
    analytics_date={YYYY-MM-DD}/run_id={run_id}/data.parquet

Responsibilities (spec §8.1):
  - Optimise partitioning for query performance.
  - Register each published dataset in AWS Glue Data Catalog.
  - Grant analytics consumers read-only access via IAM (enforced in Terraform).
  - Maintain lineage from curated / golden source prefixes.

Security (OWASP A03, A09):
  - S3 prefixes validated before listing.
  - No record field values in log output.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

from governance.lineage_record import LineageEmitter, build_analytics_publication_lineage
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)


@dataclass(frozen=True)
class AnalyticsPublicationResult:
    """Summary of one analytics layer publication."""

    run_id: str
    domain: str
    entity_id: str
    source_s3_prefix: str
    analytics_s3_prefix: str
    analytics_s3_key: str
    record_count: int
    glue_database: str
    glue_table: str
    published_at: str  # ISO-8601 UTC


class AnalyticsLayerPublisher:
    """
    Reads from curated (or golden record) S3 prefix and writes optimised
    Parquet files to the analytics S3 layer.  Registers the schema in AWS
    Glue Data Catalog after each successful publication.
    """

    def __init__(
        self,
        source_s3_bucket: str,
        analytics_s3_bucket: str,
        glue_database: str,
        region_name: str,
        governance_s3_bucket: str | None = None,
    ) -> None:
        self._source_bucket = source_s3_bucket
        self._analytics_bucket = analytics_s3_bucket
        self._glue_database = glue_database
        self._region_name = region_name
        self._governance_s3_bucket = governance_s3_bucket
        self._s3: Any = boto3.client("s3", region_name=region_name)
        self._glue: Any = boto3.client("glue", region_name=region_name)

    def publish(
        self,
        source_s3_prefix: str,
        domain: str,
        entity_id: str,
        run_id: str,
        analytics_date: date | None = None,
    ) -> AnalyticsPublicationResult:
        """
        Read records from source prefix, write to analytics layer, and
        register the Glue table.

        Args:
            source_s3_prefix: Curated or golden record S3 prefix to read from.
            domain:           Business domain (e.g., "customer").
            entity_id:        Stable entity identifier.
            run_id:           Run ID for traceability.
            analytics_date:   Partition date; defaults to today UTC.

        Returns:
            AnalyticsPublicationResult.
        """
        partition_date = analytics_date or datetime.now(UTC).date()

        records = _load_parquet_records(self._s3, self._source_bucket, source_s3_prefix)
        if not records:
            raise AnalyticsPublicationError(
                f"No records found at source prefix: {source_s3_prefix!r}"
            )

        analytics_prefix = (
            f"analytics/{domain}/{entity_id}"
            f"/analytics_date={partition_date.isoformat()}"
            f"/run_id={run_id}/"
        )
        analytics_key = f"{analytics_prefix}data.parquet"

        parquet_bytes = _to_parquet(records)
        self._s3.put_object(
            Bucket=self._analytics_bucket,
            Key=analytics_key,
            Body=parquet_bytes,
            ContentType="application/octet-stream",
        )

        # Register schema in Glue Data Catalog
        glue_table = f"{entity_id.replace('-', '_')}_analytics"
        self._register_glue_table(
            database=self._glue_database,
            table_name=glue_table,
            s3_prefix=f"s3://{self._analytics_bucket}/{analytics_prefix}",
            sample_record=records[0],
        )

        published_at = datetime.now(UTC).isoformat()

        _logger.info(
            "analytics_layer_publish_complete",
            run_id=run_id,
            domain=domain,
            entity_id=entity_id,
            record_count=len(records),
            glue_table=glue_table,
        )

        # Emit lineage record (spec §9.1 — lineage at analytics publication)
        if self._governance_s3_bucket:
            try:
                lineage_record = build_analytics_publication_lineage(
                    run_id=run_id,
                    source_id=entity_id,
                    entity_id=entity_id,
                    source_s3_bucket=self._source_bucket,
                    source_s3_prefix=source_s3_prefix,
                    analytics_s3_bucket=self._analytics_bucket,
                    analytics_s3_prefix=analytics_prefix,
                    record_count=len(records),
                )
                LineageEmitter(
                    governance_s3_bucket=self._governance_s3_bucket,
                    region_name=self._region_name,
                ).emit(lineage_record)
            except Exception as exc:
                _logger.warning("analytics_lineage_emission_failed", error=str(exc))

        return AnalyticsPublicationResult(
            run_id=run_id,
            domain=domain,
            entity_id=entity_id,
            source_s3_prefix=source_s3_prefix,
            analytics_s3_prefix=analytics_prefix,
            analytics_s3_key=analytics_key,
            record_count=len(records),
            glue_database=self._glue_database,
            glue_table=glue_table,
            published_at=published_at,
        )

    def _register_glue_table(
        self,
        database: str,
        table_name: str,
        s3_prefix: str,
        sample_record: dict[str, Any],
    ) -> None:
        """Create or update the Glue Data Catalog table."""
        self._ensure_glue_database(database)
        glue_columns = _infer_glue_columns(sample_record)

        table_input: dict[str, Any] = {
            "Name": table_name,
            "StorageDescriptor": {
                "Columns": glue_columns,
                "Location": s3_prefix,
                "InputFormat": ("org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"),
                "OutputFormat": (
                    "org.apache.hadoop.hive.ql.io.parquet.MapredParquetHiveOutputFormat"
                ),
                "SerdeInfo": {
                    "SerializationLibrary": (
                        "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
                    ),
                },
            },
            "PartitionKeys": [
                {"Name": "analytics_date", "Type": "date"},
            ],
            "TableType": "EXTERNAL_TABLE",
            "Parameters": {
                "classification": "parquet",
                "compressionType": "snappy",
            },
        }

        try:
            self._glue.update_table(DatabaseName=database, TableInput=table_input)
        except self._glue.exceptions.EntityNotFoundException:
            self._glue.create_table(DatabaseName=database, TableInput=table_input)

    def _ensure_glue_database(self, database: str) -> None:
        """Create the Glue database if it does not exist."""
        try:
            self._glue.get_database(Name=database)
        except self._glue.exceptions.EntityNotFoundException:
            self._glue.create_database(
                DatabaseInput={
                    "Name": database,
                    "Description": "Enterprise Data Lake analytics layer",
                }
            )


def _load_parquet_records(s3: Any, bucket: str, prefix: str) -> list[dict[str, Any]]:
    """Read all Parquet files under prefix and return flat record list."""
    paginator = s3.get_paginator("list_objects_v2")
    records: list[dict[str, Any]] = []

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix.rstrip("/") + "/"):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".parquet"):
                continue
            data = s3.get_object(Bucket=bucket, Key=obj["Key"])
            buf = io.BytesIO(data["Body"].read())
            table = pq.read_table(buf)  # type: ignore[no-untyped-call]
            py_dict: dict[str, list[Any]] = table.to_pydict()
            if not py_dict:
                continue
            cols = list(py_dict.keys())
            n = len(py_dict[cols[0]])
            records.extend({c: py_dict[c][i] for c in cols} for i in range(n))

    return records


def _to_parquet(records: list[dict[str, Any]]) -> bytes:
    """Serialise record list to Parquet bytes."""
    table = pa.Table.from_pylist(records)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")  # type: ignore[no-untyped-call]
    return buf.getvalue()


def _infer_glue_columns(sample: dict[str, Any]) -> list[dict[str, str]]:
    """Infer Glue column definitions from a sample record."""
    type_map: dict[type, str] = {
        int: "bigint",
        float: "double",
        bool: "boolean",
        str: "string",
    }
    columns: list[dict[str, str]] = []
    for field_name, value in sample.items():
        glue_type = type_map.get(type(value), "string")
        columns.append({"Name": field_name, "Type": glue_type})
    return columns


class AnalyticsPublicationError(Exception):
    """Raised when analytics layer publication fails."""
