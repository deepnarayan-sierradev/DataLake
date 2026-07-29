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
        # Required: see QueryCompiler.compile. The service holds it so no per-call site can
        # omit it, but construction itself must supply one.
        scope_predicate: ScopePredicate | None,
        result_cache: SemanticResultCache | None = None,
    ) -> None:
        self._compiler = QueryCompiler(model)
        # Held for the cache key: two model versions must never share an entry, because a
        # definition change is exactly when a cached number becomes wrong.
        self._model = model
        self._engine = engine
        self._resolve_uri = entity_uri_resolver
        self._granted = granted_access_tags
        # Held on the service rather than passed per call so no caller can omit it
        # (DL-SCOPE-17: a surface that bypasses the predicate builder is a defect).
        self._scope_predicate = scope_predicate
        # L17: identical dashboard queries recompute today. The cache key includes the scope
        # signature and the access-tag signature, so two callers with different grants can never
        # share an entry — a cache that ignored either would be an authorization bypass with a
        # performance justification.
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
                sql=compiled.sql, inputs={_ENTITY_VIEW: entity_uri}, params=compiled.parameters
            )
            for row in batch
        ]
        if self._cache is not None and cache_key is not None:
            # `partition_marker` is the analytics partition URI: when a new partition lands the
            # marker changes and the entry is naturally stale, which is the declared invalidation
            # basis rather than a guessed TTL (DL-CFG-05).
            self._cache.put(cache_key, rows, partition_marker=entity_uri)
        if minimum_cohort_size is not None:
            # Small-cohort suppression (DL-SCOPE-16): a benchmark over two franchisees
            # re-identifies both, so under-threshold counts are blanked rather than returned.
            rows = _suppress_small_cohorts(rows, minimum_cohort_size)
        record_platform_metric(
            PlatformMetric.SEMANTIC_QUERY_LATENCY_MS,
            time.monotonic() * 1000 - started_ms,
            EntityType=request.entity,
        )
        return SemanticQueryResult(sql=compiled.sql, rows=rows)


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
