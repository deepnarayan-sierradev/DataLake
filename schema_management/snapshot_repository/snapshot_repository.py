"""
S3-backed schema snapshot repository for the Enterprise Data Lake platform.

A schema snapshot is an immutable record of the field set for a source entity
at a specific point in time.  Snapshots are written after every successful
extraction run and are read by the schema drift evaluator to detect changes
between runs.

S3 key format (snapshot):
  {source_id}/{entity_id}/{schema_version}/{extraction_date}.json

S3 key format (latest-pointer index):
  {source_id}/{entity_id}/latest.json

The latest-pointer is the only mutable object — it is overwritten on every
successful run to point at the most recent snapshot key.  All snapshot objects
are append-only (inherited from bucket Object Lock policy).

Security:
  - Snapshots contain only structural metadata (field names, types, flags).
    Field VALUES are never included.
  - S3 writes use the bucket's default SSE-KMS encryption.
  - No ACL is set; access is governed by the extraction_runtime IAM role policy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError

from contracts.identifier_policy import STABLE_ID_PATTERN as _STABLE_ID_PATTERN
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_SNAPSHOT_KEY_TEMPLATE: Final[str] = (
    "{source_id}/{entity_id}/{schema_version}/{extraction_date}.json"
)
_LATEST_POINTER_KEY_TEMPLATE: Final[str] = "{source_id}/{entity_id}/latest.json"
_DRIFT_REPORT_KEY_TEMPLATE: Final[str] = (
    "{source_id}/{entity_id}/{schema_version}/drift-report-{extraction_date}.json"
)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSnapshot:
    """
    Immutable structural description of a single source field.

    Contains only schema metadata — field values are never stored here.
    """

    name: str
    data_type: str
    is_nullable: bool
    is_queryable: bool
    length: int | None = None
    precision: int | None = None
    scale: int | None = None
    is_custom: bool = False


@dataclass(frozen=True)
class SchemaSnapshot:
    """
    Immutable record of the complete field set for a source entity at one point
    in time.

    Written once per successful extraction run; never overwritten.
    The schema_version is the SHA-256 fingerprint produced by
    FieldContract.compute_fingerprint() and serves as the stable content hash.
    """

    source_id: str
    entity_id: str
    schema_version: str  # SHA-256 fingerprint (from FieldContract.compute_fingerprint)
    extraction_date: str  # ISO date string: YYYY-MM-DD
    captured_at: str  # ISO datetime string (UTC)
    fields: tuple[FieldSnapshot, ...]
    record_count: int | None = None


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class SchemaSnapshotRepository:
    """
    Writes and reads immutable SchemaSnapshot records to/from S3.

    Each snapshot is a distinct S3 object keyed by schema_version and
    extraction_date — existing snapshots are never overwritten.
    """

    def __init__(self, bucket_name: str, region_name: str) -> None:
        if not bucket_name:
            raise ValueError("bucket_name must not be empty.")
        self._bucket = bucket_name
        self._s3 = boto3.client("s3", region_name=region_name)

    # ── Write ──────────────────────────────────────────────────────────────────

    def write_snapshot(self, snapshot: SchemaSnapshot) -> str:
        """
        Persist a snapshot to S3 and update the latest-pointer index.

        Returns the S3 key the snapshot was written to.
        The bucket's default SSE-KMS encryption is inherited — no key override.

        If the latest-pointer update fails (e.g. transient S3 error), the snapshot
        object is still written and the key is returned.  The pointer is a lookup
        convenience; the next successful run will re-establish it.  A warning is
        logged so operators can detect prolonged pointer staleness.
        """
        if not _STABLE_ID_PATTERN.match(snapshot.source_id):
            raise ValueError(
                f"source_id={snapshot.source_id!r} does not conform to the stable "
                "identifier format and cannot be used in an S3 key."
            )
        if not _STABLE_ID_PATTERN.match(snapshot.entity_id):
            raise ValueError(
                f"entity_id={snapshot.entity_id!r} does not conform to the stable "
                "identifier format and cannot be used in an S3 key."
            )
        key = _SNAPSHOT_KEY_TEMPLATE.format(
            source_id=snapshot.source_id,
            entity_id=snapshot.entity_id,
            schema_version=snapshot.schema_version,
            extraction_date=snapshot.extraction_date,
        )
        body = json.dumps(_serialise_snapshot(snapshot), separators=(",", ":")).encode("utf-8")
        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
        )
        try:
            self._write_latest_pointer(snapshot.source_id, snapshot.entity_id, key)
        except ClientError:
            _logger.warning(
                "snapshot_pointer_write_failed",
                source_id=snapshot.source_id,
                entity_id=snapshot.entity_id,
                snapshot_key=key,
            )
            # The snapshot is durably written and recoverable via the returned key.
            # The next successful run will re-establish the latest-pointer.
        return key

    def write_drift_report(
        self,
        source_id: str,
        entity_id: str,
        schema_version: str,
        extraction_date: str,
        report_json: str,
    ) -> str:
        """
        Persist a drift report to S3 alongside its corresponding snapshot.

        S3 key: {source_id}/{entity_id}/{schema_version}/drift-report-{extraction_date}.json

        Accepts the JSON-serialised drift report string (from DriftReport.to_json())
        to avoid a circular import between snapshot_repository and drift_evaluator.
        Reports contain only structural metadata — field values are never included.
        """
        key = _DRIFT_REPORT_KEY_TEMPLATE.format(
            source_id=source_id,
            entity_id=entity_id,
            schema_version=schema_version,
            extraction_date=extraction_date,
        )
        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=report_json.encode("utf-8"),
            ContentType="application/json",
        )
        return key

    # ── Read ───────────────────────────────────────────────────────────────────

    def load_latest_snapshot(self, source_id: str, entity_id: str) -> SchemaSnapshot | None:
        """
        Load the most recent snapshot for a source entity.

        Returns None when no snapshot has been written (first extraction run).
        """
        pointer_key = _LATEST_POINTER_KEY_TEMPLATE.format(source_id=source_id, entity_id=entity_id)
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=pointer_key)
            index: dict[str, str] = json.loads(response["Body"].read().decode("utf-8"))
            snapshot_key = index["snapshot_key"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise

        return self.load_snapshot_by_key(snapshot_key)

    def load_snapshot_by_key(self, s3_key: str) -> SchemaSnapshot:
        """Load a specific snapshot by its full S3 key."""
        response = self._s3.get_object(Bucket=self._bucket, Key=s3_key)
        raw: dict[str, Any] = json.loads(response["Body"].read().decode("utf-8"))
        return _deserialise_snapshot(raw)

    # ── Private ────────────────────────────────────────────────────────────────

    def _write_latest_pointer(self, source_id: str, entity_id: str, snapshot_key: str) -> None:
        pointer_key = _LATEST_POINTER_KEY_TEMPLATE.format(source_id=source_id, entity_id=entity_id)
        body = json.dumps({"snapshot_key": snapshot_key}, separators=(",", ":")).encode("utf-8")
        self._s3.put_object(
            Bucket=self._bucket,
            Key=pointer_key,
            Body=body,
            ContentType="application/json",
        )


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _serialise_snapshot(snapshot: SchemaSnapshot) -> dict[str, Any]:
    return {
        "source_id": snapshot.source_id,
        "entity_id": snapshot.entity_id,
        "schema_version": snapshot.schema_version,
        "extraction_date": snapshot.extraction_date,
        "captured_at": snapshot.captured_at,
        "record_count": snapshot.record_count,
        "fields": [
            {
                "name": f.name,
                "data_type": f.data_type,
                "is_nullable": f.is_nullable,
                "is_queryable": f.is_queryable,
                "length": f.length,
                "precision": f.precision,
                "scale": f.scale,
                "is_custom": f.is_custom,
            }
            for f in snapshot.fields
        ],
    }


def _deserialise_snapshot(raw: dict[str, Any]) -> SchemaSnapshot:
    fields = tuple(
        FieldSnapshot(
            name=f["name"],
            data_type=f["data_type"],
            is_nullable=f["is_nullable"],
            is_queryable=f["is_queryable"],
            length=f.get("length"),
            precision=f.get("precision"),
            scale=f.get("scale"),
            is_custom=f.get("is_custom", False),
        )
        for f in raw["fields"]
    )
    return SchemaSnapshot(
        source_id=raw["source_id"],
        entity_id=raw["entity_id"],
        schema_version=raw["schema_version"],
        extraction_date=raw["extraction_date"],
        captured_at=raw["captured_at"],
        record_count=raw.get("record_count"),
        fields=fields,
    )
