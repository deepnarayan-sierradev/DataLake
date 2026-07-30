"""Tests for entity_resolution.publishing_shared."""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from entity_resolution.matching_engine.match_rule_engine import (
    MatchDecision,
    MatchStrategy,
)
from entity_resolution.publishing_shared import (
    emit_golden_record_lineage,
    flatten_list_fields,
    serialise_decisions,
)

_REGION = "us-east-1"


class TestFlattenListFields:
    def test_list_fields_serialised_to_json_strings(self):
        records = [{"id": "g1", "sources": ["a", "b"], "name": "Alice"}]
        out = list(flatten_list_fields(records))
        assert out == [{"id": "g1", "sources": '["a", "b"]', "name": "Alice"}]

    def test_non_list_fields_passed_through(self):
        records = [{"id": "g1", "count": 3, "score": 1.5, "meta": {"k": "v"}}]
        out = list(flatten_list_fields(records))
        assert out == [{"id": "g1", "count": 3, "score": 1.5, "meta": {"k": "v"}}]

    def test_empty_input_yields_nothing(self):
        assert list(flatten_list_fields([])) == []

    def test_is_lazy_iterator(self):
        gen = flatten_list_fields(iter([{"x": [1]}]))
        assert next(gen) == {"x": "[1]"}


class TestSerialiseDecisions:
    def _decision(self, is_match=True):
        return MatchDecision(
            record_a_id="sf-001",
            record_b_id="ns-001",
            rule_id="email-match",
            strategy=MatchStrategy.DETERMINISTIC,
            is_match=is_match,
            confidence_score=1.0,
            matched_fields=("email",),
            rule_set_version="1.0.0",
        )

    def test_serialises_all_fields(self):
        payload = json.loads(serialise_decisions([self._decision()]))
        assert len(payload) == 1
        d = payload[0]
        assert d["record_a_id"] == "sf-001"
        assert d["record_b_id"] == "ns-001"
        assert d["rule_id"] == "email-match"
        assert d["strategy"] == "deterministic"
        assert d["is_match"] is True
        assert d["confidence_score"] == 1.0
        assert d["matched_fields"] == ["email"]
        assert d["rule_set_version"] == "1.0.0"

    def test_empty_decisions_is_empty_json_list(self):
        assert json.loads(serialise_decisions([])) == []


@mock_aws
class TestEmitGoldenRecordLineage:
    def test_lineage_record_written_to_governance_bucket(self):
        s3 = boto3.client("s3", region_name=_REGION)
        s3.create_bucket(Bucket="gov-bucket")

        emit_golden_record_lineage(
            s3_governance_bucket="gov-bucket",
            curated_s3_bucket="curated-bucket",
            curated_s3_prefixes=("curated/customer/",),
            analytics_s3_bucket="analytics-bucket",
            analytics_s3_prefix="canonical/customer/",
            match_run_id="run-1",
            entity_type="customer",
            golden_record_count=5,
            rule_set_version="1.0.0",
            survivorship_version="1.0.0",
            region_name=_REGION,
        )

        resp = s3.list_objects_v2(Bucket="gov-bucket", Prefix="demo/lineage/")
        assert resp.get("KeyCount", 0) >= 1

    def test_emission_failure_is_swallowed(self):
        emit_golden_record_lineage(
            s3_governance_bucket="nonexistent-bucket",
            curated_s3_bucket="curated-bucket",
            curated_s3_prefixes=(),
            analytics_s3_bucket="analytics-bucket",
            analytics_s3_prefix="canonical/customer/",
            match_run_id="run-2",
            entity_type="customer",
            golden_record_count=0,
            rule_set_version="1.0.0",
            survivorship_version="1.0.0",
            region_name=_REGION,
        )  # no exception expected


def test_flatten_then_serialise_round_trip():
    """Sanity: flattened list column round-trips through JSON."""
    rec = {"contributing_source_records": ["sf-001", "ns-001"]}
    flat = next(iter(flatten_list_fields([rec])))
    assert json.loads(flat["contributing_source_records"]) == ["sf-001", "ns-001"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
