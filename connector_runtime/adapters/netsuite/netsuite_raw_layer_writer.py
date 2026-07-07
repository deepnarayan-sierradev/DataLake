"""
NetSuite raw layer writer.

Writes batches of ExtractionRecord to the S3 raw layer as Parquet files,
following the platform raw layer partition scheme for NetSuite.

Partition scheme (from spec §4.1):
    s3://{bucket}/{prefix}/netsuite/{entity_id}/
        extraction_date={YYYY-MM-DD}/
        run_id={run_id}/
            data.parquet
            metadata.json

Design requirements:
  - Append-only writes — each run produces a unique partition path via run_id.
  - No overwrites of prior raw files — the run_id partition guarantees uniqueness.
  - Raw records match source payload structure with no transformation artifacts.

Security (OWASP A05, A09):
  - S3 keys constructed from validated entity_id (stable-id pattern check).
  - Record payloads are written as-is — no values are logged.
  - IAM credentials come from the implicit boto3 credential chain (IAM role).

DUP-1: the write_partition / write_partition_streaming / _records_to_parquet /
_partition_path / _validate_stable_id logic below is shared verbatim with the
Salesforce, MySQL RDS, and Sage raw layer writers — see
connector_runtime/raw_layer_writer.RawLayerWriter, which this class subclasses
rather than reimplementing that logic.
"""

from __future__ import annotations

from typing import Final

from connector_runtime.raw_layer_writer import RawLayerWriter, RawLayerWriterError

_SOURCE_NAME: Final[str] = "netsuite"


class NetSuiteRawLayerWriterError(RawLayerWriterError):
    """Raised when raw layer writing fails in a way that aborts the extraction run."""


class NetSuiteRawLayerWriter(RawLayerWriter):
    """
    Writes a batch of ExtractionRecord objects to the S3 raw layer as Parquet.

    One instance per extraction run.  Responsible only for persistence —
    no transformation or field filtering is applied.

    Usage::

        writer = NetSuiteRawLayerWriter(
            s3_bucket="prod-raw-layer",
            s3_prefix="raw",
            region_name="us-east-1",
        )
        data_key = writer.write_partition(
            records=records,
            source_id="netsuite",
            entity_id="netsuite-customer",
            run_id="run-20260612-120000000000-ab12cd34",
            schema_fingerprint="a1b2c3d4...",
            extraction_date="2026-06-12",
        )
    """

    error_cls = NetSuiteRawLayerWriterError
    log_prefix = "netsuite"

    def __init__(self, s3_bucket: str, s3_prefix: str, region_name: str) -> None:
        super().__init__(
            s3_bucket=s3_bucket,
            s3_prefix=s3_prefix,
            path_segments=[_SOURCE_NAME],
            region_name=region_name,
        )
