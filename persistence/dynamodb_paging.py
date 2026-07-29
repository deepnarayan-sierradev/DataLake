"""
The one DynamoDB paging primitive (F10).

Sixteen call sites hand-rolled the same `while True: query → extend → LastEvaluatedKey` loop, with
no shared abstraction. That duplication was not merely untidy — it is how F8's truncation bug
arose: one of the sixteen simply omitted the loop, so `list_for_run` stopped at DynamoDB's 1 MB
page and returned a partial list that was indistinguishable from a clean run. Nothing could detect
the omission, because there was no single place for it to be absent from.

Two entry points, because callers genuinely want different things:

- `iter_items` drains every page. For internal jobs where the whole set is required (usage
  metering over a period, a reconciliation sweep). It yields, so the caller decides what to
  materialise.
- `fetch_page` returns one bounded page plus the cursor to continue. For anything serving a
  request, where an unbounded read is a latency and memory risk that grows with tenant data.

`index_available` is here for the same reason: the "does this GSI exist yet" probe was independently
implemented in two places, and it exists so code can deploy before the Terraform that adds an index.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Final

from botocore.exceptions import ClientError

# DynamoDB will not return more than 1 MB per page regardless of Limit, so a caller asking for
# more than this in one page is expressing a hope, not a bound.
MAX_PAGE_SIZE: Final[int] = 1000
DEFAULT_PAGE_SIZE: Final[int] = 100


class PagingError(Exception):
    """Raised when a paged read fails in a way the caller must handle."""


@dataclass(frozen=True)
class Page:
    """One bounded page of items plus the cursor needed to continue."""

    items: list[dict[str, Any]]
    next_key: dict[str, Any] | None

    @property
    def has_more(self) -> bool:
        return self.next_key is not None


def _read(table: Any, use_query: bool, kwargs: dict[str, Any]) -> Mapping[str, Any]:
    operation = table.query if use_query else table.scan
    try:
        result: Mapping[str, Any] = operation(**kwargs)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        raise PagingError(f"DynamoDB read failed with {code}.") from exc
    return result


def iter_items(table: Any, *, use_query: bool = True, **kwargs: Any) -> Iterator[dict[str, Any]]:
    """
    Yield every item across every page, following `LastEvaluatedKey` to exhaustion.

    Use for internal jobs that genuinely need the whole set. Do not use to serve a request: the
    work grows with the tenant's data, and nothing bounds it.
    """
    read_kwargs = dict(kwargs)
    while True:
        response = _read(table, use_query, read_kwargs)
        for item in response.get("Items", []):
            yield dict(item)
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return
        read_kwargs["ExclusiveStartKey"] = last_key


def fetch_page(
    table: Any,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    start_key: dict[str, Any] | None = None,
    use_query: bool = True,
    **kwargs: Any,
) -> Page:
    """
    Read one bounded page, returning the items and the cursor to continue from.

    `limit` is clamped to `MAX_PAGE_SIZE`. DynamoDB may return fewer items than `limit` while
    still reporting more to come — including **zero** items with a non-null cursor, when a
    FilterExpression discards a whole page. A caller that treats an empty page as the end will
    silently truncate, so honour `next_key`, never `items`.
    """
    if limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit}.")
    read_kwargs = dict(kwargs)
    read_kwargs["Limit"] = min(limit, MAX_PAGE_SIZE)
    if start_key:
        read_kwargs["ExclusiveStartKey"] = start_key

    response = _read(table, use_query, read_kwargs)
    return Page(
        items=[dict(item) for item in response.get("Items", [])],
        next_key=dict(response["LastEvaluatedKey"]) if response.get("LastEvaluatedKey") else None,
    )


def index_available(table: Any, index_name: str, cache: dict[str, bool] | None = None) -> bool:
    """
    Whether `index_name` exists on this table, cached per container when a cache dict is given.

    Exists so application code can deploy before the Terraform that creates an index: a missing
    GSI degrades a Query to a Scan rather than taking the endpoint down. `describe_table` on every
    read would add a round trip to the path the index exists to make cheaper, hence the cache.
    """
    key = f"{table.name}#{index_name}"
    if cache is not None and key in cache:
        return cache[key]
    try:
        description = table.meta.client.describe_table(TableName=table.name)
        indexes = description["Table"].get("GlobalSecondaryIndexes") or []
        present = any(index.get("IndexName") == index_name for index in indexes)
    except ClientError:
        present = False
    if cache is not None:
        cache[key] = present
    return present
