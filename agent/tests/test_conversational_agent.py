"""Tests for the conversational agent verification loop (FR-3.1 / FR-3.2 / FR-3.6)."""

from __future__ import annotations

from unittest.mock import MagicMock

from agent.conversational_agent import ConversationalAgent
from agent.proposer_interface import SemanticRequestProposer
from semantic.query_compiler import SemanticQueryRequest
from semantic.semantic_model import Dimension, Metric, SemanticEntity, SemanticModel
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
    )
    return SemanticModel(tenant_code="demo", model_version="v1", entities=(entity,))


class _ScriptedProposer(SemanticRequestProposer):
    """Returns a preset request per attempt; records the prior_error it was given."""

    def __init__(self, requests: list[SemanticQueryRequest]) -> None:
        self._requests = requests
        self.prior_errors: list[str | None] = []

    def propose(self, *, question, model, prior_error):
        self.prior_errors.append(prior_error)
        return self._requests[len(self.prior_errors) - 1]


_VALID = SemanticQueryRequest(entity="company", metrics=("total_revenue",))
_BAD_METRIC = SemanticQueryRequest(entity="company", metrics=("does_not_exist",))
_TAGGED = SemanticQueryRequest(
    entity="company", metrics=("total_revenue",), dimensions=("country",)
)


_SINGLE_TENANT_PREDICATE = scope_predicate(
    build_scope_claims("demo", TenantPartitionProfile(tenant_code="demo"))
)


def _agent(proposer, engine, granted=frozenset(), max_attempts=3):
    return ConversationalAgent(
        proposer=proposer,
        model=_model(),
        engine=engine,
        entity_uri_resolver=lambda name: f"s3://datalake-analytics-1/demo/analytics/{name}",
        scope_predicate=_SINGLE_TENANT_PREDICATE,
        granted_access_tags=granted,
        max_attempts=max_attempts,
    )


class TestAgent:
    def test_happy_path(self):
        engine = MagicMock()
        engine.stream.return_value = [[{"total_revenue": 100}]]
        result = _agent(_ScriptedProposer([_VALID]), engine).ask("total revenue?")
        assert result.answered is True
        assert result.attempts == 1
        assert result.rows == [{"total_revenue": 100}]
        assert result.checks.schema_valid and result.checks.executed and result.checks.grounded
        assert result.citations == ("company",)

    def test_self_correction_after_hallucinated_metric(self):
        engine = MagicMock()
        engine.stream.return_value = [[{"total_revenue": 100}]]
        proposer = _ScriptedProposer([_BAD_METRIC, _VALID])
        result = _agent(proposer, engine).ask("revenue?")
        assert result.answered is True
        assert result.attempts == 2
        assert proposer.prior_errors[0] is None
        assert proposer.prior_errors[1] is not None

    def test_persistent_schema_failure_returns_cannot_answer(self):
        engine = MagicMock()
        proposer = _ScriptedProposer([_BAD_METRIC, _BAD_METRIC, _BAD_METRIC])
        result = _agent(proposer, engine).ask("nonsense?")
        assert result.answered is False
        assert result.attempts == 3
        assert "Could not produce a verified query" in result.reason
        engine.stream.assert_not_called()

    def test_access_denied_is_terminal_no_retry(self):
        engine = MagicMock()
        proposer = _ScriptedProposer([_TAGGED, _VALID])
        result = _agent(proposer, engine, granted=frozenset()).ask("by country?")
        assert result.answered is False
        assert result.attempts == 1
        assert "pii" in result.reason
        engine.stream.assert_not_called()

    def test_access_allowed_with_grant(self):
        engine = MagicMock()
        engine.stream.return_value = [[{"country": "US", "total_revenue": 50}]]
        result = _agent(_ScriptedProposer([_TAGGED]), engine, granted=frozenset({"pii"})).ask("q")
        assert result.answered is True

    def test_execution_failure_then_success(self):
        engine = MagicMock()
        engine.stream.side_effect = [RuntimeError("boom"), [[{"total_revenue": 100}]]]
        result = _agent(_ScriptedProposer([_VALID, _VALID]), engine).ask("revenue?")
        assert result.answered is True
        assert result.attempts == 2
