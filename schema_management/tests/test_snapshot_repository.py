"""
Tests for the Schema Snapshot Repository (2.3).

Covers:
  - write_snapshot: writes to correct S3 key, returns key, updates latest pointer
  - load_latest_snapshot: returns None when no snapshot exists (first run)
  - load_latest_snapshot: returns the most recently written snapshot
  - load_snapshot_by_key: loads a specific snapshot by full key
  - Round-trip: written snapshot equals deserialized snapshot
"""

from __future__ import annotations

from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from schema_management.snapshot_repository.snapshot_repository import (
    FieldSnapshot,
    SchemaSnapshot,
    SchemaSnapshotRepository,
    _deserialise_snapshot,
    _serialise_snapshot,
)

_REGION = "us-east-1"
_BUCKET = "dev-schema-snapshots"


def _make_snapshot(
    source_id: str = "salesforce",
    entity_id: str = "salesforce-account",
    schema_version: str = "abc123def456",
    extraction_date: str = "2026-06-11",
    record_count: int | None = 1000,
) -> SchemaSnapshot:
    fields = (
        FieldSnapshot(name="Id", data_type="id", is_nullable=False, is_queryable=True),
        FieldSnapshot(
            name="Name", data_type="string", is_nullable=True, is_queryable=True, length=255
        ),
        FieldSnapshot(
            name="Amount",
            data_type="currency",
            is_nullable=True,
            is_queryable=True,
            precision=18,
            scale=2,
        ),
    )
    return SchemaSnapshot(
        source_id=source_id,
        entity_id=entity_id,
        schema_version=schema_version,
        extraction_date=extraction_date,
        captured_at=datetime(2026, 6, 11, 14, 0, 0, tzinfo=UTC).isoformat(),
        fields=fields,
        record_count=record_count,
    )


def _create_bucket() -> None:
    boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_BUCKET)


def _repo() -> SchemaSnapshotRepository:
    return SchemaSnapshotRepository(bucket_name=_BUCKET, region_name=_REGION)


# ---------------------------------------------------------------------------
# write_snapshot
# ---------------------------------------------------------------------------


class TestWriteSnapshot:
    @mock_aws
    def test_write_returns_correct_s3_key(self) -> None:
        _create_bucket()
        snap = _make_snapshot()
        key = _repo().write_snapshot(snap)
        assert key == "salesforce/salesforce-account/abc123def456/2026-06-11.json"

    @mock_aws
    def test_written_object_exists_in_s3(self) -> None:
        _create_bucket()
        snap = _make_snapshot()
        key = _repo().write_snapshot(snap)
        s3 = boto3.client("s3", region_name=_REGION)
        response = s3.get_object(Bucket=_BUCKET, Key=key)
        assert response["ContentType"] == "application/json"

    @mock_aws
    def test_write_updates_latest_pointer(self) -> None:
        _create_bucket()
        snap = _make_snapshot()
        key = _repo().write_snapshot(snap)
        s3 = boto3.client("s3", region_name=_REGION)
        pointer = s3.get_object(Bucket=_BUCKET, Key="salesforce/salesforce-account/latest.json")
        import json

        index = json.loads(pointer["Body"].read().decode("utf-8"))
        assert index["snapshot_key"] == key


# ---------------------------------------------------------------------------
# load_latest_snapshot
# ---------------------------------------------------------------------------


class TestLoadLatestSnapshot:
    @mock_aws
    def test_returns_none_when_no_snapshot(self) -> None:
        _create_bucket()
        result = _repo().load_latest_snapshot("salesforce", "salesforce-account")
        assert result is None

    @mock_aws
    def test_returns_snapshot_after_write(self) -> None:
        _create_bucket()
        snap = _make_snapshot()
        repo = _repo()
        repo.write_snapshot(snap)
        loaded = repo.load_latest_snapshot("salesforce", "salesforce-account")
        assert loaded is not None
        assert loaded.schema_version == snap.schema_version
        assert loaded.source_id == snap.source_id

    @mock_aws
    def test_returns_latest_after_two_writes(self) -> None:
        _create_bucket()
        snap_v1 = _make_snapshot(schema_version="v1fingerprint1", extraction_date="2026-06-10")
        snap_v2 = _make_snapshot(schema_version="v2fingerprint2", extraction_date="2026-06-11")
        repo = _repo()
        repo.write_snapshot(snap_v1)
        repo.write_snapshot(snap_v2)
        loaded = repo.load_latest_snapshot("salesforce", "salesforce-account")
        assert loaded is not None
        assert loaded.schema_version == "v2fingerprint2"


# ---------------------------------------------------------------------------
# load_snapshot_by_key
# ---------------------------------------------------------------------------


class TestLoadSnapshotByKey:
    @mock_aws
    def test_round_trip_via_key(self) -> None:
        _create_bucket()
        snap = _make_snapshot()
        repo = _repo()
        key = repo.write_snapshot(snap)
        loaded = repo.load_snapshot_by_key(key)
        assert loaded.source_id == snap.source_id
        assert loaded.entity_id == snap.entity_id
        assert loaded.schema_version == snap.schema_version
        assert loaded.record_count == snap.record_count
        assert len(loaded.fields) == len(snap.fields)


