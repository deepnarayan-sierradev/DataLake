"""Tests for the twin build orchestration (FR-1.1 / FR-1.3). Engine/resolver/repo mocked."""

from __future__ import annotations

from unittest.mock import MagicMock

from knowledge.relationship_resolver import RelationshipResolutionResult
from knowledge.relationship_rules import RelationshipRule
from knowledge.twin_pipeline import RelationshipInput, TwinPipeline
from processing_engine.interfaces.set_based_engine_interface import QueryOutput

_RULE = RelationshipRule(
    relationship_type="contract_of_company",
    from_entity_type="company",
    to_entity_type="contract",
    from_field="account_id",
    to_field="company_id",
)


def _rel_input() -> RelationshipInput:
    return RelationshipInput(
        rule=_RULE,
        to_uri="s3://edl-analytics-1/demo/canonical/contract",
        edges_bucket="edl-analytics-1",
        edges_prefix="demo/relationships/contract_of_company",
    )


class TestTwinPipeline:
    def test_builds_and_upserts_twins_with_edges(self):
        engine = MagicMock()
        # first stream() call → edges; second → golden records
        engine.stream.side_effect = [
            [[{"from_golden_id": "c1", "to_golden_id": "k1"}]],
            [[{"golden_id": "c1", "full_name": "Acme", "stage": "ramp"}]],
        ]
        resolver = MagicMock()
        resolver.resolve.return_value = RelationshipResolutionResult(
            relationship_type="contract_of_company",
            output=QueryOutput(output_uri="s3://x/y/data.parquet", row_count=1),
        )
        repository = MagicMock()

        pipeline = TwinPipeline(engine=engine, resolver=resolver, repository=repository)
        summary = pipeline.build_twins(
            tenant_code="demo",
            entity_type="company",
            golden_uri="s3://edl-analytics-1/demo/canonical/company",
            relationships=[_rel_input()],
            lifecycle_field="stage",
        )

        assert summary.twin_count == 1
        assert summary.edge_count == 1
        # one twin upserted, with the enriched edge + lifecycle
        repository.upsert_twin.assert_called_once()
        tenant_arg, twin_arg = repository.upsert_twin.call_args[0]
        assert tenant_arg == "demo"
        assert twin_arg.golden_id == "c1"
        assert twin_arg.lifecycle_stage == "ramp"
        assert len(twin_arg.edges) == 1
        assert twin_arg.edges[0].relationship_type == "contract_of_company"
        assert twin_arg.edges[0].to_entity_type == "contract"

    def test_no_relationships_still_builds_bare_twins(self):
        engine = MagicMock()
        engine.stream.side_effect = [[[{"golden_id": "c1"}]]]  # only golden stream
        pipeline = TwinPipeline(engine=engine, resolver=MagicMock(), repository=MagicMock())
        summary = pipeline.build_twins(
            tenant_code="demo",
            entity_type="company",
            golden_uri="s3://edl-analytics-1/demo/canonical/company",
            relationships=[],
        )
        assert summary.twin_count == 1
        assert summary.edge_count == 0

    def test_resolver_invoked_per_relationship(self):
        engine = MagicMock()
        engine.stream.side_effect = [
            [[{"from_golden_id": "c1", "to_golden_id": "k1"}]],
            [[{"golden_id": "c1"}]],
        ]
        resolver = MagicMock()
        resolver.resolve.return_value = RelationshipResolutionResult(
            relationship_type="contract_of_company",
            output=QueryOutput(output_uri="s3://x/y/data.parquet", row_count=1),
        )
        TwinPipeline(engine=engine, resolver=resolver, repository=MagicMock()).build_twins(
            tenant_code="demo",
            entity_type="company",
            golden_uri="s3://edl-analytics-1/demo/canonical/company",
            relationships=[_rel_input()],
        )
        resolver.resolve.assert_called_once()
