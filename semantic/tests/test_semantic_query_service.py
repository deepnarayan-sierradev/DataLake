"""Tests for the semantic query service (FR-2.3). Engine mocked."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from semantic.query_compiler import AccessDeniedError, SemanticQueryRequest
from semantic.semantic_model import Dimension, Metric, SemanticEntity, SemanticModel
from semantic.semantic_query_service import SemanticQueryService


def _model() -> SemanticModel:
    entity = SemanticEntity(
        name="company",
        entity_type="company",
        dimensions=(
            Dimension(name="industry", column="industry"),
            Dimension(name="country", column="billing_country", access_tag="pii"),
        ),
        metrics=(Metric(name="total_revenue", aggregation="sum", column="annual_revenue"),),
    )
    return SemanticModel(tenant_code="demo", model_version="v1", entities=(entity,))


def _service(engine, granted=frozenset()):
    return SemanticQueryService(
        model=_model(),
        engine=engine,
        entity_uri_resolver=lambda name: f"s3://edl-analytics-1/demo/analytics/{name}",
        granted_access_tags=granted,
        scope_predicate=None,
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
