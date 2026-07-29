"""
NetSuite connector adapter.

Implements ConnectorInterface for NetSuite as the single, metadata-driven
adapter for all NetSuite record types.  No record-type-specific subclasses.

Design:
  - All fields discovered at runtime via Metadata Catalog API.
  - Single connector class handles all NetSuite record types (customer,
    transaction, item, etc.) through configuration only.
  - Registers itself with the platform ConnectorRegistry at import time.
  - Paginates SuiteQL results using offset/limit (NetSuite page size: 1,000).

Authentication:
  - NetSuite TBA (Token-Based Authentication) — OAuth 1.0a HMAC-SHA256.
  - Credentials fetched from AWS Secrets Manager.
  - Per-request signing — no cached token to expire.

Security (OWASP A03, A07, A09):
  - SuiteQL built from validated, discovered field names only.
  - Watermark values substituted via bind_parameters() with ISO-8601 validation.
  - Credentials never in logs or exception messages.

Known limitation — SuiteQL offset pagination ceiling:
  NetSuite's REST SuiteQL endpoint unconditionally rejects any request whose
  ``offset`` parameter exceeds 100,000, regardless of the underlying result
  set size. Because pagination here is plain offset/limit (not a keyset /
  cursor scheme), any single extraction window whose result set spans more
  than 100,000 rows hits that ceiling and fails deterministically — the same
  offset will be rejected on every retry, permanently wedging the entity
  until the operator intervenes. A full keyset-pagination redesign (walking
  a monotonic column instead of offset/limit) is the real fix but is
  deferred; in the meantime, execute_extraction() fails fast with
  NetSuiteSuiteQLOffsetLimitExceededError as soon as the next page would
  cross the ceiling, instructing the operator to narrow the extraction
  window (e.g. lower entity_configuration extraction_window_days) or reduce
  the watermark increment so each run's result set stays under 100,000 rows.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, Final

import requests

from connector_runtime.adapters.netsuite.netsuite_auth_client import NetSuiteAuthClient
from connector_runtime.adapters.netsuite.netsuite_incremental_query_planner import (
    NetSuiteIncrementalQueryPlanner,
)
from connector_runtime.adapters.netsuite.netsuite_metadata_adapter import (
    NetSuiteMetadataAdapter,
    NetSuiteMetadataAdapterError,
)
from connector_runtime.adapters.netsuite.netsuite_params import NetSuiteConnectorParams
from connector_runtime.interfaces.connector_interface import (
    ConnectorCapabilities,
    ConnectorInterface,
    DeterministicConnectorError,
    ExtractionErrorClassification,
    ExtractionRecord,
    FieldContract,
    QueryContract,
    TransientConnectorError,
)
from connector_runtime.registry import connector_registry
from contracts.entity_configuration_contract import FieldMode, LoadType
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_SOURCE_ID: Final[str] = "netsuite"

# SuiteQL endpoint URL template.
_SUITEQL_URL_TEMPLATE: Final[str] = (
    "https://{account_id}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"
)

# NetSuite SuiteQL maximum page size is 10,000 rows per request.
# Default set to 10,000 (§3.6) — reduces API calls 10x vs the previous 1,000.
# Individual entities can override via connector_params.page_size.
_PAGE_SIZE: Final[int] = 10_000

# NetSuite's REST SuiteQL endpoint rejects any request whose `offset` query
# parameter exceeds this value (HTTP 400) — a hard platform ceiling, not a
# per-account or configurable limit. See the module docstring's "Known
# limitation" section for why this is handled as a fail-fast rather than a
# retry, and for the deferred full fix (keyset pagination).
_MAX_SUITEQL_OFFSET: Final[int] = 100_000

# Keyset seek columns are interpolated into the SuiteQL text (the endpoint has no parameter
# slots), so the identifier is allowlisted before it gets there (OWASP A03).
_SAFE_SUITEQL_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class NetSuiteSuiteQLRateLimitError(TransientConnectorError):
    """Raised when NetSuite returns HTTP 429 (SuiteQL rate limit exceeded)."""

    classification = ExtractionErrorClassification.TRANSIENT_THROTTLE


class NetSuiteSuiteQLKeysetError(DeterministicConnectorError):
    """Raised when an entity's watermark field cannot be used as a keyset seek column."""

    classification = ExtractionErrorClassification.DETERMINISTIC_INVALID_CONFIGURATION


