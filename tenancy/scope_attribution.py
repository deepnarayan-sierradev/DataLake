"""
Scope attribution at ingestion (DL-SCOPE-09).

`scope_unit_id` is stamped once, from the connection owner for scope-unit-owned
connections or from a declared mapping for tenant-owned ones, and then flows unchanged
through raw → curated → golden → analytics → twin → serving. There is exactly one place
attribution can be wrong.

Security (OWASP A05): a row that cannot be attributed gets `scope_unit_id = null`, which
resolves to tenant-level visibility — visible only to tenant-scoped roles, never to all
scope units.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger
from tenancy.scope_contract import (
    IMPLICIT_SCOPE_UNIT_ID,
    AttributionMode,
    PartitionModel,
    TenantPartitionProfile,
    validate_scope_unit_id,
)
from tenancy.scope_predicate import SCOPE_UNIT_COLUMN
from tenancy.source_connection import SourceConnection

_logger = get_platform_logger(__name__)

# Above this share of unattributable rows the mapping is treated as broken (DL-DQ-14).
DEFAULT_UNATTRIBUTED_THRESHOLD_PCT: float = 5.0


@dataclass
class AttributionOutcome:
    """Counts for `UnattributedRowRate` and the quality exception it may raise."""

    total_rows: int = 0
    unattributed_rows: int = 0

    @property
    def unattributed_rate_pct(self) -> float:
        if self.total_rows == 0:
            return 0.0
        return 100.0 * self.unattributed_rows / self.total_rows

    def exceeds(self, threshold_pct: float = DEFAULT_UNATTRIBUTED_THRESHOLD_PCT) -> bool:
        return self.unattributed_rate_pct > threshold_pct


class ScopeAttributor:
    """Stamps `scope_unit_id` on extracted records for one connection."""

    def __init__(
        self,
        connection: SourceConnection | None,
        profile: TenantPartitionProfile,
        known_scope_unit_ids: frozenset[str] | None = None,
    ) -> None:
        self._connection = connection
        self._profile = profile
        self._known = known_scope_unit_ids
        self.outcome = AttributionOutcome()

    def resolve(self, record: dict[str, Any]) -> str | None:
        """Owning scope unit for one record, or None when unattributable."""
        if self._profile.partition_model is PartitionModel.SINGLE:
            # Degenerate by construction — every row belongs to the one implicit unit.
            return IMPLICIT_SCOPE_UNIT_ID

        if self._connection is None:
            # Partitioned tenant with no connection context: unattributable, which fails closed
            # downstream rather than defaulting to the implicit unit (which would be match-all).
            return None

        owner = self._connection.owning_scope_unit_id()
        if owner is not None:
            return owner

        if self._connection.attribution_mode is AttributionMode.FIELD_DERIVED:
            field = self._connection.scope_attribution_field
            raw = record.get(field) if field else None
            if raw in (None, ""):
                return None
            candidate = str(raw).strip().lower()
            try:
                validate_scope_unit_id(candidate)
            except ValueError:
                return None
            if self._known is not None and candidate not in self._known:
                return None
            return candidate
        return None

    def stamp(self, record: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of `record` carrying `scope_unit_id`; counts the outcome."""
        scope_unit_id = self.resolve(record)
        self.outcome.total_rows += 1
        if scope_unit_id is None:
            self.outcome.unattributed_rows += 1
        return {**record, SCOPE_UNIT_COLUMN: scope_unit_id}

    def stamp_all(self, records: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        """Streaming stamp — never materialises the batch (performance contract)."""
        for record in records:
            yield self.stamp(record)

    def log_outcome(self, entity_id: str) -> None:
        record_platform_metric(
            PlatformMetric.UNATTRIBUTED_ROW_RATE,
            self.outcome.unattributed_rate_pct,
            EntityId=entity_id,
        )
        if self.outcome.exceeds():
            _logger.warning(
                "scope_attribution_unattributed_rate_exceeded",
                tenant_code=self._connection.tenant_code if self._connection else "",
                connection_id=self._connection.connection_id if self._connection else "",
                entity_id=entity_id,
                unattributed_rate_pct=round(self.outcome.unattributed_rate_pct, 2),
            )
