"""
Twin builder (FR-1.3 / FR-1.5).

Assembles connected twins from a golden-record dataset plus the edges resolved
by RelationshipResolver. Pure, set-in/set-out logic (no I/O): the orchestration
stage reads golden records and edges via the processing engine and hands them
here. Rollups are per-relationship edge counts (degree); value rollups that
join related entities' attributes are deferred (FR-1.5, follow-up).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from knowledge.twin import Twin, TwinEdge
from tenancy.scope_predicate import SCOPE_UNIT_COLUMN


class TwinBuilder:
    def build(
        self,
        *,
        entity_type: str,
        golden_records: Iterable[Mapping[str, Any]],
        edges: Iterable[Mapping[str, Any]],
        lifecycle_field: str | None = None,
    ) -> list[Twin]:
        edges_by_source: dict[str, list[TwinEdge]] = {}
        for edge in edges:
            source_id = str(edge["from_golden_id"])
            target_unit = edge.get("to_scope_unit_id")
            edges_by_source.setdefault(source_id, []).append(
                TwinEdge(
                    relationship_type=str(edge["relationship_type"]),
                    to_entity_type=str(edge["to_entity_type"]),
                    to_golden_id=str(edge["to_golden_id"]),
                    scope_unit_id=None if target_unit is None else str(target_unit),
                )
            )

        twins: list[Twin] = []
        for record in golden_records:
            attributes = dict(record)
            golden_id = str(attributes["golden_id"])
            node_edges = tuple(edges_by_source.get(golden_id, ()))
            rollups: dict[str, int] = {}
            for node_edge in node_edges:
                key = f"{node_edge.relationship_type}_count"
                rollups[key] = rollups.get(key, 0) + 1
            lifecycle_stage: str | None = None
            if lifecycle_field and attributes.get(lifecycle_field) is not None:
                lifecycle_stage = str(attributes[lifecycle_field])
            node_unit = attributes.get(SCOPE_UNIT_COLUMN)
            twins.append(
                Twin(
                    entity_type=entity_type,
                    golden_id=golden_id,
                    attributes=attributes,
                    edges=node_edges,
                    lifecycle_stage=lifecycle_stage,
                    rollups=rollups,
                    scope_unit_id=None if node_unit is None else str(node_unit),
                )
            )
        return twins