class NetSuiteSuiteQLOffsetLimitExceededError(DeterministicConnectorError):
    """
    Raised when the next SuiteQL page would require an offset beyond
    NetSuite's hard 100,000-row pagination ceiling (_MAX_SUITEQL_OFFSET).

    This is deterministic, not transient: NetSuite rejects the offending
    offset on every request regardless of retry count, so the reliability
    framework's exponential-backoff retry would just burn through its retry
    budget and fail identically each time, permanently wedging the entity.
    The only fix is operator action — narrow the extraction window (e.g.
    lower entity_configuration extraction_window_days) or reduce the
    watermark increment so each run's result set stays under 100,000 rows.
    """

    classification = ExtractionErrorClassification.DETERMINISTIC_INVALID_CONFIGURATION


@connector_registry.register(_SOURCE_ID)
class NetSuiteConnector(ConnectorInterface):
    """
    Metadata-driven NetSuite connector for all NetSuite record types.

    One instance per extraction run.  The NetSuite record type is provided
    as a constructor argument (from entity config) and is never hardcoded.

    Constructor args are NOT used for credentials — those come exclusively
    from AWS Secrets Manager via NetSuiteAuthClient.
    """

    def __init__(
        self,
        environment: str,
        region_name: str,
        record_type: str,
    ) -> None:
        if not record_type:
            raise ValueError("record_type must not be empty.")
        self._record_type = record_type
        self._auth = NetSuiteAuthClient(
            environment=environment,
            region_name=region_name,
        )
        # Create the metadata adapter once — the per-instance cache on the adapter
        # prevents redundant Metadata Catalog API calls within the same extraction run.
        self._metadata_adapter = NetSuiteMetadataAdapter(
            auth_client=self._auth,
            record_type=record_type,
        )

    def get_capability_declaration(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            source_id=_SOURCE_ID,
            supports_bulk_extraction=False,
            supports_incremental=True,
            supports_full_load=True,
            supports_metadata_discovery=True,
            bulk_threshold_records=0,
            max_concurrent_jobs=1,
            supported_field_modes=(
                FieldMode.ALL,
                FieldMode.STANDARD,
                FieldMode.CUSTOM,
                FieldMode.INCLUDE_ONLY,
            ),
        )

    def discover_queryable_fields(
        self,
        source_id: str,
        entity_id: str,
        field_mode: FieldMode,
        include_fields: list[str],
        exclude_fields: list[str],
    ) -> FieldContract:
        """
        Discover all queryable fields for this record type via Metadata Catalog.

        New fields added to the NetSuite record type appear automatically in
        the next run without any code changes.
        """
        return self._metadata_adapter.discover_fields(
            source_id=source_id,
            entity_id=entity_id,
            field_mode=field_mode,
            include_fields=include_fields,
            exclude_fields=exclude_fields,
        )

    def build_extraction_query(
        self,
        field_contract: FieldContract,
        load_type: LoadType,
        watermark_field: str | None,
        watermark_lower: str | None,
        watermark_upper: str | None,
        extraction_window_days: int,
    ) -> QueryContract:
        """
        Build a parameterized SuiteQL query from the discovered FieldContract.

        Watermark bounds are stored as query_parameters — never interpolated
        into query_text.  bind_parameters() is called at execution time.
        """
        planner = NetSuiteIncrementalQueryPlanner(record_type=self._record_type)
        return planner.build(
            field_contract=field_contract,
            load_type=load_type,
            watermark_field=watermark_field,
            watermark_lower=watermark_lower,
            watermark_upper=watermark_upper,
            extraction_window_days=extraction_window_days,
        )

    def execute_extraction(
        self,
        query_contract: QueryContract,
        run_id: str,
    ) -> Iterator[ExtractionRecord]:
        """
        Execute paginated SuiteQL extraction and yield records.

        Pages through all results using offset/limit.  Sets source_timestamp
        from the watermark field value for each record when available.
        """
        _logger.info(
            "netsuite_extraction_started",
            source_id=query_contract.source_id,
            entity_id=query_contract.entity_id,
            run_id=run_id,
            load_type=str(query_contract.load_type),
            record_type=self._record_type,
        )

        bound_query = NetSuiteIncrementalQueryPlanner.bind_parameters(
            query_text=query_contract.query_text,
            parameters=query_contract.query_parameters,
        )

        suiteql_url = _SUITEQL_URL_TEMPLATE.format(account_id=self._auth.account_id)
        record_count = 0
        offset = 0
        # L17: keyset pagination seeks by the watermark column instead of walking an offset, so
        # the 100,000-row ceiling stops applying. It needs a monotonic column to seek on, which is
        # exactly what a watermark field is — when the entity has none, offset/limit remains the
        # only option and the ceiling below is still the honest answer.
        keyset_field = query_contract.watermark_field
        keyset_cursor: str | None = None

        while True:
            if keyset_field is None and offset > _MAX_SUITEQL_OFFSET:
                # Stop BEFORE issuing a request NetSuite will reject outright —
                # see module docstring "Known limitation" and
                # NetSuiteSuiteQLOffsetLimitExceededError's docstring.
                raise NetSuiteSuiteQLOffsetLimitExceededError(
                    f"NetSuite SuiteQL pagination for source_id={query_contract.source_id!r}, "
                    f"entity_id={query_contract.entity_id!r} (record_type={self._record_type!r}) "
                    f"would require offset={offset}, which exceeds NetSuite's hard "
                    f"{_MAX_SUITEQL_OFFSET:,}-row pagination ceiling. This extraction window "
                    "has more matching rows than SuiteQL offset/limit pagination can page "
                    "through, and retrying will fail identically every time. Narrow the "
                    "extraction window (e.g. lower entity_configuration "
                    "extraction_window_days) or reduce the watermark increment so each run's "
                    "result set stays under 100,000 rows, then re-run this entity."
                )

            page_query = (
                _with_keyset_predicate(bound_query, keyset_field, keyset_cursor)
                if keyset_field is not None
                else bound_query
            )
            page_rows = list(
                self._fetch_page(
                    suiteql_url=suiteql_url,
                    query=page_query,
                    # Keyset pagination always asks for the first page of the *remaining* rows, so
                    # the offset stays at zero and never approaches the ceiling.
                    offset=0 if keyset_field is not None else offset,
                    limit=_PAGE_SIZE,
                )
            )
            if not page_rows:
                break

            for row in page_rows:
                record_count += 1
                rec = ExtractionRecord(payload=row)
                if query_contract.watermark_field and query_contract.watermark_field in row:
                    rec.source_timestamp = row[query_contract.watermark_field]
                yield rec

            if len(page_rows) < _PAGE_SIZE:
                # Last page — no more data.
                break

            if keyset_field is not None:
                advanced = _next_keyset_cursor(page_rows, keyset_field, keyset_cursor)
                if advanced is None:
                    # The cursor did not move: every row in this page shares one watermark value,
                    # so seeking past it would skip rows and seeking to it would loop forever.
                    # Fall back to offset for the remainder rather than risk either.
                    _logger.warning(
                        "netsuite_keyset_cursor_stalled_falling_back_to_offset",
                        entity_id=query_contract.entity_id,
                        keyset_field=keyset_field,
                        cursor=keyset_cursor,
                    )
                    keyset_field = None
                    offset += _PAGE_SIZE
                else:
                    keyset_cursor = advanced
            else:
                offset += _PAGE_SIZE

        _logger.info(
            "netsuite_extraction_completed",
            source_id=query_contract.source_id,
            entity_id=query_contract.entity_id,
            run_id=run_id,
            record_count=record_count,
        )

    def classify_extraction_error(self, exc: Exception) -> ExtractionErrorClassification:
        """
        Classify a NetSuite extraction exception for the retry framework.

        Credential, auth, query-planner, rate-limit, and SuiteQL-offset-limit
        exceptions carry their own classification via the shared
        TransientConnectorError / DeterministicConnectorError markers (DP-3) —
        see netsuite_auth_client.py, netsuite_incremental_query_planner.py,
        NetSuiteSuiteQLRateLimitError, and NetSuiteSuiteQLOffsetLimitExceededError
        above. NetSuiteMetadataAdapterError is deliberately excluded from that
        hierarchy: metadata discovery failure may be transient (API unavailable)
        or deterministic (invalid record type), so it stays UNKNOWN for DLQ
        + manual review rather than guessing.
        """
        if isinstance(exc, (DeterministicConnectorError, TransientConnectorError)):
            return exc.classification
        if isinstance(exc, NetSuiteMetadataAdapterError):
            return ExtractionErrorClassification.UNKNOWN
        if isinstance(exc, requests.Timeout):
            return ExtractionErrorClassification.TRANSIENT_TIMEOUT
        if isinstance(exc, requests.ConnectionError):
            return ExtractionErrorClassification.TRANSIENT_NETWORK
        if isinstance(exc, OSError):
            return ExtractionErrorClassification.TRANSIENT_NETWORK
        return ExtractionErrorClassification.UNKNOWN

    # ── Private ────────────────────────────────────────────────────────────────

    def _fetch_page(
        self,
        suiteql_url: str,
        query: str,
        offset: int,
        limit: int,
    ) -> Iterator[dict[str, Any]]:
        """
        Execute a single SuiteQL page request and yield row dicts.

        Posts the query with pagination parameters; raises on HTTP errors.
        """
        headers = self._auth.get_auth_headers("POST", suiteql_url)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"

        body: dict[str, Any] = {
            "q": query,
        }

        try:
            response = requests.post(
                suiteql_url,
                headers=headers,
                json=body,
                params={"offset": offset, "limit": limit},
                timeout=60,
            )
        except requests.RequestException as exc:
            raise OSError(f"SuiteQL request failed: {type(exc).__name__}") from exc

        if response.status_code == 429:
            raise NetSuiteSuiteQLRateLimitError("NetSuite SuiteQL rate limit exceeded (HTTP 429).")

        if not response.ok:
            raise NetSuiteMetadataAdapterError(
                f"SuiteQL endpoint returned HTTP {response.status_code}."
            )

        data: dict[str, Any] = response.json()
        items: list[dict[str, Any]] = data.get("items", [])
        yield from items


