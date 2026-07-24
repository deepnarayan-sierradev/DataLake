"""Tests for the relationship resolver (FR-1.1). The processing engine is mocked."""

from __future__ import annotations

from unittest.mock import MagicMock

from knowledge.relationship_resolver import RelationshipResolver
from knowledge.relationship_rules import RelationshipRule
from processing_engine.interfaces.set_based_engine_interface import QueryOutput

_RULE = RelationshipRule(
    relationship_type="contract_of_company",
    from_entity_type="contract",
    to_entity_type="company",
    from_field="company_id",
    to_field="account_id",
)


class TestBuildEdgeQuery:
    def test_query_uses_validated_join_columns(self):
        resolver = RelationshipResolver(MagicMock())
        sql = resolver.build_edge_query(_RULE)
        assert "f.company_id = t.account_id" in sql
        assert "f.golden_id AS from_golden_id" in sql
        assert "t.golden_id AS to_golden_id" in sql
        assert "WHERE f.golden_id IS NOT NULL AND t.golden_id IS NOT NULL" in sql


class TestResolve:
    def test_resolve_materializes_and_returns_result(self):
        engine = MagicMock()
        engine.materialize.return_value = QueryOutput(
            output_uri="s3://edl-analytics-1/demo/relationships/contract_of_company/data.parquet",
            row_count=42,
        )
        resolver = RelationshipResolver(engine)

        result = resolver.resolve(
            rule=_RULE,
            from_uri="s3://edl-analytics-1/demo/canonical/contract",
            to_uri="s3://edl-analytics-1/demo/canonical/company",
            output_bucket="edl-analytics-1",
            output_prefix="demo/relationships/contract_of_company",
        )

        assert result.relationship_type == "contract_of_company"
        assert result.output.row_count == 42
        _, kwargs = engine.materialize.call_args
        assert kwargs["inputs"] == {
            "from_rel": "s3://edl-analytics-1/demo/canonical/contract",
            "to_rel": "s3://edl-analytics-1/demo/canonical/company",
        }
        assert kwargs["output_bucket"] == "edl-analytics-1"
        assert "f.company_id = t.account_id" in kwargs["sql"]
