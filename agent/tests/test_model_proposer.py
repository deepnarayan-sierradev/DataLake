"""Tests for the model-backed semantic request proposer (FR-3.1)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from agent.llm_client import LlmStructuredClient
from agent.model_proposer import ModelSemanticRequestProposer
from semantic.semantic_model import Dimension, Metric, SemanticEntity, SemanticModel


class _RecordingClient(LlmStructuredClient):
    def __init__(self, response):
        self.response = response
        self.system_prompt = ""
        self.user_prompt = ""
        self.response_schema: Mapping[str, Any] = {}

    def complete_structured(self, *, system_prompt, user_prompt, response_schema):
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        self.response_schema = response_schema
        return self.response


def _model():
    entity = SemanticEntity(
        name="company",
        entity_type="company",
        dimensions=(Dimension(name="industry", column="industry"),),
        metrics=(Metric(name="total_revenue", aggregation="sum", column="annual_revenue"),),
    )
    return SemanticModel(tenant_code="demo", model_version="v1", entities=(entity,))


class TestModelSemanticRequestProposer:
    def test_translates_response_to_request(self):
        client = _RecordingClient(
            {"entity": "company", "metrics": ["total_revenue"], "dimensions": ["industry"]}
        )
        request = ModelSemanticRequestProposer(client).propose(
            question="revenue by industry", model=_model(), prior_error=None
        )
        assert request.entity == "company"
        assert request.metrics == ("total_revenue",)
        assert request.dimensions == ("industry",)

    def test_prompt_lists_catalog_and_never_asks_for_sql(self):
        client = _RecordingClient({"entity": "company", "metrics": ["total_revenue"]})
        ModelSemanticRequestProposer(client).propose(
            question="how much revenue", model=_model(), prior_error=None
        )
        assert "company" in client.user_prompt
        assert "total_revenue" in client.user_prompt
        assert "industry" in client.user_prompt
        assert "Never write SQL" in client.system_prompt

    def test_prior_error_is_fed_back_for_self_correction(self):
        client = _RecordingClient({"entity": "company", "metrics": ["total_revenue"]})
        ModelSemanticRequestProposer(client).propose(
            question="q", model=_model(), prior_error="No metric 'revenue'"
        )
        assert "No metric 'revenue'" in client.user_prompt

    def test_missing_entity_raises(self):
        client = _RecordingClient({"metrics": ["total_revenue"]})
        with pytest.raises(ValueError):
            ModelSemanticRequestProposer(client).propose(
                question="q", model=_model(), prior_error=None
            )

    def test_missing_metrics_raises(self):
        client = _RecordingClient({"entity": "company", "metrics": []})
        with pytest.raises(ValueError):
            ModelSemanticRequestProposer(client).propose(
                question="q", model=_model(), prior_error=None
            )
