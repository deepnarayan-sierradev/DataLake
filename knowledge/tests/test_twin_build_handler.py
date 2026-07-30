"""Tests for the twin-build Lambda handler (FR-1.1 / FR-1.3). Collaborators mocked."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import knowledge.twin_build_handler as twin_build_handler
from knowledge.relationship_rules import RelationshipRule, RelationshipRuleSet
from knowledge.relationship_rules_registry import RelationshipRulesNotFoundError
from knowledge.twin_pipeline import TwinBuildSummary


def _event(**overrides):
    event = {
        "source_id": "mysql",
        "entity_id": "contract",
        "environment": "dev",
        "run_id": "run-123",
        "tenant_code": "demo",
    }
    event.update(overrides)
    return event


def _rule_set():
    return RelationshipRuleSet(
        tenant_code="demo",
        rule_set_version="v1",
        rules=(
            RelationshipRule(
                relationship_type="signed_by",
                from_entity_type="contract",
                to_entity_type="company",
                from_field="company_id",
                to_field="golden_id",
            ),
            RelationshipRule(
                relationship_type="unrelated",
                from_entity_type="company",
                to_entity_type="contract",
                from_field="golden_id",
                to_field="company_id",
            ),
        ),
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("ANALYTICS_S3_BUCKET", "datalake-analytics-1")
    monkeypatch.setenv("RELATIONSHIP_RULES_S3_BUCKET", "datalake-curated-1")


def _patch_common(monkeypatch, *, entity_type="contract", rules=None, partitions=None):
    registry = MagicMock()
    registry.get_entity_type.return_value = entity_type
    monkeypatch.setattr(twin_build_handler, "EntityTypeRegistryClient", lambda **k: registry)

    rules_registry = MagicMock()
    if isinstance(rules, Exception):
        rules_registry.load.side_effect = rules
    else:
        rules_registry.load.return_value = rules
    monkeypatch.setattr(twin_build_handler, "RelationshipRulesRegistry", lambda **k: rules_registry)

    partitions = partitions or {}

    def _paginate(**kwargs):
        segments = kwargs["Prefix"].split("/")
        found = partitions.get(segments[2] if len(segments) > 2 else "", [])
        return [{"CommonPrefixes": [{"Prefix": p} for p in found]}]

    paginator = MagicMock()
    paginator.paginate.side_effect = _paginate
    s3 = MagicMock()
    s3.get_paginator.return_value = paginator
    monkeypatch.setattr(twin_build_handler.boto3, "client", lambda *a, **k: s3)

    monkeypatch.setattr(twin_build_handler, "TwinRepository", lambda **k: MagicMock())
    monkeypatch.setattr(
        twin_build_handler.set_based_engine_registry, "build", lambda *a, **k: MagicMock()
    )
    monkeypatch.setattr(twin_build_handler, "RelationshipResolver", lambda engine: MagicMock())


class TestTwinBuildHandler:
    def test_missing_field_raises(self):
        event = _event()
        del event["tenant_code"]
        with pytest.raises(ValueError):
            twin_build_handler.lambda_handler(event, None)

    def test_invalid_tenant_rejected(self):
        with pytest.raises(ValueError):
            twin_build_handler.lambda_handler(_event(tenant_code="BAD_TENANT"), None)

    def test_skips_when_no_rules(self, monkeypatch):
        _patch_common(monkeypatch, rules=RelationshipRulesNotFoundError("none"))
        result = twin_build_handler.lambda_handler(_event(), None)
        assert result == {
            "skipped": True,
            "entity_type": "contract",
            "twin_count": 0,
            "edge_count": 0,
        }

    def test_missing_golden_partition_raises(self, monkeypatch):
        _patch_common(monkeypatch, rules=_rule_set(), partitions={})
        with pytest.raises(ValueError):
            twin_build_handler.lambda_handler(_event(), None)

    def test_happy_path_builds_and_filters_relationships(self, monkeypatch):
        _patch_common(
            monkeypatch,
            rules=_rule_set(),
            partitions={
                "contract": [
                    "demo/analytics/contract/analytics_date=2026-07-20/",
                    "demo/analytics/contract/analytics_date=2026-07-22/",
                ],
                "company": ["demo/analytics/company/analytics_date=2026-07-21/"],
            },
        )
        captured = {}

        class _FakePipeline:
            def __init__(self, **kwargs):
                pass

            def build_twins(
                self, *, tenant_code, entity_type, golden_uri, relationships, lifecycle_field
            ):
                captured["golden_uri"] = golden_uri
                captured["relationships"] = relationships
                return TwinBuildSummary(entity_type=entity_type, twin_count=3, edge_count=5)

        monkeypatch.setattr(twin_build_handler, "TwinPipeline", _FakePipeline)

        result = twin_build_handler.lambda_handler(_event(), None)
        assert result == {
            "skipped": False,
            "entity_type": "contract",
            "twin_count": 3,
            "edge_count": 5,
        }
        assert captured["golden_uri"] == (
            "s3://datalake-analytics-1/demo/analytics/contract/analytics_date=2026-07-22"
        )
        assert len(captured["relationships"]) == 1
        assert captured["relationships"][0].to_uri == (
            "s3://datalake-analytics-1/demo/analytics/company/analytics_date=2026-07-21"
        )
