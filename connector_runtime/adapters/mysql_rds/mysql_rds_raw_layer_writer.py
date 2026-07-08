"""
MySQL RDS raw layer writer.

Writes batches of ExtractionRecord to the S3 raw layer as Parquet files,
following the platform raw layer partition scheme for MySQL RDS.

Partition scheme:
    s3://{bucket}/{tenant_code}/mysql-rds/{entity_id}/
        extraction_date={YYYY-MM-DD}/
        run_id={run_id}/
            data.parquet
            metadata.json

Security (OWASP A05, A09):
  - S3 keys constructed from validated entity_id (stable-id pattern check).
  - Record payloads are written as-is — no values are logged.
  - IAM credentials come from the implicit boto3 credential chain (IAM role).

DUP-1: the write_partition / write_partition_streaming / _records_to_parquet /
_partition_path / _validate_stable_id logic below is shared verbatim with the
Salesforce, NetSuite, and Sage raw layer writers — see
connector_runtime/raw_layer_writer.RawLayerWriter, which this class subclasses
rather than reimplementing that logic.
"""

from __future__ import annotations

from typing import Final

from connector_runtime.raw_layer_writer import RawLayerWriter, RawLayerWriterError

_SOURCE_NAME: Final[str] = "mysql-rds"


class MySqlRdsRawLayerWriterError(RawLayerWriterError):
    """Raised when raw layer writing fails in a way that aborts the extraction run."""


class MySqlRdsRawLayerWriter(RawLayerWriter):
    """
    Writes a batch of ExtractionRecord objects to the S3 raw layer as Parquet.

    One instance per extraction run.  Responsible only for persistence —
    no transformation or field filtering is applied.

    Usage::

        writer = MySqlRdsRawLayerWriter(
            s3_bucket="prod-raw-layer",
            region_name="us-east-1",
            tenant_code="demo",
        )
        data_key = writer.write_partition(
            records=records,
            source_id="mysql-rds",
            entity_id="mysql-rds-orders",
            run_id="run-20260612-120000000000-ab12cd34",
            schema_fingerprint="a1b2c3d4...",
            extraction_date="2026-06-12",
        )
    """

    error_cls = MySqlRdsRawLayerWriterError
    log_prefix = "mysql_rds"

    def __init__(self, s3_bucket: str, region_name: str, tenant_code: str) -> None:
        super().__init__(
            s3_bucket=s3_bucket,
            path_segments=[_SOURCE_NAME],
            region_name=region_name,
            tenant_code=tenant_code,
        )
