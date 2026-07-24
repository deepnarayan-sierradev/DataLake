"""Tests for the relationship-rules registry (FR-1.2). S3 mocked with moto."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from knowledge.relationship_rules import RelationshipRule, RelationshipRuleSet
from knowledge.relationship_rules_registry import (
    RelationshipRulesNotFoundError,
    RelationshipRulesRegistry,
)

_REGION = "us-east-1"
_BUCKET = "edl-curated-test"


def _rule_set(version: str = "v1") -> RelationshipRuleSet:
    return RelationshipRuleSet(
        tenant_code="demo",
        rule_set_version=version,
        rules=(
            RelationshipRule(
                relationship_type="signed_by",
                from_entity_type="contract",
                to_entity_type="company",
                from_field="company_id",
                to_field="golden_id",
            ),
        ),
    )


class TestRelationshipRulesRegistry:
    @mock_aws
    def test_publish_and_load_latest(self):
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_BUCKET)
        registry = RelationshipRulesRegistry(_BUCKET, _REGION)
        registry.publish("contract", _rule_set("v1"))
        loaded = registry.load("demo", "contract")
        assert loaded.rule_set_version == "v1"
        assert loaded.rules[0].relationship_type == "signed_by"

    @mock_aws
    def test_load_explicit_version(self):
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_BUCKET)
        registry = RelationshipRulesRegistry(_BUCKET, _REGION)
        registry.publish("contract", _rule_set("v2"))
        assert registry.load("demo", "contract", "v2").rule_set_version == "v2"

    @mock_aws
    def test_missing_config_raises(self):
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_BUCKET)
        registry = RelationshipRulesRegistry(_BUCKET, _REGION)
        with pytest.raises(RelationshipRulesNotFoundError):
            registry.load("demo", "contract", "v9")

    def test_invalid_tenant_rejected(self):
        registry = RelationshipRulesRegistry(_BUCKET, _REGION)
        with pytest.raises(ValueError):
            registry.load("BAD_TENANT", "contract")

    def test_invalid_version_rejected(self):
        registry = RelationshipRulesRegistry(_BUCKET, _REGION)
        with pytest.raises(ValueError):
            registry.load("demo", "contract", "not-a-version")
