"""Tests for GoldenRecordPublisher — Phase 7."""

from __future__ import annotations

import io
import json

import boto3
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

from entity_resolution.canonical_record_publisher.canonical_record_publisher import (
    GoldenRecordPublicationError,
    GoldenRecordPublisher,
    _to_parquet,
)
from entity_resolution.matching_engine.match_rule_engine import (
    DeterministicMatchField,
    DeterministicMatchRule,
    MatchRuleSet,
)
from entity_resolution.survivorship_policy import (
    SurvivorshipPolicy,
    SurvivorshipStrategy,
)

_REGION = "us-east-1"
_ANALYTICS_BUCKET = "test-analytics-bucket"
_MATCH_RUN_ID = "match-run-001"


def _make_rule_set():
    return MatchRuleSet(
        entity_type="customer",
        rule_set_version="1.0.0",
        rules=(
            DeterministicMatchRule(
                rule_id="email-match",
                fields=(DeterministicMatchField("email"),),
            ),
        ),
    )


def _make_survivorship_policy():
    return SurvivorshipPolicy(
        entity_type="customer",
        policy_version="1.0.0",
        attribute_rules=(),
        default_strategy=SurvivorshipStrategy.FIRST_NON_NULL,
    )


def _make_publisher():
    return GoldenRecordPublisher(
        rule_set=_make_rule_set(),
        survivorship_policy=_make_survivorship_policy(),
        analytics_s3_bucket=_ANALYTICS_BUCKET,
        region_name=_REGION,
    )


@mock_aws
class TestGoldenRecordPublisher:
    def setup_method(self, method=None):
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_ANALYTICS_BUCKET)

    def _sample_records(self):
        return [
            {
                "record_id": "sf-001",
                "source_id": "salesforce",
                "email": "alice@example.com",
                "name": "Alice",
            },
            {
                "record_id": "ns-001",
                "source_id": "netsuite",
                "email": "alice@example.com",
                "name": "Alicia",
            },
            {
                "record_id": "sf-002",
                "source_id": "salesforce",
                "email": "bob@example.com",
                "name": "Bob",
            },
        ]

    def test_publish_returns_correct_metadata(self):
        publisher = _make_publisher()
        result = publisher.publish(
            curated_records=self._sample_records(),
            entity_type="customer",
            match_run_id=_MATCH_RUN_ID,
            id_field="record_id",
            source_field="source_id",
        )
        assert result.input_curated_record_count == 3
        assert result.golden_record_count == 2  # alice cluster + bob cluster
        assert result.cluster_count == 2
        assert result.entity_type == "customer"

    def test_golden_records_written_to_s3_as_parquet(self):
        publisher = _make_publisher()
        result = publisher.publish(
            curated_records=self._sample_records(),
            entity_type="customer",
            match_run_id=_MATCH_RUN_ID,
            id_field="record_id",
            source_field="source_id",
        )
        s3 = boto3.client("s3", region_name=_REGION)
        obj = s3.get_object(
            Bucket=_ANALYTICS_BUCKET, Key=result.analytics_s3_prefix + "golden.parquet"
        )
        table = pq.read_table(io.BytesIO(obj["Body"].read()))
        assert table.num_rows == 2

    def test_golden_records_contain_required_fields(self):
        publisher = _make_publisher()
        result = publisher.publish(
            curated_records=self._sample_records(),
            entity_type="customer",
            match_run_id=_MATCH_RUN_ID,
            id_field="record_id",
            source_field="source_id",
        )
        s3 = boto3.client("s3", region_name=_REGION)
        obj = s3.get_object(
            Bucket=_ANALYTICS_BUCKET, Key=result.analytics_s3_prefix + "golden.parquet"
        )
        table = pq.read_table(io.BytesIO(obj["Body"].read()))
        schema_names = table.schema.names
        assert "golden_id" in schema_names
        assert "match_run_id" in schema_names
        assert "survivorship_version" in schema_names

    def test_match_decisions_written_to_s3(self):
        publisher = _make_publisher()
        result = publisher.publish(
            curated_records=self._sample_records(),
            entity_type="customer",
            match_run_id=_MATCH_RUN_ID,
            id_field="record_id",
            source_field="source_id",
        )
        s3 = boto3.client("s3", region_name=_REGION)
        obj = s3.get_object(Bucket=_ANALYTICS_BUCKET, Key=result.decisions_s3_key)
        decisions = json.loads(obj["Body"].read())
        assert isinstance(decisions, list)
        assert len(decisions) > 0
        assert "rule_id" in decisions[0]
        # Verify PII values (actual email addresses) not in audit trail
        for d in decisions:
            assert "alice@example.com" not in json.dumps(d)
            assert "alice_at_example" not in json.dumps(d)

    def test_golden_id_is_deterministic(self):
        publisher = _make_publisher()
        r1 = publisher.publish(
            curated_records=self._sample_records(),
            entity_type="customer",
            match_run_id="run-1",
            id_field="record_id",
            source_field="source_id",
        )
        r2 = publisher.publish(
            curated_records=self._sample_records(),
            entity_type="customer",
            match_run_id="run-2",
            id_field="record_id",
            source_field="source_id",
        )
        # golden_ids should be the same across runs (deterministic from cluster members)
        s3 = boto3.client("s3", region_name=_REGION)
        t1 = pq.read_table(
            io.BytesIO(
                s3.get_object(
                    Bucket=_ANALYTICS_BUCKET, Key=r1.analytics_s3_prefix + "golden.parquet"
                )["Body"].read()
            )
        )
        t2 = pq.read_table(
            io.BytesIO(
                s3.get_object(
                    Bucket=_ANALYTICS_BUCKET, Key=r2.analytics_s3_prefix + "golden.parquet"
                )["Body"].read()
            )
        )
        ids1 = set(t1.column("golden_id").to_pylist())
        ids2 = set(t2.column("golden_id").to_pylist())
        assert ids1 == ids2

    def test_empty_records_raises(self):
        publisher = _make_publisher()
        with pytest.raises(GoldenRecordPublicationError):
            publisher.publish([], "customer", _MATCH_RUN_ID, "record_id", "source_id")


