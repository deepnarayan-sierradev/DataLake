"""Tests for relationship rule models (FR-1.2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from knowledge.relationship_rules import RelationshipRule, RelationshipRuleSet


def _rule(**overrides):
    base = {
        "relationship_type": "contract_of_company",
        "from_entity_type": "contract",
        "to_entity_type": "company",
        "from_field": "company_id",
        "to_field": "account_id",
    }
    base.update(overrides)
    return RelationshipRule(**base)


class TestRelationshipRule:
    def test_valid_rule(self):
        rule = _rule()
        assert rule.relationship_type == "contract_of_company"
        assert rule.from_field == "company_id"

    def test_invalid_entity_type_rejected(self):
        with pytest.raises(ValidationError):
            _rule(from_entity_type="Bad Type")

    def test_invalid_column_rejected(self):
        with pytest.raises(ValidationError):
            _rule(from_field="company id; DROP TABLE x")

    def test_uppercase_column_rejected(self):
        with pytest.raises(ValidationError):
            _rule(to_field="AccountId")

    def test_extra_field_forbidden(self):
        with pytest.raises(ValidationError):
            RelationshipRule(
                relationship_type="x_of_y",
                from_entity_type="x",
                to_entity_type="y",
                from_field="a",
                to_field="b",
                unexpected="nope",
            )

    def test_frozen(self):
        rule = _rule()
        with pytest.raises(ValidationError):
            rule.from_field = "other"


class TestRelationshipRuleSet:
    def test_valid_rule_set(self):
        rule_set = RelationshipRuleSet(tenant_code="demo", rule_set_version="v1", rules=(_rule(),))
        assert len(rule_set.rules) == 1
        assert rule_set.rules[0].relationship_type == "contract_of_company"
