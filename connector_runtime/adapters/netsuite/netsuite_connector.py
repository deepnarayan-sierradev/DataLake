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
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Final

import requests

from connector_runtime.adapters.netsuite.netsuite_auth_client import (
    NetSuiteAuthClient,
    NetSuiteAuthError,
    NetSuiteCredentialError,
)
from connector_runtime.adapters.netsuite.netsuite_incremental_query_planner import (
    NetSuiteIncrementalQueryPlanner,
    NetSuiteIncrementalQueryPlannerError,
)
from connector_runtime.adapters.netsuite.netsuite_metadata_adapter import (
    NetSuiteMetadataAdapter,
    NetSuiteMetadataAdapterError,
)
from connector_runtime.interfaces.connector_interface import (
    ConnectorCapabilities,
    ConnectorInterface,
    ExtractionErrorClassification,
    ExtractionRecord,
    FieldContract,
    QueryContract,
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

# NetSuite enforces a maximum page size of 1,000 rows per SuiteQL request.
_PAGE_SIZE: Final[int] = 1_000


class NetSuiteSuiteQLRateLimitError(Exception):
    """Raised when NetSuite returns HTTP 429 (SuiteQL rate limit exceeded)."""


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

        while True:
            page_rows = list(
                self._fetch_page(
                    suiteql_url=suiteql_url,
                    query=bound_query,
                    offset=offset,
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
        """
        if isinstance(exc, NetSuiteCredentialError):
            return ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS
        if isinstance(exc, NetSuiteAuthError):
            return ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS
        if isinstance(exc, NetSuiteIncrementalQueryPlannerError):
            return ExtractionErrorClassification.DETERMINISTIC_INVALID_CONFIGURATION
        if isinstance(exc, NetSuiteSuiteQLRateLimitError):
            return ExtractionErrorClassification.TRANSIENT_THROTTLE
        if isinstance(exc, NetSuiteMetadataAdapterError):
            # Metadata discovery failure may be transient (e.g. API unavailable)
            # or deterministic (invalid record type).  Default to UNKNOWN to
            # route to DLQ for manual review.
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
        s3_prefix="netsuite",
        region_name=region_name,
    )
    return connector, writer


connector_registry.register_builder(_SOURCE_ID, _build_netsuite)
