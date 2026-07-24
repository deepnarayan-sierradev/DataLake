"""Tests for the semantic model (FR-2.1 / FR-2.4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from semantic.semantic_model import Dimension, Metric, SemanticEntity, SemanticModel


def _company_entity() -> SemanticEntity:
    return SemanticEntity(
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


class TestModelValidation:
    def test_valid_model(self):
        model = SemanticModel(tenant_code="demo", model_version="v1", entities=(_company_entity(),))
        assert model.entity("company").metric("total_revenue").sql_aggregation() == "SUM"

    def test_count_distinct_maps_to_count(self):
        metric = Metric(name="distinct_x", aggregation="count_distinct", column="x")
        assert metric.sql_aggregation() == "COUNT"

    def test_invalid_column_rejected(self):
        with pytest.raises(ValidationError):
            Dimension(name="bad", column="DROP TABLE; --")

    def test_invalid_aggregation_rejected(self):
        with pytest.raises(ValidationError):
            Metric(name="x", aggregation="median", column="y")  # type: ignore[arg-type]

    def test_duplicate_metric_names_rejected(self):
        with pytest.raises(ValidationError):
            SemanticEntity(
                name="company",
                entity_type="company",
                metrics=(
                    Metric(name="dup", aggregation="sum", column="a"),
                    Metric(name="dup", aggregation="min", column="b"),
                ),
            )

    def test_duplicate_entity_names_rejected(self):
        with pytest.raises(ValidationError):
            SemanticModel(
                tenant_code="demo",
                model_version="v1",
                entities=(_company_entity(), _company_entity()),
            )


class TestLookups:
    def test_missing_entity_raises_keyerror(self):
        model = SemanticModel(tenant_code="demo", model_version="v1", entities=(_company_entity(),))
        with pytest.raises(KeyError):
            model.entity("nope")

    def test_missing_metric_raises_keyerror(self):
        with pytest.raises(KeyError):
            _company_entity().metric("nope")
