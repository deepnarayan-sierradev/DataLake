"""
Aggregate and benchmark protection (DL-SCOPE-16).

Peer comparison computes over data the viewer cannot see, so without a k-anonymity floor
a benchmark widget is a data-exfiltration path with a friendly UI. Rank combined with an
average also permits back-computation of an individual unit's figure, so that combination
is rejected rather than suppressed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric

DEFAULT_MINIMUM_COHORT_SIZE: Final[int] = 5


class AggregateFunction(StrEnum):
    """Aggregate shapes a benchmark may request."""

    COUNT = "count"
    SUM = "sum"
    AVERAGE = "average"
    MEDIAN = "median"
    MIN = "min"
    MAX = "max"
    RANK = "rank"
    PERCENTILE = "percentile"


# Combinations that let a viewer recover a single unit's value from a cohort result.
_BACK_COMPUTABLE_PAIRS: Final[frozenset[frozenset[AggregateFunction]]] = frozenset(
    {
        frozenset({AggregateFunction.RANK, AggregateFunction.AVERAGE}),
        frozenset({AggregateFunction.RANK, AggregateFunction.SUM}),
        frozenset({AggregateFunction.MIN, AggregateFunction.MAX, AggregateFunction.AVERAGE}),
    }
)


class AggregateSuppressedError(PermissionError):
    """Raised when a cohort is too small to publish an aggregate over."""


class BackComputableAggregateError(PermissionError):
    """Raised when the requested aggregate combination permits per-unit back-computation."""


@dataclass(frozen=True)
class BenchmarkRequest:
    """A peer-comparison request over a cohort of scope units."""

    cohort_scope_unit_ids: frozenset[str]
    functions: frozenset[AggregateFunction]
    viewer_scope_unit_ids: frozenset[str] = field(default_factory=frozenset)

    @property
    def cohort_size(self) -> int:
        return len(self.cohort_scope_unit_ids)

    @property
    def peer_count(self) -> int:
        """Units in the cohort the viewer cannot already see directly."""
        return len(self.cohort_scope_unit_ids - self.viewer_scope_unit_ids)


@dataclass(frozen=True)
class BenchmarkVerdict:
    """Outcome of the protection check, carrying the reason for an operator."""

    permitted: bool
    cohort_size: int
    reason: str = ""


def evaluate_benchmark(
    request: BenchmarkRequest,
    minimum_cohort_size: int = DEFAULT_MINIMUM_COHORT_SIZE,
) -> BenchmarkVerdict:
    """
    Decide whether a benchmark may be published.

    Suppression counts *peers*, not cohort members: a cohort of five in which the viewer
    owns four leaves one identifiable unit, which the raw cohort size would not catch.
    """
    if minimum_cohort_size < 2:
        raise ValueError("minimum_cohort_size must be at least 2 to protect any individual unit.")
    if _is_back_computable(request.functions):
        return BenchmarkVerdict(
            permitted=False,
            cohort_size=request.cohort_size,
            reason=(
                "Requested aggregate combination permits back-computation of an individual "
                f"unit's figure: {sorted(f.value for f in request.functions)}."
            ),
        )
    if request.peer_count < minimum_cohort_size:
        return BenchmarkVerdict(
            permitted=False,
            cohort_size=request.cohort_size,
            reason=(
                f"Cohort contains {request.peer_count} peer unit(s), below the minimum of "
                f"{minimum_cohort_size}."
            ),
        )
    return BenchmarkVerdict(permitted=True, cohort_size=request.cohort_size)


def enforce_benchmark(
    request: BenchmarkRequest,
    minimum_cohort_size: int = DEFAULT_MINIMUM_COHORT_SIZE,
) -> BenchmarkVerdict:
    """Raise rather than return when a benchmark must not be published."""
    verdict = evaluate_benchmark(request, minimum_cohort_size)
    record_platform_metric(PlatformMetric.BENCHMARK_COHORT_SIZE, request.peer_count)
    if verdict.permitted:
        return verdict
    record_platform_metric(PlatformMetric.AGGREGATE_SUPPRESSIONS)
    if _is_back_computable(request.functions):
        raise BackComputableAggregateError(verdict.reason)
    # Existence disclosure: the message names no unit and no count of the viewer's peers.
    raise AggregateSuppressedError(
        "This comparison cannot be shown because the peer group is too small."
    )


def suppress_small_cells(
    cells: dict[str, int],
    minimum_cohort_size: int = DEFAULT_MINIMUM_COHORT_SIZE,
) -> dict[str, int | None]:
    """Replace under-threshold cell counts with None; the key set is preserved."""
    return {key: (value if value >= minimum_cohort_size else None) for key, value in cells.items()}


def _is_back_computable(functions: frozenset[AggregateFunction]) -> bool:
    return any(pair <= functions for pair in _BACK_COMPUTABLE_PAIRS)