# ---------------------------------------------------------------------------
# Connector builder
# ---------------------------------------------------------------------------


def _build_netsuite(
    environment: str,
    region_name: str,
    connector_params: dict[str, str],
    raw_s3_bucket: str,
    tenant_code: str,
) -> tuple[ConnectorInterface, Any]:
    """
    Factory used by the extraction pipeline Lambda to construct a fully-wired
    NetSuiteConnector and NetSuiteRawLayerWriter from the Step Functions
    execution input.

    Required connector_params key:
      record_type (str) — NetSuite record type (e.g. 'customer', 'transaction').
    """
    from connector_runtime.adapters.netsuite.netsuite_raw_layer_writer import (
        NetSuiteRawLayerWriter,
    )

    record_type = connector_params.get("record_type", "")
    if not record_type:
        raise ValueError(
            "connector_params must include 'record_type' for source_id='netsuite'. "
            "Example: {'record_type': 'customer'}."
        )
    connector = NetSuiteConnector(
        environment=environment,
        region_name=region_name,
        record_type=record_type,
    )
    writer = NetSuiteRawLayerWriter(
        s3_bucket=raw_s3_bucket,
        region_name=region_name,
        tenant_code=tenant_code,
    )
    return connector, writer


connector_registry.register_builder(_SOURCE_ID, _build_netsuite)
connector_registry.register_params_model(_SOURCE_ID, NetSuiteConnectorParams)


