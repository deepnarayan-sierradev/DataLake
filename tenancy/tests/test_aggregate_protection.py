"""Benchmark and aggregate protection tests (DL-SCOPE-16)."""

from __future__ import annotations

import pytest

from tenancy.aggregate_protection import (
    AggregateFunction,
    AggregateSuppressedError,
    BackComputableAggregateError,
    BenchmarkRequest,
    enforce_benchmark,
    evaluate_benchmark,
    suppress_small_cells,
)


def _cohort(size: int, viewer_owns: int = 1) -> BenchmarkRequest:
    units = frozenset(f"franchisee-{i:04d}" for i in range(size))
    viewer = frozenset(f"franchisee-{i:04d}" for i in range(viewer_owns))
    return BenchmarkRequest(
        cohort_scope_unit_ids=units,
        functions=frozenset({AggregateFunction.AVERAGE}),
        viewer_scope_unit_ids=viewer,
    )


class TestCohortFloor:
    def test_large_cohort_is_permitted(self):
        assert evaluate_benchmark(_cohort(10)).permitted is True

    def test_small_cohort_is_suppressed(self):
        verdict = evaluate_benchmark(_cohort(3))
        assert verdict.permitted is False
        assert "below the minimum" in verdict.reason

    def test_peers_are_counted_not_cohort_members(self):
        assert evaluate_benchmark(_cohort(6, viewer_owns=5)).permitted is False

    def test_suppression_message_discloses_no_existence_detail(self):
        with pytest.raises(AggregateSuppressedError) as exc:
            enforce_benchmark(_cohort(2))
        assert "franchisee" not in str(exc.value)

    def test_minimum_below_two_is_rejected(self):
        with pytest.raises(ValueError, match="at least 2"):
            evaluate_benchmark(_cohort(10), minimum_cohort_size=1)


class TestBackComputation:
    def test_rank_plus_average_is_rejected(self):
        request = BenchmarkRequest(
            cohort_scope_unit_ids=frozenset(f"f-{i:04d}" for i in range(20)),
            functions=frozenset({AggregateFunction.RANK, AggregateFunction.AVERAGE}),
        )
        assert evaluate_benchmark(request).permitted is False
        with pytest.raises(BackComputableAggregateError):
            enforce_benchmark(request)

    def test_min_max_average_together_is_rejected(self):
        request = BenchmarkRequest(
            cohort_scope_unit_ids=frozenset(f"f-{i:04d}" for i in range(20)),
            functions=frozenset(
                {AggregateFunction.MIN, AggregateFunction.MAX, AggregateFunction.AVERAGE}
            ),
        )
        with pytest.raises(BackComputableAggregateError):
            enforce_benchmark(request)

    def test_rank_alone_is_permitted(self):
        request = BenchmarkRequest(
            cohort_scope_unit_ids=frozenset(f"f-{i:04d}" for i in range(20)),
            functions=frozenset({AggregateFunction.RANK}),
        )
        assert evaluate_benchmark(request).permitted is True


class TestCellSuppression:
    def test_under_threshold_cells_become_none(self):
        cells = {"north": 12, "south": 3, "east": 5}
        assert suppress_small_cells(cells) == {"north": 12, "south": None, "east": 5}

    def test_key_set_is_preserved_so_absence_is_not_a_signal(self):
        cells = {"north": 1, "south": 1}
        assert set(suppress_small_cells(cells)) == {"north", "south"}
