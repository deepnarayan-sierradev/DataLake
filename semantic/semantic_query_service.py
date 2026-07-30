"""
Semantic query service (FR-2.3).

Compiles a structured SemanticQueryRequest against a tenant's semantic model and
executes it tenant-scoped on the processing engine — the single governed path
that dashboards, saved queries, and (later) the agent share. Callers pass a
structured request, never SQL; access tags are enforced by the compiler.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from processing_engine.interfaces.set_based_engine_interface import SetBasedQueryEngine
from semantic.query_compiler import QueryCompiler, SemanticQueryRequest
from semantic.result_cache import ResultCacheKey, SemanticResultCache
from semantic.semantic_model import SemanticModel
from tenancy.aggregate_protection import suppress_small_cells
from tenancy.scope_predicate import ScopePredicate

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
        scope_predicate: ScopePredicate,
        result_cache: SemanticResultCache | None = None,
    ) -> None:
        self._compiler = QueryCompiler(model)
        self._model = model
        self._engine = engine
        self._resolve_uri = entity_uri_resolver
        self._granted = granted_access_tags
        self._scope_predicate = scope_predicate
        self._cache = result_cache

    def run(
        self, request: SemanticQueryRequest, *, minimum_cohort_size: int | None = None
    ) -> SemanticQueryResult:
        started_ms = time.monotonic() * 1000
        compiled = self._compiler.compile(
            request,
            granted_access_tags=self._granted,
            scope_predicate=self._scope_predicate,
        )
        entity_uri = self._resolve_uri(request.entity)

        cache_key = (
            ResultCacheKey.build(
                tenant_code=self._model.tenant_code,
                model_version=self._model.model_version,
                sql=compiled.sql,
                parameters=compiled.parameters,
                granted_access_tags=self._granted,
                predicate=self._scope_predicate,
            )
            if self._cache is not None
            else None
        )
        cached = (
            self._cache.get(cache_key, partition_marker=entity_uri)
            if self._cache is not None and cache_key is not None
            else None
        )
        if cached is not None:
            record_platform_metric(
                PlatformMetric.SEMANTIC_QUERY_LATENCY_MS,
                time.monotonic() * 1000 - started_ms,
                EntityType=request.entity,
            )
            return SemanticQueryResult(sql=compiled.sql, rows=cached)

        rows = [
            row
            for batch in self._engine.stream(
                sql=compiled.sql,
                inputs=self._inputs_for(request, entity_uri),
                params=compiled.parameters,
            )
            for row in batch
        ]
        if self._cache is not None and cache_key is not None:
            self._cache.put(cache_key, rows, partition_marker=entity_uri)
        if minimum_cohort_size is not None:
            rows = _suppress_small_cohorts(rows, minimum_cohort_size)
        record_platform_metric(
            PlatformMetric.SEMANTIC_QUERY_LATENCY_MS,
            time.monotonic() * 1000 - started_ms,
            EntityType=request.entity,
        )
        return SemanticQueryResult(sql=compiled.sql, rows=rows)

    def _inputs_for(self, request: SemanticQueryRequest, entity_uri: str) -> dict[str, str]:
        """
        Register a relation for the base entity **and** for every joined entity.

        Only `entity_data` was registered until 2026-07-29, while the compiler happily emitted
        `LEFT JOIN <entity> AS j_0 ...` for any declared join — so a joined query compiled cleanly
        and then failed at execution with "table does not exist". The join tests asserted the SQL
        string and never ran it, which is why the whole of DL-03's entity-relationship surface was
        broken without a red test.

        The compiler names a joined relation by the entity's own name, so that is the view name
        bound here; `validate_inputs` allowlists it, and the URI comes from the same resolver the
        base entity uses.
        """
        inputs = {_ENTITY_VIEW: entity_uri}
        for target_entity_name, _dimension in request.joined_dimensions:
            if target_entity_name not in inputs:
                inputs[target_entity_name] = self._resolve_uri(target_entity_name)
        return inputs


def _suppress_small_cohorts(
    rows: list[dict[str, Any]], minimum_cohort_size: int
) -> list[dict[str, Any]]:
    """Blank integer measures below the cohort threshold, preserving the row shape."""
    suppressed: list[dict[str, Any]] = []
    for row in rows:
        counts = {
            key: int(value)
            for key, value in row.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
        if not counts:
            suppressed.append(row)
            continue
        protected = suppress_small_cells(counts, minimum_cohort_size)
        suppressed.append({**row, **protected})
    return suppressed
