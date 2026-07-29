"""
Semantic result cache (DL-SEM-12).

Keyed on `(tenant, model_version, compiled_query_hash, access_tags, scope_signature)` with
explicit invalidation on model publish and on analytics partition change — never TTL-only,
because a stale figure that expires on its own schedule is indistinguishable from a correct
one while it is being read.

The access tags and scope signature are part of the key so a cache hit can never return rows
one caller may see to another who may not (OWASP A01).
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Final

from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger
from tenancy.scope_predicate import ScopePredicate

_logger = get_platform_logger(__name__)

DEFAULT_MAX_ENTRIES: Final[int] = 512

# A backstop only — invalidation is explicit; this bounds unbounded staleness if a publish
# signal is ever missed.
DEFAULT_MAX_AGE_SECONDS: Final[float] = 900.0


def scope_signature(predicate: ScopePredicate | None) -> str:
    """Stable signature of the scope filter, so two scopes never share a cache entry."""
    if predicate is None:
        return "none"
    ordered = "|".join(f"{k}={predicate.parameters[k]}" for k in sorted(predicate.parameters))
    return hashlib.sha256(f"{predicate.sql}#{ordered}".encode()).hexdigest()[:16]


def compiled_query_hash(sql: str, parameters: list[Any]) -> str:
    """Hash of the compiled statement and its bound values."""
    payload = sql + "|" + "|".join(repr(p) for p in parameters)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ResultCacheKey:
    """The full cache key; every component is part of the isolation guarantee."""

    tenant_code: str
    model_version: str
    query_hash: str
    access_tags_signature: str
    scope_signature: str

    @classmethod
    def build(
        cls,
        tenant_code: str,
        model_version: str,
        sql: str,
        parameters: list[Any],
        granted_access_tags: frozenset[str],
        predicate: ScopePredicate | None = None,
    ) -> ResultCacheKey:
        tags = ",".join(sorted(granted_access_tags))
        return cls(
            tenant_code=tenant_code,
            model_version=model_version,
            query_hash=compiled_query_hash(sql, parameters),
            access_tags_signature=hashlib.sha256(tags.encode()).hexdigest()[:16],
            scope_signature=scope_signature(predicate),
        )

    def as_string(self) -> str:
        return "|".join(
            (
                self.tenant_code,
                self.model_version,
                self.query_hash,
                self.access_tags_signature,
                self.scope_signature,
            )
        )


@dataclass
class _CacheEntry:
    rows: list[dict[str, Any]]
    stored_at: float
    partition_marker: str


@dataclass
class SemanticResultCache:
    """Bounded in-process cache with explicit, signal-driven invalidation."""

    max_entries: int = DEFAULT_MAX_ENTRIES
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS
    _entries: OrderedDict[str, _CacheEntry] = field(default_factory=OrderedDict, repr=False)
    hits: int = 0
    misses: int = 0

    def get(self, key: ResultCacheKey, partition_marker: str = "") -> list[dict[str, Any]] | None:
        """
        Return cached rows, or None on a miss.

        A changed `partition_marker` (the analytics partition date or its object version) is
        treated as a miss, which is the analytics-partition-change invalidation.
        """
        entry = self._entries.get(key.as_string())
        if entry is None:
            self.misses += 1
            return None
        if partition_marker and entry.partition_marker != partition_marker:
            del self._entries[key.as_string()]
            self.misses += 1
            return None
        if time.monotonic() - entry.stored_at > self.max_age_seconds:
            del self._entries[key.as_string()]
            self.misses += 1
            return None
        self._entries.move_to_end(key.as_string())
        self.hits += 1
        record_platform_metric(PlatformMetric.SEMANTIC_CACHE_HIT_RATE, self.hit_rate_pct)
        return entry.rows

    def put(
        self,
        key: ResultCacheKey,
        rows: list[dict[str, Any]],
        partition_marker: str = "",
    ) -> None:
        cache_key = key.as_string()
        self._entries[cache_key] = _CacheEntry(
            rows=rows, stored_at=time.monotonic(), partition_marker=partition_marker
        )
        self._entries.move_to_end(cache_key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def invalidate_tenant(self, tenant_code: str) -> int:
        """Drop every entry for one tenant; returns the number evicted."""
        stale = [k for k in self._entries if k.startswith(f"{tenant_code}|")]
        for key in stale:
            del self._entries[key]
        return len(stale)

    def invalidate_model_version(self, tenant_code: str, model_version: str) -> int:
        """Wired to model publish and activate — the signal, not a timer (DL-CFG-04)."""
        prefix = f"{tenant_code}|{model_version}|"
        stale = [k for k in self._entries if k.startswith(prefix)]
        for key in stale:
            del self._entries[key]
        _logger.info(
            "semantic_result_cache_invalidated",
            tenant_code=tenant_code,
            model_version=model_version,
            evicted=len(stale),
        )
        return len(stale)

    def clear(self) -> None:
        self._entries.clear()

    @property
    def hit_rate_pct(self) -> float:
        total = self.hits + self.misses
        return 0.0 if total == 0 else 100.0 * self.hits / total

    @property
    def size(self) -> int:
        return len(self._entries)
