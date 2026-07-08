"""
Salesforce raw layer writer — Phase 3 deliverable §3.5.

Writes batches of ExtractionRecord to the S3 raw layer as Parquet files.

Partition scheme:
    s3://{bucket}/{tenant_code}/salesforce/{entity_id}/
        extraction_date={YYYY-MM-DD}/
        run_id={run_id}/
            data.parquet
            metadata.json

Extraction metadata written alongside payload:
    run_id, source_id, entity_id, extraction_timestamp, schema_version, record_count

Design requirements:
  - Append-only writes — each run produces a unique partition path via run_id.
  - No overwrites of prior raw files — the run_id partition guarantees uniqueness.
  - Raw records match source payload structure with no transformation artifacts.
  - Parquet format chosen for columnar compression and downstream Athena/Glue
    compatibility.

Security (OWASP A05, A09):
  - S3 keys constructed from validated entity_id (stable-id pattern check) to
    prevent path traversal.
  - Record payloads are written as-is — no values are logged.
  - IAM credentials come from the implicit boto3 credential chain (IAM role).

DUP-1: the write_partition / write_partition_streaming / _records_to_parquet /
_partition_path / _validate_stable_id logic below is shared verbatim with the
NetSuite, MySQL RDS, and Sage raw layer writers — see
connector_runtime/raw_layer_writer.RawLayerWriter, which this class subclasses
rather than reimplementing that logic.
"""

from __future__ import annotations

from typing import Final

from connector_runtime.raw_layer_writer import RawLayerWriter, RawLayerWriterError

_SOURCE_NAME: Final[str] = "salesforce"


class SalesforceRawLayerWriterError(RawLayerWriterError):
    """Raised when raw layer writing fails in a way that aborts the extraction run."""


class SalesforceRawLayerWriter(RawLayerWriter):
    """
    Writes a batch of ExtractionRecord objects to the S3 raw layer as Parquet.

    One instance per extraction run.  The writer is responsible only for
    persistence — it does not perform any transformation or field filtering.

    Usage::

        writer = SalesforceRawLayerWriter(
            s3_bucket="prod-raw-layer",
            region_name="us-east-1",
            tenant_code="demo",
        )
        data_key = writer.write_partition(
            records=records,
            source_id="salesforce",
            entity_id="salesforce-account",
            run_id="run-20260612-120000000000-ab12cd34",
            schema_fingerprint="a1b2c3d4...",
            extraction_date="2026-06-12",
        )
    """

    error_cls = SalesforceRawLayerWriterError
    log_prefix = "salesforce"

    def __init__(
        self,
        s3_bucket: str,
        region_name: str,
        tenant_code: str,
    ) -> None:
        super().__init__(
            s3_bucket=s3_bucket,
            path_segments=[_SOURCE_NAME],
            region_name=region_name,
            tenant_code=tenant_code,
        )