# ---------------------------------------------------------------------------
# Uncovered branches — targeted gap-fill tests
# ---------------------------------------------------------------------------


class TestToParquet:
    """Cover _to_parquet(empty list) branch (line 240)."""

    def test_empty_records_returns_empty_bytes(self):
        assert _to_parquet([]) == b""


@mock_aws
class TestPublisherWithLineageEmission:
    """Cover the _emit_golden_record_lineage path (lines 316-336)."""

    def setup_method(self, method=None):
        s3 = boto3.client("s3", region_name=_REGION)
        s3.create_bucket(Bucket=_ANALYTICS_BUCKET)
        s3.create_bucket(Bucket="test-governance-bucket")
        s3.create_bucket(Bucket="test-curated-bucket")

    def test_publish_with_governance_bucket_emits_lineage(self):
        publisher = GoldenRecordPublisher(
            rule_set=_make_rule_set(),
            survivorship_policy=_make_survivorship_policy(),
            analytics_s3_bucket=_ANALYTICS_BUCKET,
            region_name=_REGION,
            governance_s3_bucket="test-governance-bucket",
            curated_s3_bucket="test-curated-bucket",
            curated_s3_prefixes=("curated/customer/",),
        )
        records = [
            {"record_id": "sf-001", "source_id": "salesforce", "email": "a@x.com", "name": "Alice"},
        ]
        result = publisher.publish(
            curated_records=records,
            entity_type="customer",
            match_run_id="lineage-run-001",
            id_field="record_id",
            source_field="source_id",
        )
        assert result.golden_record_count == 1

        # Verify lineage object written to governance bucket
        s3 = boto3.client("s3", region_name=_REGION)
        response = s3.list_objects_v2(Bucket="test-governance-bucket", Prefix="lineage/")
        assert response.get("KeyCount", 0) >= 1

    def test_publish_lineage_emission_failure_does_not_propagate(self):
        """Best-effort: lineage write failure must not abort the publish."""
        publisher = GoldenRecordPublisher(
            rule_set=_make_rule_set(),
            survivorship_policy=_make_survivorship_policy(),
            analytics_s3_bucket=_ANALYTICS_BUCKET,
            region_name=_REGION,
            governance_s3_bucket="nonexistent-bucket",  # S3 write will fail
            curated_s3_bucket="test-curated-bucket",
            curated_s3_prefixes=("curated/customer/",),
        )
        records = [
            {"record_id": "sf-001", "source_id": "salesforce", "email": "b@x.com", "name": "Bob"},
        ]
        # Should complete successfully despite lineage failure
        result = publisher.publish(
            curated_records=records,
            entity_type="customer",
            match_run_id="lineage-fail-run",
            id_field="record_id",
            source_field="source_id",
        )
        assert result.golden_record_count == 1
