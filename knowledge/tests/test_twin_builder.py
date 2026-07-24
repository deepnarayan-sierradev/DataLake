"""Tests for the twin builder (FR-1.3 / FR-1.5)."""

from __future__ import annotations

from knowledge.twin_builder import TwinBuilder


def _edge(from_id, rel, to_type, to_id):
    return {
        "from_golden_id": from_id,
        "relationship_type": rel,
        "to_entity_type": to_type,
        "to_golden_id": to_id,
    }


class TestTwinBuilder:
    def test_attributes_and_edges_assembled(self):
        golden = [{"golden_id": "c1", "full_name": "Acme", "stage": "ramp"}]
        edges = [
            _edge("c1", "contract_of_company", "contract", "k1"),
            _edge("c1", "contract_of_company", "contract", "k2"),
            _edge("c1", "person_of_company", "person", "p1"),
        ]
        twins = TwinBuilder().build(
            entity_type="company", golden_records=golden, edges=edges, lifecycle_field="stage"
        )
        assert len(twins) == 1
        twin = twins[0]
        assert twin.entity_type == "company"
        assert twin.golden_id == "c1"
        assert twin.attributes["full_name"] == "Acme"
        assert len(twin.edges) == 3
        assert twin.lifecycle_stage == "ramp"

    def test_rollups_count_edges_per_relationship(self):
        golden = [{"golden_id": "c1"}]
        edges = [
            _edge("c1", "contract_of_company", "contract", "k1"),
            _edge("c1", "contract_of_company", "contract", "k2"),
            _edge("c1", "person_of_company", "person", "p1"),
        ]
        twin = TwinBuilder().build(entity_type="company", golden_records=golden, edges=edges)[0]
        assert twin.rollups == {"contract_of_company_count": 2, "person_of_company_count": 1}

    def test_node_without_edges_has_empty_edges_and_rollups(self):
        golden = [{"golden_id": "c9"}]
        twin = TwinBuilder().build(entity_type="company", golden_records=golden, edges=[])[0]
        assert twin.edges == ()
        assert twin.rollups == {}

    def test_lifecycle_absent_when_no_field(self):
        golden = [{"golden_id": "c1", "stage": "ramp"}]
        twin = TwinBuilder().build(entity_type="company", golden_records=golden, edges=[])[0]
        assert twin.lifecycle_stage is None

    def test_edges_only_attached_to_matching_source(self):
        golden = [{"golden_id": "c1"}, {"golden_id": "c2"}]
        edges = [_edge("c1", "contract_of_company", "contract", "k1")]
        twins = {
            t.golden_id: t
            for t in TwinBuilder().build(entity_type="company", golden_records=golden, edges=edges)
        }
        assert len(twins["c1"].edges) == 1
        assert twins["c2"].edges == ()
