"""Tests for the semantic query compiler (FR-2.3 / FR-2.4 / FR-2.5)."""

from __future__ import annotations

import pytest

from semantic.query_compiler import (
    AccessDeniedError,
    QueryCompiler,
    SemanticFilter,
    SemanticQueryError,
    SemanticQueryRequest,
)
from semantic.semantic_model import Dimension, Metric, SemanticEntity, SemanticModel
from tenancy.scope_predicate import UnrestrictedScopeReason, unrestricted_predicate


def _model() -> SemanticModel:
    entity = SemanticEntity(
        name="company",
        entity_type="company",
        dimensions=(
            Dimension(name="industry", column="industry"),
            Dimension(name="country", column="billing_country", access_tag="pii"),
        ),
        metrics=(
            Metric(name="total_revenue", aggregation="sum", column="annual_revenue"),
            Metric(name="company_count", aggregation="count", column="*"),
        ),
    )
    return SemanticModel(tenant_code="demo", model_version="v1", entities=(entity,))


_UNSCOPED = unrestricted_predicate(UnrestrictedScopeReason.DEFINITION_VALIDATION)

_COMPILER = QueryCompiler(_model())
_ALL_TAGS = frozenset({"pii"})


class TestCompile:
    def test_metrics_and_dimensions(self):
        req = SemanticQueryRequest(
            entity="company", metrics=("total_revenue", "company_count"), dimensions=("industry",)
        )
        compiled = _COMPILER.compile(
            req, granted_access_tags=frozenset(), scope_predicate=_UNSCOPED
        )
        assert compiled.sql == (
            "SELECT entity_data.industry AS industry, "
            "SUM(entity_data.annual_revenue) AS total_revenue, "
            "COUNT(*) AS company_count FROM entity_data "
            "WHERE (scope_unit_id IS NOT NULL OR scope_unit_id IS NULL) "
            "GROUP BY entity_data.industry LIMIT 10000"
        )
        assert compiled.parameters == []

    def test_filters_are_parameterized(self):
        req = SemanticQueryRequest(
            entity="company",
            metrics=("total_revenue",),
            filters=(SemanticFilter(dimension="industry", operator="eq", value="Tech"),),
        )
        compiled = _COMPILER.compile(
            req, granted_access_tags=frozenset(), scope_predicate=_UNSCOPED
        )
        assert "entity_data.industry = ?" in compiled.sql
        assert compiled.sql.index("scope_unit_id") < compiled.sql.index("entity_data.industry = ?")
        assert compiled.parameters == ["Tech"]

    def test_no_metrics_rejected(self):
        with pytest.raises(SemanticQueryError):
            _COMPILER.compile(
                SemanticQueryRequest(entity="company", metrics=()),
                granted_access_tags=frozenset(),
                scope_predicate=_UNSCOPED,
            )

    def test_unknown_metric_rejected(self):
        with pytest.raises(SemanticQueryError):
            _COMPILER.compile(
                SemanticQueryRequest(entity="company", metrics=("nope",)),
                granted_access_tags=frozenset(),
                scope_predicate=_UNSCOPED,
            )

    def test_unknown_entity_rejected(self):
        with pytest.raises(SemanticQueryError):
            _COMPILER.compile(
                SemanticQueryRequest(entity="ghost", metrics=("total_revenue",)),
                granted_access_tags=frozenset(),
                scope_predicate=_UNSCOPED,
            )

    def test_access_tag_denied_without_grant(self):
        req = SemanticQueryRequest(
            entity="company", metrics=("total_revenue",), dimensions=("country",)
        )
        with pytest.raises(AccessDeniedError):
            _COMPILER.compile(req, granted_access_tags=frozenset(), scope_predicate=_UNSCOPED)

    def test_access_tag_allowed_with_grant(self):
        req = SemanticQueryRequest(
            entity="company", metrics=("total_revenue",), dimensions=("country",)
        )
        compiled = _COMPILER.compile(req, granted_access_tags=_ALL_TAGS, scope_predicate=_UNSCOPED)
        assert "billing_country AS country" in compiled.sql

    def test_access_tag_enforced_on_filter(self):
        req = SemanticQueryRequest(
            entity="company",
            metrics=("total_revenue",),
            filters=(SemanticFilter(dimension="country", operator="eq", value="US"),),
        )
        with pytest.raises(AccessDeniedError):
            _COMPILER.compile(req, granted_access_tags=frozenset(), scope_predicate=_UNSCOPED)
