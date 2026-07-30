"""
Scope-partitioned entity resolution (DL-SCOPE-08).

This is the defect DL-12 was created for: resolution merged across every contributing source
within a tenant, so two franchisees' identical customer became one golden record carrying both
units' data in `field_provenance`. No downstream row filter repairs that — the merged record
*is* the leak — so the guarantee has to hold at candidate generation.

The tests below therefore assert on cluster output, not on a predicate.
"""

from __future__ import annotations

from typing import Any

from entity_resolution.matching_engine.match_rule_engine import (
    DeterministicMatchField,
    DeterministicMatchRule,
    MatchRuleEngine,
    MatchRuleSet,
)
from entity_resolution.matching_engine.record_blocker import (
    BlockingKeyType,
    BlockingStrategy,
    RecordBlocker,
)
from tenancy.scope_contract import ResolutionScope

_ID_FIELD = "record_id"


def _rule_set(*, with_blocking: bool) -> MatchRuleSet:
    return MatchRuleSet(
        entity_type="company",
        rule_set_version="v1",
        rules=(
            DeterministicMatchRule(
                rule_id="exact-company-name",
                fields=(DeterministicMatchField(field_name="company_name"),),
            ),
        ),
        blocking_strategy=(
            BlockingStrategy(key_type=BlockingKeyType.NAME_FIRST3, source_field="company_name")
            if with_blocking
            else None
        ),
    )


def _two_franchisees_same_customer() -> list[dict[str, Any]]:
    """The exact shape that used to merge: identical customer, two different units."""
    return [
        {
            _ID_FIELD: "a-1",
            "company_name": "Acme Industrial",
            "scope_unit_id": "franchisee-0001",
        },
        {
            _ID_FIELD: "b-1",
            "company_name": "Acme Industrial",
            "scope_unit_id": "franchisee-0002",
        },
    ]


class TestScopeUnitGrainedResolution:
    def test_two_units_identical_customer_stays_two_records(self) -> None:
        engine = MatchRuleEngine(
            _rule_set(with_blocking=True), resolution_scope=ResolutionScope.SCOPE_UNIT
        )
        clusters, _ = engine.cluster(_two_franchisees_same_customer(), _ID_FIELD)
        assert len(clusters) == 2, (
            "Two franchisees' identical customer merged into one golden record — the DL-12 "
            "defect. A row filter cannot repair this; the merged record is the disclosure."
        )

    def test_the_guarantee_holds_without_a_blocking_strategy(self) -> None:
        engine = MatchRuleEngine(
            _rule_set(with_blocking=False), resolution_scope=ResolutionScope.SCOPE_UNIT
        )
        clusters, _ = engine.cluster(_two_franchisees_same_customer(), _ID_FIELD)
        assert len(clusters) == 2

    def test_records_within_one_unit_still_merge(self) -> None:
        records = [
            {_ID_FIELD: "a-1", "company_name": "Acme", "scope_unit_id": "franchisee-0001"},
            {_ID_FIELD: "a-2", "company_name": "Acme", "scope_unit_id": "franchisee-0001"},
        ]
        engine = MatchRuleEngine(
            _rule_set(with_blocking=True), resolution_scope=ResolutionScope.SCOPE_UNIT
        )
        clusters, _ = engine.cluster(records, _ID_FIELD)
        assert len(clusters) == 1

    def test_unattributed_records_do_not_join_every_unit(self) -> None:
        records = [
            {_ID_FIELD: "a-1", "company_name": "Acme", "scope_unit_id": "franchisee-0001"},
            {_ID_FIELD: "x-1", "company_name": "Acme", "scope_unit_id": None},
        ]
        engine = MatchRuleEngine(
            _rule_set(with_blocking=True), resolution_scope=ResolutionScope.SCOPE_UNIT
        )
        clusters, _ = engine.cluster(records, _ID_FIELD)
        assert len(clusters) == 2


class TestTenantGrainedResolutionUnchanged:
    def test_a_tenant_scoped_entity_still_merges_across_units(self) -> None:
        engine = MatchRuleEngine(
            _rule_set(with_blocking=True), resolution_scope=ResolutionScope.TENANT
        )
        clusters, _ = engine.cluster(_two_franchisees_same_customer(), _ID_FIELD)
        assert len(clusters) == 1

    def test_the_default_is_tenant_grained_for_backward_compatibility(self) -> None:
        engine = MatchRuleEngine(_rule_set(with_blocking=True))
        clusters, _ = engine.cluster(_two_franchisees_same_customer(), _ID_FIELD)
        assert len(clusters) == 1


class TestBlockerKeyComposition:
    def test_the_scope_unit_participates_in_the_blocking_key(self) -> None:
        blocker = RecordBlocker(
            BlockingStrategy(key_type=BlockingKeyType.NAME_FIRST3, source_field="company_name"),
            resolution_scope=ResolutionScope.SCOPE_UNIT,
        )
        blocks = blocker.partition(_two_franchisees_same_customer())
        assert len(blocks) == 2

    def test_tenant_grained_blocking_ignores_the_scope_unit(self) -> None:
        blocker = RecordBlocker(
            BlockingStrategy(key_type=BlockingKeyType.NAME_FIRST3, source_field="company_name"),
            resolution_scope=ResolutionScope.TENANT,
        )
        blocks = blocker.partition(_two_franchisees_same_customer())
        assert len(blocks) == 1
