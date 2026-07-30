"""
Negative tests for the paging primitive (F10).

This module exists because sixteen hand-rolled copies of the same loop meant one of them could
omit the loop entirely and nothing would notice — which is exactly what happened to
`list_for_run`. The assertions that matter here are the ones a single-page implementation fails.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from persistence.dynamodb_paging import (
    MAX_PAGE_SIZE,
    PagingError,
    fetch_page,
    index_available,
    iter_items,
)


class _FakeTable:
    """Returns a scripted sequence of pages and records the kwargs it was called with."""

    name = "DatalakeFake"

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.calls: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._pages[len(self.calls) - 1]

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        return self.query(**kwargs)


def _page(keys: list[str], next_key: str | None = None) -> dict[str, Any]:
    page: dict[str, Any] = {"Items": [{"k": key} for key in keys]}
    if next_key is not None:
        page["LastEvaluatedKey"] = {"k": next_key}
    return page


class TestIterItemsDrainsEveryPage:
    def test_follows_the_cursor_to_exhaustion(self) -> None:
        table = _FakeTable([_page(["a"], "a"), _page(["b"], "b"), _page(["c"])])
        assert [item["k"] for item in iter_items(table)] == ["a", "b", "c"]
        assert len(table.calls) == 3

    def test_threads_the_exclusive_start_key(self) -> None:
        table = _FakeTable([_page(["a"], "a"), _page(["b"])])
        list(iter_items(table))
        assert "ExclusiveStartKey" not in table.calls[0]
        assert table.calls[1]["ExclusiveStartKey"] == {"k": "a"}

    def test_a_single_page_stops_cleanly(self) -> None:
        table = _FakeTable([_page(["only"])])
        assert [item["k"] for item in iter_items(table)] == ["only"]
        assert len(table.calls) == 1

    def test_an_empty_result_yields_nothing(self) -> None:
        assert list(iter_items(_FakeTable([_page([])]))) == []

    def test_it_is_lazy(self) -> None:
        table = _FakeTable([_page(["a"], "a"), _page(["b"])])
        iterator = iter_items(table)
        assert table.calls == []
        next(iterator)
        assert len(table.calls) == 1


class TestFetchPageIsBounded:
    def test_returns_items_and_cursor(self) -> None:
        page = fetch_page(_FakeTable([_page(["a", "b"], "b")]), limit=2)
        assert [item["k"] for item in page.items] == ["a", "b"]
        assert page.next_key == {"k": "b"}
        assert page.has_more is True

    def test_last_page_has_no_cursor(self) -> None:
        page = fetch_page(_FakeTable([_page(["a"])]), limit=10)
        assert page.next_key is None
        assert page.has_more is False

    def test_reads_exactly_one_page(self) -> None:
        table = _FakeTable([_page(["a"], "a"), _page(["b"])])
        fetch_page(table, limit=1)
        assert len(table.calls) == 1

    def test_limit_is_clamped_to_the_dynamodb_page_ceiling(self) -> None:
        table = _FakeTable([_page(["a"])])
        fetch_page(table, limit=10_000)
        assert table.calls[0]["Limit"] == MAX_PAGE_SIZE

    def test_zero_limit_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            fetch_page(_FakeTable([_page([])]), limit=0)

    def test_empty_page_with_a_cursor_still_reports_more(self) -> None:
        page = fetch_page(_FakeTable([_page([], "next")]), limit=10)
        assert page.items == []
        assert page.has_more is True


class TestFailuresAreTyped:
    def test_client_error_becomes_paging_error(self) -> None:
        class _Failing:
            name = "DatalakeFake"

            def query(self, **kwargs: Any) -> dict[str, Any]:
                raise ClientError(
                    {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Query"
                )

        with pytest.raises(PagingError, match="ProvisionedThroughputExceededException"):
            list(iter_items(_Failing()))


class TestIndexAvailability:
    class _Table:
        name = "DatalakeFake"

        def __init__(self, indexes: list[str], fail: bool = False) -> None:
            self._indexes = indexes
            self._fail = fail
            self.describe_calls = 0
            outer = self

            class _Meta:
                @property
                def client(self) -> Any:
                    return outer

            self.meta = _Meta()

        def describe_table(self, TableName: str) -> dict[str, Any]:  # noqa: N803 — boto3 kwarg
            self.describe_calls += 1
            if self._fail:
                raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "DescribeTable")
            return {
                "Table": {"GlobalSecondaryIndexes": [{"IndexName": name} for name in self._indexes]}
            }

    def test_present_index_is_found(self) -> None:
        assert index_available(self._Table(["tenant-started-index"]), "tenant-started-index")

    def test_absent_index_is_reported_absent(self) -> None:
        assert not index_available(self._Table([]), "tenant-started-index")

    def test_describe_failure_degrades_rather_than_raising(self) -> None:
        assert not index_available(self._Table([], fail=True), "any-index")

    def test_result_is_cached_when_a_cache_is_supplied(self) -> None:
        table = self._Table(["idx"])
        cache: dict[str, bool] = {}
        assert index_available(table, "idx", cache)
        assert index_available(table, "idx", cache)
        assert table.describe_calls == 1

    def test_cache_is_keyed_by_table_and_index(self) -> None:
        table = self._Table(["idx"])
        cache: dict[str, bool] = {}
        index_available(table, "idx", cache)
        index_available(table, "other", cache)
        assert table.describe_calls == 2
