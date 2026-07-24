"""
Semantic query service (FR-2.3).

Compiles a structured SemanticQueryRequest against a tenant's semantic model and
executes it tenant-scoped on the processing engine — the single governed path
that dashboards, saved queries, and (later) the agent share. Callers pass a
structured request, never SQL; access tags are enforced by the compiler.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from processing_engine.interfaces.set_based_engine_interface import SetBasedQueryEngine
from semantic.query_compiler import QueryCompiler, SemanticQueryRequest
from semantic.semantic_model import SemanticModel

_ENTITY_VIEW = "entity_data"


@dataclass(frozen=True)
class SemanticQueryResult:
    sql: str
    rows: list[dict[str, Any]] = field(default_factory=list)


class SemanticQueryService:
    def __init__(
        self,
        *,
        model: SemanticModel,
        engine: SetBasedQueryEngine,
        entity_uri_resolver: Callable[[str], str],
        granted_access_tags: frozenset[str],
    ) -> None:
        self._compiler = QueryCompiler(model)
        self._engine = engine
        self._resolve_uri = entity_uri_resolver
        self._granted = granted_access_tags

    def run(self, request: SemanticQueryRequest) -> SemanticQueryResult:
        compiled = self._compiler.compile(request, granted_access_tags=self._granted)
        entity_uri = self._resolve_uri(request.entity)
        rows = [
            row
            for batch in self._engine.stream(
                sql=compiled.sql, inputs={_ENTITY_VIEW: entity_uri}, params=compiled.parameters
            )
            for row in batch
        ]
        return SemanticQueryResult(sql=compiled.sql, rows=rows)