def _with_keyset_predicate(query: str, keyset_field: str, cursor: str | None) -> str:
    """
    Add a `> cursor` seek predicate and a deterministic order to a SuiteQL query (L17).

    The ordering is not cosmetic: keyset pagination is only correct if the rows come back in the
    column's order, and SuiteQL makes no ordering guarantee without an explicit ORDER BY.

    The cursor is embedded rather than bound because SuiteQL's REST endpoint takes one query
    string with no parameter slots. It is safe here because `keyset_field` comes from the entity's
    own validated configuration and the cursor is a value NetSuite itself returned — but the quote
    escaping below is what stops a value containing a quote from breaking the statement.
    """
    if not _SAFE_SUITEQL_IDENTIFIER.match(keyset_field):
        raise NetSuiteSuiteQLKeysetError(
            f"keyset field {keyset_field!r} is not a plain SuiteQL identifier, so it cannot be "
            "used in a seek predicate."
        )
    ordered = f"{query} ORDER BY {keyset_field} ASC"
    if cursor is None:
        return ordered
    escaped = str(cursor).replace("'", "''")
    connector = "AND" if " WHERE " in query.upper() else "WHERE"
    # Insert the predicate before the ORDER BY that was just appended.
    return f"{query} {connector} {keyset_field} > '{escaped}' ORDER BY {keyset_field} ASC"


def _next_keyset_cursor(
    page_rows: list[dict[str, Any]], keyset_field: str, current: str | None
) -> str | None:
    """The last row's keyset value, or None when it did not advance past `current`."""
    last = page_rows[-1].get(keyset_field)
    if last in (None, ""):
        return None
    candidate = str(last)
    return None if candidate == current else candidate
