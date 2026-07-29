"""Tests for the semantic query service (FR-2.3). Engine mocked."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from semantic.query_compiler import AccessDeniedError, SemanticQueryRequest
from semantic.semantic_model import (
    Dimension,
    JoinKind,
    Metric,
    SemanticEntity,
    SemanticJoin,
    SemanticModel,
)
from semantic.semantic_query_service import SemanticQueryService
from tenancy.scope_contract import TenantPartitionProfile
from tenancy.scope_predicate import build_scope_claims, scope_predicate


def _model() -> SemanticModel:
    entity = SemanticEntity(
        name="company",
        entity_type="company",
        dimensions=(
            Dimension(name="industry", column="industry"),
            Dimension(name="country", column="billing_country", access_tag="pii"),
        ),
        metrics=(Metric(name="total_revenue", aggregation="sum", column="annual_revenue"),),
        joins=(
            SemanticJoin(
                target_entity="franchisee",
                kind=JoinKind.LEFT,
                local_column="scope_unit_id",
                target_column="scope_unit_id",
            ),
        ),
    )
    franchisee = SemanticEntity(
        name="franchisee",
        entity_type="franchisee",
        dimensions=(Dimension(name="franchisee_name", column="franchisee_name"),),
    )
    return SemanticModel(tenant_code="demo", model_version="v1", entities=(entity, franchisee))


# The `demo` single-tenant predicate: applied, and matching everything because a single
# tenant owns every row it can read. Never `None`, which meant "apply nothing".
_SINGLE_TENANT_PREDICATE = scope_predicate(
    build_scope_claims("demo", TenantPartitionProfile(tenant_code="demo"))
)


def _service(engine, granted=frozenset()):
    return SemanticQueryService(
        model=_model(),
        engine=engine,
        entity_uri_resolver=lambda name: f"s3://edl-analytics-1/demo/analytics/{name}",
        granted_access_tags=granted,
        scope_predicate=_SINGLE_TENANT_PREDICATE,
    )


class TestSemanticQueryService:
    def test_compiles_and_runs(self):
        engine = MagicMock()
        engine.stream.return_value = [[{"industry": "Tech", "total_revenue": 100}]]
        result = _service(engine).run(
            SemanticQueryRequest(
                entity="company", metrics=("total_revenue",), dimensions=("industry",)
            )
        )
        assert result.rows == [{"industry": "Tech", "total_revenue": 100}]
        assert "SUM(entity_data.annual_revenue) AS total_revenue" in result.sql
        _, kwargs = engine.stream.call_args
        assert kwargs["inputs"] == {"entity_data": "s3://edl-analytics-1/demo/analytics/company"}

    def test_access_denied_propagates(self):
        engine = MagicMock()
        with pytest.raises(AccessDeniedError):
            _service(engine, granted=frozenset()).run(
                SemanticQueryRequest(
                    entity="company", metrics=("total_revenue",), dimensions=("country",)
                )
            )
        engine.stream.assert_not_called()


class TestJoinedQueriesRegisterEveryRelation:
    """
    The compiler emitted `LEFT JOIN franchisee AS j_0 ...` while the service registered only
    `entity_data`, so every joined query compiled cleanly and then failed at execution with "table
    does not exist". The join tests asserted the SQL string and never ran it, so DL-03's whole
    entity-relationship surface was broken with no red test.
    """

    def test_a_joined_entity_is_bound_as_an_input_relation(self):
        engine = MagicMock()
        engine.stream.return_value = [[{"franchisee_franchisee_name": "Acme", "total_revenue": 1}]]
        request = SemanticQueryRequest(
            entity="company",
            metrics=("total_revenue",),
            joined_dimensions=(("franchisee", "franchisee_name"),),
        )
        result = _service(engine).run(request)

        _, kwargs = engine.stream.call_args
        assert kwargs["inputs"] == {
            "entity_data": "s3://edl-analytics-1/demo/analytics/company",
            "franchisee": "s3://edl-analytics-1/demo/analytics/franchisee",
        }
        # Every relation the SQL names must be a registered input, or the engine fails at runtime.
        assert "JOIN franchisee AS j_0" in result.sql

    def test_an_unjoined_query_registers_only_the_base_entity(self):
        # Positive control: the fix must not register relations a query never references.
        engine = MagicMock()
        engine.stream.return_value = [[]]
        _service(engine).run(SemanticQueryRequest(entity="company", metrics=("total_revenue",)))
        _, kwargs = engine.stream.call_args
        assert kwargs["inputs"] == {"entity_data": "s3://edl-analytics-1/demo/analytics/company"}
