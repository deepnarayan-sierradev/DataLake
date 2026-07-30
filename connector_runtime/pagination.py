"""
Pagination strategies as a registered concern (DL-CONN-15).

Offset/limit, cursor, keyset, and link-header pagination behind one `PaginationStrategy`
interface. NetSuite's keyset pagination becomes an implementation of the interface rather
than a special case, which closes gap 17.

Every strategy is an iterator end-to-end: a page is yielded, written to the raw layer, and
released. No strategy accumulates a full result set.
"""

from __future__ import annotations

import abc
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

MAX_PAGES: Final[int] = 10_000


class PaginationKind(StrEnum):
    """Registered pagination shapes."""

    OFFSET_LIMIT = "offset_limit"
    CURSOR = "cursor"
    KEYSET = "keyset"
    LINK_HEADER = "link_header"
    PAGE_NUMBER = "page_number"
    SINGLE_REQUEST = "single_request"


class PaginationExhaustionError(Exception):
    """Raised when a provider never signals the end of its result set."""


@dataclass(frozen=True)
class PaginationParameters:
    """
    The query-parameter names one provider uses for the pagination knobs.

    Defaults reproduce the names the strategies previously hardcoded, so a source that never
    declares them behaves exactly as before.
    """

    offset: str = "offset"
    limit: str = "limit"
    cursor: str = "after"
    page: str = "page"
    keyset_after: str = "after_key"
    keyset_field: str = "keyset_field"
    first_page_index: int = 0


@dataclass(frozen=True)
class SourceRequest:
    """One logical read of a source entity, before pagination is applied."""

    entity_id: str
    page_size: int = 100
    query_parameters: Mapping[str, Any] = field(default_factory=dict)
    keyset_field: str | None = None
    initial_cursor: str | None = None
    parameters: PaginationParameters = field(default_factory=PaginationParameters)


@dataclass
class SourcePage:
    """One page of source records, with whatever the provider said about the next page."""

    records: list[dict[str, Any]]
    next_cursor: str | None = None
    next_link: str | None = None
    total_available: int | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.records


PageFetcher = Callable[[Mapping[str, Any]], SourcePage]
"""Adapter-supplied callable: pagination parameters in, one page out."""


class PaginationStrategy(abc.ABC):
    """Port that turns one logical read into a lazy sequence of pages."""

    kind: PaginationKind

    def __init__(self, fetch_page: PageFetcher, max_pages: int = MAX_PAGES) -> None:
        self._fetch_page = fetch_page
        self._max_pages = max_pages
        self.pages_fetched = 0

    @abc.abstractmethod
    def pages(self, request: SourceRequest) -> Iterator[SourcePage]:
        """Yield pages until the provider signals exhaustion."""
        raise NotImplementedError

    def _guard_page_budget(self, request: SourceRequest) -> None:
        if self.pages_fetched >= self._max_pages:
            raise PaginationExhaustionError(
                f"Entity {request.entity_id!r} produced {self.pages_fetched} pages without "
                "signalling exhaustion. Refusing to continue — check the provider's "
                "pagination contract."
            )


class OffsetLimitPagination(PaginationStrategy):
    """`offset`/`limit`; ends on a short page."""

    kind = PaginationKind.OFFSET_LIMIT

    def pages(self, request: SourceRequest) -> Iterator[SourcePage]:
        names = request.parameters
        offset = 0
        while True:
            self._guard_page_budget(request)
            page = self._fetch_page(
                {
                    **request.query_parameters,
                    names.offset: offset,
                    names.limit: request.page_size,
                }
            )
            self.pages_fetched += 1
            if page.records:
                yield page
            if len(page.records) < request.page_size:
                return
            offset += len(page.records)


class CursorPagination(PaginationStrategy):
    """Opaque provider cursor; ends when the cursor is absent."""

    kind = PaginationKind.CURSOR

    def pages(self, request: SourceRequest) -> Iterator[SourcePage]:
        names = request.parameters
        cursor = request.initial_cursor
        seen_cursors: set[str] = set()
        while True:
            self._guard_page_budget(request)
            parameters: dict[str, Any] = {
                **request.query_parameters,
                names.limit: request.page_size,
            }
            if cursor:
                parameters[names.cursor] = cursor
            page = self._fetch_page(parameters)
            self.pages_fetched += 1
            if page.records:
                yield page
            if not page.next_cursor:
                return
            if page.next_cursor in seen_cursors:
                raise PaginationExhaustionError(
                    f"Entity {request.entity_id!r} returned a repeated pagination cursor; "
                    "the provider is not advancing."
                )
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor


class KeysetPagination(PaginationStrategy):
    """
    Seek by the last row's key rather than by offset.

    Correct for large, concurrently-mutating sources where offset paging skips or repeats
    rows as earlier pages shift. Requires a monotonic `keyset_field`.
    """

    kind = PaginationKind.KEYSET

    def pages(self, request: SourceRequest) -> Iterator[SourcePage]:
        if not request.keyset_field:
            raise ValueError(
                f"Keyset pagination for entity {request.entity_id!r} requires keyset_field — "
                "without a monotonic key it cannot seek."
            )
        names = request.parameters
        last_key: Any = None
        while True:
            self._guard_page_budget(request)
            parameters: dict[str, Any] = {
                **request.query_parameters,
                names.limit: request.page_size,
            }
            if last_key is not None:
                parameters[names.keyset_after] = last_key
                parameters[names.keyset_field] = request.keyset_field
            page = self._fetch_page(parameters)
            self.pages_fetched += 1
            if not page.records:
                return
            next_key = page.records[-1].get(request.keyset_field)
            if last_key is not None and next_key == last_key:
                return
            yield page
            if next_key is None:
                return
            last_key = next_key
            if len(page.records) < request.page_size:
                return


