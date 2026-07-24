"""
Twin build orchestration (FR-1.1 / FR-1.3).

Composes the relationship resolver, processing engine, twin builder and twin
repository into the end-to-end build for one primary entity type: resolve each
configured relationship's edges (set-based, materialised to the relationships
S3 layer), read them back with the golden records, assemble connected twins and
upsert the twin index. Dependency-injected so it is testable without AWS; a
thin Lambda handler / Step Functions stage wraps this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from knowledge.relationship_resolver import RelationshipResolver
from knowledge.relationship_rules import RelationshipRule
from knowledge.twin_builder import TwinBuilder
from knowledge.twin_repository import TwinRepository
from observability.structured_logger import get_platform_logger
from processing_engine.interfaces.set_based_engine_interface import SetBasedQueryEngine

_logger = get_platform_logger(__name__)


@dataclass(frozen=True)
class RelationshipInput:
    rule: RelationshipRule
    to_uri: str
    edges_bucket: str
    edges_prefix: str


@dataclass(frozen=True)
class TwinBuildSummary:
    entity_type: str
    twin_count: int
    edge_count: int


class TwinPipeline:
    def __init__(
        self,
        *,
        engine: SetBasedQueryEngine,
        resolver: RelationshipResolver,
        repository: TwinRepository,
        builder: TwinBuilder | None = None,
    ) -> None:
        self._engine = engine
        self._resolver = resolver
        self._repository = repository
        self._builder = builder or TwinBuilder()

    def build_twins(
        self,
        *,
        tenant_code: str,
        entity_type: str,
        golden_uri: str,
        relationships: list[RelationshipInput],
        lifecycle_field: str | None = None,
    ) -> TwinBuildSummary:
        all_edges: list[dict[str, Any]] = []
        total_edges = 0
        for relationship in relationships:
            result = self._resolver.resolve(
                rule=relationship.rule,
                from_uri=golden_uri,
                to_uri=relationship.to_uri,
                output_bucket=relationship.edges_bucket,
                output_prefix=relationship.edges_prefix,
            )
            total_edges += result.output.row_count
            edge_uri = f"s3://{relationship.edges_bucket}/{relationship.edges_prefix}"
            for batch in self._engine.stream(
                sql="SELECT from_golden_id, to_golden_id FROM edges", inputs={"edges": edge_uri}
            ):
                for row in batch:
                    all_edges.append(
                        {
                            "from_golden_id": row["from_golden_id"],
                            "to_golden_id": row["to_golden_id"],
                            "relationship_type": relationship.rule.relationship_type,
                            "to_entity_type": relationship.rule.to_entity_type,
                        }
                    )

        golden_records: list[dict[str, Any]] = []
        for batch in self._engine.stream(sql="SELECT * FROM golden", inputs={"golden": golden_uri}):
            golden_records.extend(batch)

        twins = self._builder.build(
            entity_type=entity_type,
            golden_records=golden_records,
            edges=all_edges,
            lifecycle_field=lifecycle_field,
        )
        for twin in twins:
            self._repository.upsert_twin(tenant_code, twin)

        _logger.info(
            "twin_build_complete",
            tenant_code=tenant_code,
            entity_type=entity_type,
            twin_count=len(twins),
            edge_count=total_edges,
        )
        return TwinBuildSummary(
            entity_type=entity_type, twin_count=len(twins), edge_count=total_edges
        )
