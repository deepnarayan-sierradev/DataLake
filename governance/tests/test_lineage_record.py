"""Tests for LineageEmitter and LineageRecord — Phase 9."""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from governance.lineage_record import (
    LineageEmitter,
    LineageNode,
    LineageRecord,
    LineageRecordNotFoundError,
    LineageStage,
    build_extraction_lineage,
    build_transformation_lineage,
)

_REGION = "us-east-1"
_BUCKET = "test-governance-bucket"
_RUN_ID = "run-lineage-test-001"
_SOURCE_ID = "salesforce"
_ENTITY_ID = "salesforce-account"


def _sample_record():
    return LineageRecord(
        run_id=_RUN_ID,
        source_id=_SOURCE_ID,
        entity_id=_ENTITY_ID,
        pipeline_stage=LineageStage.EXTRACTION,
        source_nodes=(
            LineageNode(name="salesforce", s3_path="external://source", data_layer="source"),
        ),
        target_node=LineageNode(
            name="salesforce-account-raw",
            s3_path="s3://raw/salesforce/salesforce-account/run-001/",
            data_layer="raw",
        ),
        record_count=500,
        captured_at="2024-01-15T10:00:00+00:00",
        additional_context={"schema_version": "v1.2"},
    )


@mock_aws
class TestLineageEmitter:
    def setup_method(self, method=None):
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_BUCKET)
        self.emitter = LineageEmitter(_BUCKET, _REGION)

    def test_emit_writes_to_s3(self):
        record = _sample_record()
        key = self.emitter.emit(record)
        assert key.startswith("lineage/")
        assert _ENTITY_ID in key
        assert _RUN_ID in key

    def test_emitted_record_is_valid_json(self):
        record = _sample_record()
        key = self.emitter.emit(record)
        s3 = boto3.client("s3", region_name=_REGION)
        obj = s3.get_object(Bucket=_BUCKET, Key=key)
        payload = json.loads(obj["Body"].read())
        assert payload["run_id"] == _RUN_ID
        assert payload["source_id"] == _SOURCE_ID
        assert payload["pipeline_stage"] == "extraction"

    def test_emitted_record_contains_no_pii_values(self):
        record = _sample_record()
        key = self.emitter.emit(record)
        s3 = boto3.client("s3", region_name=_REGION)
        obj = s3.get_object(Bucket=_BUCKET, Key=key)
        raw_json = obj["Body"].read().decode()
        # Confirm no data values (only structural metadata)
        assert "alice@" not in raw_json
        assert "password" not in raw_json.lower()

    def test_load_returns_matching_record(self):
        record = _sample_record()
        self.emitter.emit(record)
        loaded = self.emitter.load(_RUN_ID, _ENTITY_ID, LineageStage.EXTRACTION)
        assert loaded.run_id == _RUN_ID
        assert loaded.source_id == _SOURCE_ID
        assert loaded.record_count == 500
        assert loaded.additional_context["schema_version"] == "v1.2"

    def test_load_nonexistent_raises(self):
        with pytest.raises(LineageRecordNotFoundError):
            self.emitter.load("nonexistent-run", _ENTITY_ID, LineageStage.EXTRACTION)

    def test_all_stages_produce_distinct_keys(self):
        keys = set()
        for stage in LineageStage:
            rec = LineageRecord(
                run_id=_RUN_ID,
                source_id=_SOURCE_ID,
                entity_id=_ENTITY_ID,
                pipeline_stage=stage,
                source_nodes=(),
                target_node=LineageNode("t", "s3://x/", "raw"),
                record_count=0,
                captured_at="2024-01-01T00:00:00",
                additional_context={},
            )
            keys.add(self.emitter.emit(rec))
        assert len(keys) == len(list(LineageStage))


class TestLineageFactories:
    def test_build_extraction_lineage_has_correct_stage(self):
        record = build_extraction_lineage(
            run_id=_RUN_ID,
            source_id="salesforce",
            entity_id="salesforce-account",
            raw_s3_prefix="raw/salesforce/",
            raw_s3_bucket="raw-bucket",
            record_count=100,
            schema_version="v1.0",
        )
        assert record.pipeline_stage == LineageStage.EXTRACTION
        assert record.record_count == 100
        assert record.additional_context["schema_version"] == "v1.0"

    def test_build_transformation_lineage_has_correct_stage(self):
        record = build_transformation_lineage(
            run_id=_RUN_ID,
            source_id="salesforce",
            entity_id="salesforce-account",
            raw_s3_bucket="raw-bucket",
            raw_s3_prefix="raw/salesforce/",
            curated_s3_bucket="curated-bucket",
            curated_s3_prefix="curated/customer/",
            record_count=95,
            mapping_version="1.0.0",
        )
        assert record.pipeline_stage == LineageStage.TRANSFORMATION
        assert record.additional_context["mapping_version"] == "1.0.0"
        assert "raw-bucket" in record.source_nodes[0].s3_path
        assert "curated-bucket" in record.target_node.s3_path


# ---------------------------------------------------------------------------
# Error-path coverage: emit raises, load non-404 re-raises, compact JSON
# ---------------------------------------------------------------------------


@mock_aws
class TestLineageEmitterErrorPaths:
    def setup_method(self, method: object = None) -> None:
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_BUCKET)
        self.emitter = LineageEmitter(
            governance_s3_bucket=_BUCKET, region_name=_REGION
        )

    def test_emit_s3_failure_raises_lineage_emission_error(self) -> None:
        from unittest.mock import MagicMock

        from governance.lineage_record import LineageEmissionError

        self.emitter._s3.put_object = MagicMock(side_effect=OSError("s3 down"))  # type: ignore[attr-defined]
        record = _sample_record()
        with pytest.raises(LineageEmissionError, match="Failed to write lineage"):
            self.emitter.emit(record)

    def test_load_non_404_client_error_reraises(self) -> None:
        from unittest.mock import MagicMock

        from botocore.exceptions import ClientError

        self.emitter._s3.get_object = MagicMock(  # type: ignore[attr-defined]
            side_effect=ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "Forbidden"}},
                "GetObject",
            )
        )
        with pytest.raises(ClientError):
            self.emitter.load(_RUN_ID, _ENTITY_ID, LineageStage.EXTRACTION)

    def test_emitted_json_is_compact(self) -> None:
        record = _sample_record()
        key = self.emitter.emit(record)
        s3 = boto3.client("s3", region_name=_REGION)
        body = s3.get_object(Bucket=_BUCKET, Key=key)["Body"].read().decode()
        # Compact JSON has no spaces after separators
        assert " " not in body, f"Expected compact JSON but found spaces: {body[:100]}"