_LINK_NEXT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r'<(?P<url>[^>]+)>\s*;\s*rel\s*=\s*"?next"?', re.IGNORECASE
)


def parse_next_link(link_header: str) -> str | None:
    """Extract the `rel="next"` URL from an RFC 8288 Link header."""
    match = _LINK_NEXT_PATTERN.search(link_header or "")
    return match.group("url") if match else None


class LinkHeaderPagination(PaginationStrategy):
    """Follows `Link: <...>; rel="next"` until the header stops offering one."""

    kind = PaginationKind.LINK_HEADER

    def pages(self, request: SourceRequest) -> Iterator[SourcePage]:
        names = request.parameters
        next_url: str | None = None
        seen_urls: set[str] = set()
        while True:
            self._guard_page_budget(request)
            parameters: dict[str, Any] = {
                **request.query_parameters,
                names.limit: request.page_size,
            }
            if next_url:
                parameters["url"] = next_url
            page = self._fetch_page(parameters)
            self.pages_fetched += 1
            if page.records:
                yield page
            candidate = page.next_link or parse_next_link(
                str(page.headers.get("Link", page.headers.get("link", "")))
            )
            if not candidate or candidate in seen_urls:
                return
            seen_urls.add(candidate)
            next_url = candidate


class PageNumberPagination(PaginationStrategy):
    """
    Increments a page index rather than a row offset; ends on a short page.

    Distinct from offset/limit because the provider counts pages, not rows: WellSky's
    `_page`/`_count` advances by one per request, so reusing offset paging would skip
    `page_size - 1` pages out of every `page_size`.
    """

    kind = PaginationKind.PAGE_NUMBER

    def pages(self, request: SourceRequest) -> Iterator[SourcePage]:
        names = request.parameters
        page_index = names.first_page_index
        while True:
            self._guard_page_budget(request)
            page = self._fetch_page(
                {
                    **request.query_parameters,
                    names.page: page_index,
                    names.limit: request.page_size,
                }
            )
            self.pages_fetched += 1
            if page.records:
                yield page
            if len(page.records) < request.page_size:
                return
            page_index += 1


class SingleRequestPagination(PaginationStrategy):
    """
    One request, one page — for an endpoint the provider does not paginate at all.

    Modelling it as a strategy rather than a `None` keeps the connector free of a
    "is this entity paginated" branch, and makes the absence of paging a declared fact.
    """

    kind = PaginationKind.SINGLE_REQUEST

    def pages(self, request: SourceRequest) -> Iterator[SourcePage]:
        self._guard_page_budget(request)
        page = self._fetch_page(dict(request.query_parameters))
        self.pages_fetched += 1
        if page.records:
            yield page


class PaginationStrategyRegistry:
    """Named pagination kinds; a new source composes an existing one."""

    def __init__(self) -> None:
        self._strategies: dict[str, type[PaginationStrategy]] = {}

    def register(self, name: str, strategy_cls: type[PaginationStrategy]) -> None:
        if name in self._strategies:
            raise ValueError(f"Pagination strategy {name!r} is already registered.")
        self._strategies[name] = strategy_cls

    def registered_names(self) -> list[str]:
        return sorted(self._strategies)

    def resolve(
        self, name: str, fetch_page: PageFetcher, max_pages: int = MAX_PAGES
    ) -> PaginationStrategy:
        strategy_cls = self._strategies.get(name)
        if strategy_cls is None:
            raise KeyError(
                f"No pagination strategy registered under {name!r}. "
                f"Registered: {self.registered_names()}."
            )
        return strategy_cls(fetch_page, max_pages)

    def reset(self) -> None:
        """Testing only."""
        self._strategies.clear()


pagination_strategy_registry: Final[PaginationStrategyRegistry] = PaginationStrategyRegistry()
pagination_strategy_registry.register(PaginationKind.OFFSET_LIMIT.value, OffsetLimitPagination)
pagination_strategy_registry.register(PaginationKind.CURSOR.value, CursorPagination)
pagination_strategy_registry.register(PaginationKind.KEYSET.value, KeysetPagination)
pagination_strategy_registry.register(PaginationKind.LINK_HEADER.value, LinkHeaderPagination)
pagination_strategy_registry.register(PaginationKind.PAGE_NUMBER.value, PageNumberPagination)
pagination_strategy_registry.register(PaginationKind.SINGLE_REQUEST.value, SingleRequestPagination)


def stream_records(
    strategy: PaginationStrategy, request: SourceRequest
) -> Iterator[dict[str, Any]]:
    """Flatten pages to records without materialising the result set."""
    for page in strategy.pages(request):
        yield from page.records


def page_record_counts(pages: Sequence[SourcePage]) -> list[int]:
    """Per-page counts, for `PagesFetched` and progress logging."""
    return [len(page.records) for page in pages]
