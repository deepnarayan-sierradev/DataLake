"""
The single server-side scope predicate builder (DL-SCOPE-14, DL-SCOPE-17).

Every consumption surface — semantic compiler, serving-store view generation, exports,
scheduled reports, Excel add-in, twin traversal, drill-through, aggregates — obtains its
row filter here. A surface that builds its own is a defect.

Security (OWASP A01): an empty scope set means **no access**, never unrestricted access.
Tenant-wide visibility is an affirmative grant, never an absent or empty claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from contracts.identifier_policy import SAFE_COLUMN_PATTERN, validate_tenant_code
from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from tenancy.scope_contract import (
    IMPLICIT_SCOPE_UNIT_ID,
    PartitionModel,
    ScopeUnit,
    TenantPartitionProfile,
    validate_scope_unit_id,
)

SCOPE_UNIT_COLUMN: Final[str] = "scope_unit_id"


class ConsumptionSurface(StrEnum):
    """Surfaces required to apply the predicate (DL-SCOPE-17); used as a metric dimension."""

    SEMANTIC_QUERY = "semantic_query"
    ATHENA = "athena"
    SERVING_STORE = "serving_store"
    EXPORT = "export"
    SCHEDULED_REPORT = "scheduled_report"
    EXCEL_ADDIN = "excel_addin"
    TWIN_TRAVERSAL = "twin_traversal"
    DRILL_THROUGH = "drill_through"
    AGGREGATE = "aggregate"


class UnrestrictedScopeReason(StrEnum):
    """Why a read legitimately has no end-user claim to scope by; there are very few of these."""

    DEFINITION_VALIDATION = "definition_validation"


class EmptyScopeDenialError(PermissionError):
    """Raised when a claim carries neither a tenant-wide grant nor any scope unit."""


class UnknownScopeUnitError(PermissionError):
    """Raised when a claim names a scope unit that does not exist for the tenant."""


@dataclass(frozen=True)
class ScopeClaims:
    """
    Verified, server-side scope authorisation for one caller.

    Never constructed from request input — built from validated JWT claims by
    `build_scope_claims`, which is the only place a grant is expanded.
    """

    tenant_code: str
    scope_unit_ids: frozenset[str] = frozenset()
    tenant_wide: bool = False
    partition_model: PartitionModel = PartitionModel.PARTITIONED

    def __post_init__(self) -> None:
        validate_tenant_code(self.tenant_code)
        for unit_id in self.scope_unit_ids:
            validate_scope_unit_id(unit_id, allow_reserved=True)

    @property
    def is_empty(self) -> bool:
        return not self.tenant_wide and not self.scope_unit_ids


@dataclass(frozen=True)
class ScopePredicate:
    """A parameterised row filter; `sql` never interpolates a value (OWASP A03)."""

    sql: str
    parameters: dict[str, str] = field(default_factory=dict)
    surface: ConsumptionSurface = ConsumptionSurface.SEMANTIC_QUERY
    matches_all_rows: bool = False

    def matches(self, row_scope_unit_id: str | None) -> bool:
        """In-process equivalent of `sql`, for stores with no SQL surface (twin, exports)."""
        if self.matches_all_rows:
            return True
        allowed = set(self.parameters.values())
        if row_scope_unit_id is None:
            return IMPLICIT_SCOPE_UNIT_ID in allowed
        return row_scope_unit_id in allowed


def expand_scope_grant(
    granted_node_ids: frozenset[str],
    units: list[ScopeUnit],
) -> frozenset[str]:
    """
    Expand hierarchy nodes to their leaf units (DL-SCOPE-10).

    Runs at claim issuance, not per query: a multi-unit owner or a region grant becomes
    the concrete leaf set once and is then cached on the claim.
    """
    children: dict[str, list[str]] = {}
    for unit in units:
        if unit.parent_scope_unit_id:
            children.setdefault(unit.parent_scope_unit_id, []).append(unit.scope_unit_id)

    resolved: set[str] = set()
    pending = list(granted_node_ids)
    while pending:
        node = pending.pop()
        if node in resolved:
            continue
        resolved.add(node)
        pending.extend(children.get(node, []))
    return frozenset(resolved)


def build_scope_claims(
    tenant_code: str,
    profile: TenantPartitionProfile,
    *,
    granted_scope_unit_ids: frozenset[str] = frozenset(),
    tenant_wide: bool = False,
    units: list[ScopeUnit] | None = None,
) -> ScopeClaims:
    """
    Build verified scope claims, expanding hierarchy grants to leaf units.

    A `single` tenant resolves to its one implicit unit whether or not the caller was
    granted anything specific — degenerate, not absent (DL-12 design decision D1).

    For a `partitioned` tenant the unit set is **validated against `units`, always**. This
    previously read `if known and u not in known`, so an empty `units` list — the state a
    partitioned tenant is in before its units are seeded, and the state `effective_only`
    filtering produces — skipped the check entirely and let a crafted `__tenant__` grant through
    to a match-all predicate. An empty unit set is now a denial, not an unvalidated pass.
    """
    validate_tenant_code(tenant_code)
    if profile.partition_model is PartitionModel.SINGLE:
        return ScopeClaims(
            tenant_code=tenant_code,
            scope_unit_ids=frozenset({IMPLICIT_SCOPE_UNIT_ID}),
            tenant_wide=tenant_wide,
            partition_model=PartitionModel.SINGLE,
        )

    if tenant_wide:
        return ScopeClaims(tenant_code=tenant_code, tenant_wide=True)

    if IMPLICIT_SCOPE_UNIT_ID in granted_scope_unit_ids:
        record_platform_metric(PlatformMetric.CROSS_SCOPE_ACCESS_ATTEMPTS, 1.0)
        raise UnknownScopeUnitError(
            f"Scope grant for partitioned tenant {tenant_code!r} names the reserved implicit "
            f"unit {IMPLICIT_SCOPE_UNIT_ID!r}, which exists only for single-partition tenants."
        )

    known = {u.scope_unit_id for u in (units or [])}
    unknown = {u for u in granted_scope_unit_ids if u not in known}
    if unknown:
        record_platform_metric(PlatformMetric.CROSS_SCOPE_ACCESS_ATTEMPTS, len(unknown))
        raise UnknownScopeUnitError(
            f"Scope grant names units that do not exist for tenant {tenant_code!r}: "
            f"{sorted(unknown)}."
        )
    expanded = frozenset(
        u for u in expand_scope_grant(granted_scope_unit_ids, units or []) if u in known
    )
    record_platform_metric(PlatformMetric.SCOPE_GRANT_EXPANSIONS, len(expanded))
    return ScopeClaims(tenant_code=tenant_code, scope_unit_ids=expanded)


def unrestricted_predicate(
    reason: UnrestrictedScopeReason,
    *,
    surface: ConsumptionSurface = ConsumptionSurface.SEMANTIC_QUERY,
    column: str = SCOPE_UNIT_COLUMN,
) -> ScopePredicate:
    """
    The one way to obtain a predicate that filters nothing — an affirmative object, never `None`.

    `None` used to be how a caller said "no scope applies here", and every consumer had an
    `if predicate is None: return` branch that produced no log line, no metric, and no error. That
    is indistinguishable from a caller who simply forgot, which is how a fail-open survives review.
    This is the same tautological SQL a tenant-wide grant produces, so the predicate is still
    *applied* at every call site; the difference is that choosing it is recorded, named, and
    countable.
    """
    if not SAFE_COLUMN_PATTERN.match(column):
        raise ValueError(f"scope column {column!r} is not an allowlisted identifier.")
    record_platform_metric(
        PlatformMetric.UNRESTRICTED_SCOPE_READS, 1.0, Surface=surface.value, Reason=reason.value
    )
    record_platform_metric(PlatformMetric.SCOPE_PREDICATE_APPLIED, 1.0, Surface=surface.value)
    record_platform_metric(PlatformMetric.ROW_LEVEL_PREDICATE_APPLIED, 1.0, Surface=surface.value)
    return ScopePredicate(
        sql=f"({column} IS NOT NULL OR {column} IS NULL)",
        parameters={},
        surface=surface,
        matches_all_rows=True,
    )


def scope_predicate(
    claims: ScopeClaims,
    *,
    surface: ConsumptionSurface = ConsumptionSurface.SEMANTIC_QUERY,
    column: str = SCOPE_UNIT_COLUMN,
    parameter_prefix: str = "scope_unit",
) -> ScopePredicate:
    """
    Build the row filter for one caller on one surface.

    Raises `EmptyScopeDenialError` on an empty scope set — the single most likely
    implementation defect in DL-12 is reading "no units" as "no filter".
    """
    if not SAFE_COLUMN_PATTERN.match(column):
        raise ValueError(f"scope column {column!r} is not an allowlisted identifier.")
    if claims.is_empty:
        record_platform_metric(PlatformMetric.EMPTY_SCOPE_DENIALS, 1.0, Surface=surface.value)
        raise EmptyScopeDenialError(
            f"Scope claim for tenant {claims.tenant_code!r} grants no scope units and no "
            "tenant-wide access. An empty scope set denies all access."
        )

    record_platform_metric(PlatformMetric.SCOPE_PREDICATE_APPLIED, 1.0, Surface=surface.value)
    record_platform_metric(PlatformMetric.ROW_LEVEL_PREDICATE_APPLIED, 1.0, Surface=surface.value)

    if claims.tenant_wide:
        return ScopePredicate(
            sql=f"({column} IS NOT NULL OR {column} IS NULL)",
            parameters={},
            surface=surface,
            matches_all_rows=True,
        )

    ordered = sorted(claims.scope_unit_ids)
    parameters = {f"{parameter_prefix}_{index}": unit for index, unit in enumerate(ordered)}
    placeholders = ", ".join(f":{name}" for name in parameters)
    in_clause = f"{column} IN ({placeholders})"
    if IMPLICIT_SCOPE_UNIT_ID in claims.scope_unit_ids:
        if claims.partition_model is not PartitionModel.SINGLE:
            record_platform_metric(PlatformMetric.CROSS_SCOPE_ACCESS_ATTEMPTS, 1.0)
            raise UnknownScopeUnitError(
                f"Claim for tenant {claims.tenant_code!r} carries the implicit unit but is not "
                "declared single-partition. Build claims through build_scope_claims()."
            )
        return ScopePredicate(
            sql=f"({column} IS NULL OR {in_clause})",
            parameters=parameters,
            surface=surface,
            matches_all_rows=True,
        )
    return ScopePredicate(sql=in_clause, parameters=parameters, surface=surface)