# ---------------------------------------------------------------------------
# Round-trip serialisation
# ---------------------------------------------------------------------------


class TestSerialisation:
    def test_serialise_then_deserialise_produces_equal_snapshot(self) -> None:
        snap = _make_snapshot()
        raw = _serialise_snapshot(snap)
        restored = _deserialise_snapshot(raw)
        assert restored.source_id == snap.source_id
        assert restored.schema_version == snap.schema_version
        assert len(restored.fields) == len(snap.fields)
        assert restored.fields[0].name == "Id"
        assert restored.fields[1].length == 255
        assert restored.fields[2].precision == 18
        assert restored.fields[2].scale == 2

    def test_field_values_not_in_serialised_output(self) -> None:
        """Confirm the schema is structural only — no data values."""
        snap = _make_snapshot()
        raw = _serialise_snapshot(snap)
        # Only structural attributes present; values never stored
        field_keys = set(raw["fields"][0].keys())
        assert field_keys == {
            "name",
            "data_type",
            "is_nullable",
            "is_queryable",
            "length",
            "precision",
            "scale",
            "is_custom",
        }


# ---------------------------------------------------------------------------
# Regression tests for fixed bugs
# ---------------------------------------------------------------------------


class TestWriteDriftReport:
    """
    Regression test for Bug #7: missing write_drift_report() method.

    The spec requires the drift report to be written to S3 alongside each
    snapshot.  SchemaSnapshotRepository must expose this method.
    """

    @mock_aws
    def test_write_drift_report_creates_s3_object_at_expected_key(self) -> None:
        _create_bucket()
        repo = _repo()
        key = repo.write_drift_report(
            source_id="salesforce",
            entity_id="salesforce-account",
            schema_version="abc123def456",
            extraction_date="2026-06-11",
            report_json='{"overall_classification":"no_drift","field_changes":[]}',
        )
        assert key == ("salesforce/salesforce-account/abc123def456/drift-report-2026-06-11.json")
        s3 = boto3.client("s3", region_name=_REGION)
        response = s3.get_object(Bucket=_BUCKET, Key=key)
        assert response["ContentType"] == "application/json"

    @mock_aws
    def test_drift_report_alongside_snapshot_uses_same_schema_version(self) -> None:
        _create_bucket()
        snap = _make_snapshot()
        repo = _repo()
        snapshot_key = repo.write_snapshot(snap)
        drift_key = repo.write_drift_report(
            source_id=snap.source_id,
            entity_id=snap.entity_id,
            schema_version=snap.schema_version,
            extraction_date=snap.extraction_date,
            report_json='{"overall_classification":"no_drift","field_changes":[]}',
        )
        # Snapshot and drift report share the same schema_version directory.
        snapshot_prefix = "/".join(snapshot_key.split("/")[:3])
        drift_prefix = "/".join(drift_key.split("/")[:3])
        assert snapshot_prefix == drift_prefix


class TestInputValidationOnWrite:
    """
    Regression test for Bug #5 (snapshot repo): source_id/entity_id must be
    validated before being used in S3 key construction.
    """

    @mock_aws
    def test_invalid_source_id_raises_before_s3_call(self) -> None:
        _create_bucket()
        snap = _make_snapshot()
        # Construct a snapshot with an invalid source_id bypassing Pydantic
        import dataclasses

        bad_snap = dataclasses.replace(snap, source_id="INVALID_SOURCE")
        with pytest.raises(ValueError, match="stable identifier"):
            _repo().write_snapshot(bad_snap)


class TestSnapshotPointerWriteFailureHandling:
    """
    Regression test for Bug #8: pointer update failure orphans the snapshot.

    If _write_latest_pointer fails, write_snapshot must still return the key
    and log a warning rather than propagating the error.  The snapshot is
    durably written and recoverable.
    """

    @mock_aws
    def test_snapshot_key_returned_even_when_pointer_write_fails(self, monkeypatch: object) -> None:
        _create_bucket()
        repo = _repo()

        # Patch _write_latest_pointer to simulate failure
        from botocore.exceptions import ClientError

        def _fail_pointer(*args: object, **kwargs: object) -> None:
            raise ClientError(
                {"Error": {"Code": "ServiceUnavailable", "Message": "Transient"}},
                "PutObject",
            )

        monkeypatch.setattr(repo, "_write_latest_pointer", _fail_pointer)

        snap = _make_snapshot()
        # Must not raise; key must still be returned
        key = repo.write_snapshot(snap)
        assert key == "salesforce/salesforce-account/abc123def456/2026-06-11.json"

        # Snapshot object must exist in S3 (was written before pointer failure)
        import boto3 as _boto3

        s3 = _boto3.client("s3", region_name=_REGION)
        response = s3.get_object(Bucket=_BUCKET, Key=key)
        assert response["ContentType"] == "application/json"
