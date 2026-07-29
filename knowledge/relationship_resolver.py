"""
Relationship resolver (FR-1.1).

Resolves edges between two analytics-layer golden-record datasets with a
deterministic key join, executed set-based on the processing engine — the full
candidate set never lands in Python memory. Output edges are
(from_golden_id, to_golden_id) pairs; the relationship_type is carried on the
result and in the output prefix rather than embedded in the query.
"""

from __future__ import annotations

from dataclasses import dataclass

from knowledge.relationship_rules import RelationshipRule
from observability.structured_logger import get_platform_logger
from processing_engine.interfaces.set_based_engine_interface import (
    QueryOutput,
    SetBasedQueryEngine,
)

_logger = get_platform_logger(__name__)


@dataclass(frozen=True)
class RelationshipResolutionResult:
    relationship_type: str
    output: QueryOutput


class RelationshipResolver:
    def __init__(self, engine: SetBasedQueryEngine) -> None:
        self._engine = engine

    def build_edge_query(self, rule: RelationshipRule) -> str:
        # from_field/to_field allowlisted by RelationshipRule (OWASP A03).
        on_clause = f"f.{rule.from_field} = t.{rule.to_field}"
        # The target's scope unit travels with the edge so twin fan-out can be filtered without a
        # second lookup; without it an edge leaks the existence of another unit's entity.
        query = (
            "SELECT "  # noqa: S608  # nosec B608
            "f.golden_id AS from_golden_id, t.golden_id AS to_golden_id, "
            "t.scope_unit_id AS to_scope_unit_id "
            f"FROM from_rel f JOIN to_rel t ON {on_clause} "
            "WHERE f.golden_id IS NOT NULL AND t.golden_id IS NOT NULL"
        )
        return query

    def resolve(
        self,
        *,
        rule: RelationshipRule,
        from_uri: str,
        to_uri: str,
        output_bucket: str,
        output_prefix: str,
    ) -> RelationshipResolutionResult:
        output = self._engine.materialize(
            sql=self.build_edge_query(rule),
            inputs={"from_rel": from_uri, "to_rel": to_uri},
            output_bucket=output_bucket,
            output_prefix=output_prefix,
        )
        _logger.info(
            "relationship_resolution_complete",
            relationship_type=rule.relationship_type,
            from_entity_type=rule.from_entity_type,
            to_entity_type=rule.to_entity_type,
            edge_count=output.row_count,
        )
        return RelationshipResolutionResult(relationship_type=rule.relationship_type, output=output)
