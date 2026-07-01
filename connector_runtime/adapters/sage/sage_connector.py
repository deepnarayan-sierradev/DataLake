"""
SageConnector — generic Sage ERP connector adapter for the Enterprise Data Lake platform.

Registered with the platform ConnectorRegistry as source_id="sage".
A single implementation handles ALL Sage products (Intacct, X3, Sage 100, etc.)
through a Strategy pattern: product-specific auth, query, and metadata behaviour
are provided by pluggable strategy objects resolved from SageProductRegistry.

connector_params schema (supplied in Step Functions execution input):
    {
        "sage_product":  str  — Sage product name, validated against whitelist
                                Accepted values: "intacct" (and future products)
        "object_path":   str  — Sage object identifier
                                e.g. "accounts-receivable/customer"
        "entity_label":  str  — Optional human-readable label for log events only
    }

Credentials are NOT in connector_params.  They live in Secrets Manager at:
    {environment}/sources/sage/{sage_product}/credentials

Design principles enforced:
    1. No hardcoded field lists — fields discovered at runtime via metadata strategy.
    2. No credentials in constructor args — SageCredentialManager fetches from Secrets Manager.
    3. Error taxonomy — every exception classified as TRANSIENT_* or DETERMINISTIC_*.
    4. Idempotent extraction — safe to replay without duplicates.
    5. Zero impact on existing connectors — only adds new registry entries.

Security (OWASP A03, A07, A09):
    - sage_product validated against SUPPORTED_SAGE_PRODUCTS whitelist before use
      as a registry key — no arbitrary string can route to arbitrary code.
    - object_path validated by both query engine and metadata client before use.
    - No credential values in any log event or exception message.
    - TLS enforced by SageHttpClient (verify=True always).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, Final, Literal

from connector_runtime.adapters.sage.common.sage_credential_manager import (
    SageCredentialError,
    SageCredentialManager,
)
from connector_runtime.adapters.sage.common.sage_http_client import (
    SageAuthenticationError,
    SageHttpClient,
    SageInvalidRequestError,
    SageNetworkError,
    SageObjectNotFoundError,
    SageRateLimitError,
    SageServiceUnavailableError,
    SageTimeoutError,
)
from connector_runtime.adapters.sage.common.sage_product_registry import (
    SUPPORTED_SAGE_PRODUCTS,
    resolve_product_strategies,
)
from connector_runtime.adapters.sage.common.sage_raw_layer_writer import SageRawLayerWriter
from connector_runtime.adapters.sage.products.intacct.intacct_auth import (
    IntacctAuthError,
    IntacctCredentialError,
)
from connector_runtime.adapters.sage.common.sage_errors import (
    SageMetadataDeterministicError,
    SageMetadataError,
    SageMetadataTransientError,
    SageQueryBuildError,
)
from connector_runtime.adapters.sage.products.intacct.intacct_query_engine import (
    PAGE_SIZE,
    IntacctQueryEngine,
)
from connector_runtime.adapters.sage.products.x3.x3_auth import (
    X3AuthError,
    X3CredentialError,
)
from connector_runtime.adapters.sage.products.x3.x3_query_engine import (
    X3_PAGE_SIZE,
    X3QueryBuildError,
    X3QueryEngine,
    X3_ODATA_DISCRIMINANT,
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

_SOURCE_ID: Final[str] = "sage"

# Required keys in connector_params (validated in __init__).
_REQUIRED_CONNECTOR_PARAMS: Final[frozenset[str]] = frozenset({"sage_product", "object_path"})

# Required credential keys shared by all Sage products (validated by SageCredentialManager).
# Product-specific required keys are declared in each auth client module.
_INTACCT_REQUIRED_KEYS: Final[frozenset[str]] = frozenset(
    {"base_url", "token_url", "client_id", "client_secret", "company_id"}
)

# Mapping of sage_product → required credential keys.
# Extend this when new products are added.
_PRODUCT_REQUIRED_CREDENTIAL_KEYS: Final[dict[str, frozenset[str]]] = {
    "intacct": _INTACCT_REQUIRED_KEYS,
    "x3": frozenset(
        {"base_url", "token_url", "client_id", "client_secret", "folder"}
    ),
}

# Intacct query service endpoint path (relative to base_url).
_INTACCT_QUERY_PATH: Final[str] = "/services/v1/query"


@connector_registry.register(_SOURCE_ID)
class SageConnector(ConnectorInterface):
    """
    Metadata-driven Sage ERP connector handling all Sage products through
    product-specific strategy objects.

    One instance per extraction run.  The sage_product and object_path are
    provided as connector_params (not constructor arguments), so the same
    class serves any Sage product without subclassing.

    Constructor args are NOT used for credentials — those come exclusively from
    AWS Secrets Manager via SageCredentialManager.
    """

    def __init__(
        self,
        environment: str,
        region_name: str,
        sage_product: str,
        object_path: str,
    ) -> None:
        # ── Validate sage_product against whitelist FIRST (OWASP A03) ─────────
        if sage_product not in SUPPORTED_SAGE_PRODUCTS:
            raise ValueError(
                f"Unsupported sage_product {sage_product!r}. "
                f"Supported products: {sorted(SUPPORTED_SAGE_PRODUCTS)}. "
                "Add new products by implementing the three protocol interfaces "
                "and registering them in sage_product_registry."
            )

        self._sage_product = sage_product
        self._object_path = object_path

        # ── Resolve product-specific strategy classes ─────────────────────────
        strategies = resolve_product_strategies(sage_product)

        # ── Instantiate shared infrastructure (one per extraction run) ────────
        required_keys = _PRODUCT_REQUIRED_CREDENTIAL_KEYS.get(sage_product, frozenset())
        self._credential_manager = SageCredentialManager(
            environment=environment,
            region_name=region_name,
            product_name=sage_product,
            required_keys=required_keys,
        )
        self._http_client = SageHttpClient()

        # ── Instantiate product strategies (constructor-injected deps) ────────
        self._auth = strategies.auth_class(
            credential_manager=self._credential_manager,
            http_client=self._http_client,
        )
        self._metadata_client = strategies.metadata_client_class(
            auth_client=self._auth,
            http_client=self._http_client,
            object_path=object_path,
        )
        self._query_engine = strategies.query_engine_class(object_path=object_path)

    # ── ConnectorInterface implementation ─────────────────────────────────────

    def get_capability_declaration(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            source_id=_SOURCE_ID,
            supports_bulk_extraction=False,
            supports_incremental=True,
            supports_full_load=True,
            supports_metadata_discovery=getattr(
                self._metadata_client, "supports_live_discovery", True
            ),
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
        Discover all queryable fields for this Sage object via the product
        metadata strategy (e.g. Intacct Models endpoint).

        New fields added to the Sage object appear automatically in the next
        run without any code changes — metadata-driven discovery at runtime.
        """
        return self._metadata_client.discover_fields(
            source_id=source_id,
            entity_id=entity_id,
            object_path=self._object_path,
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
        Build a parameterised query from the discovered FieldContract using
        the product-specific query engine strategy.

        Watermark values are stored in QueryContract.query_parameters — they
        are NEVER interpolated into query_text at this stage.
        """
        return self._query_engine.build(
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
        Execute paginated extraction against the Sage REST API and yield records.

        Dispatches to the product-specific private execution method based on the
        discriminant key in query_text:
          - Sage X3 (OData v4 GET):   _execute_x3_extraction()
          - Sage Intacct (JSON POST):  _execute_intacct_extraction()

        Watermark placeholder values are bound from query_parameters immediately
        before sending — ISO-8601 validation enforced by each product's
        query engine bind_parameters().

        Sets source_timestamp from the watermark field value on each record when
        the watermark field is present in the record payload.
        """
        # Deserialise the query template to inspect the product discriminant.
        query_body: dict[str, Any] = json.loads(query_contract.query_text)

        if query_body.get(X3_ODATA_DISCRIMINANT):
            # ── Sage X3 OData GET-based extraction path ──────────────────────
            yield from self._execute_x3_extraction(query_contract, query_body, run_id)
        else:
            # ── Sage Intacct JSON-POST extraction path (default) ─────────────
            yield from self._execute_intacct_extraction(query_contract, query_body, run_id)

    def classify_extraction_error(self, exc: Exception) -> ExtractionErrorClassification:
        """
        Classify a Sage extraction exception for the platform retry framework.

        TRANSIENT_* errors are retry-eligible (Step Functions retries).
        DETERMINISTIC_* errors trigger immediate fail-fast with DLQ routing.
        """
        # ── Credential / auth failures — deterministic ─────────────────────
        if isinstance(exc, (IntacctCredentialError, X3CredentialError, SageCredentialError)):
            return ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS
        if isinstance(exc, (IntacctAuthError, X3AuthError)):
            return ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS
        if isinstance(exc, SageAuthenticationError):
            return ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS

        # ── Configuration / query failures — deterministic ─────────────────
        if isinstance(exc, SageObjectNotFoundError):
            return ExtractionErrorClassification.DETERMINISTIC_INVALID_OBJECT
        if isinstance(exc, (SageQueryBuildError, X3QueryBuildError)):
            return ExtractionErrorClassification.DETERMINISTIC_INVALID_CONFIGURATION
        if isinstance(exc, SageMetadataDeterministicError):
            return ExtractionErrorClassification.DETERMINISTIC_INVALID_OBJECT
        if isinstance(exc, SageMetadataTransientError):
            return ExtractionErrorClassification.TRANSIENT_NETWORK
        if isinstance(exc, SageMetadataError):
            # Generic base — unknown subtype; route to UNKNOWN for DLQ + manual review.
            return ExtractionErrorClassification.UNKNOWN
        if isinstance(exc, SageInvalidRequestError):
            return ExtractionErrorClassification.DETERMINISTIC_INVALID_CONFIGURATION

        # ── Transient infrastructure failures — retry eligible ─────────────
        if isinstance(exc, SageRateLimitError):
            return ExtractionErrorClassification.TRANSIENT_THROTTLE
        if isinstance(exc, SageServiceUnavailableError):
            return ExtractionErrorClassification.TRANSIENT_NETWORK
        if isinstance(exc, SageTimeoutError):
            return ExtractionErrorClassification.TRANSIENT_TIMEOUT
        if isinstance(exc, SageNetworkError):
            return ExtractionErrorClassification.TRANSIENT_NETWORK

        return ExtractionErrorClassification.UNKNOWN

    # ── Private ────────────────────────────────────────────────────────────────

    def _execute_intacct_extraction(
        self,
        query_contract: QueryContract,
        query_body: dict[str, Any],
        run_id: str,
    ) -> Iterator[ExtractionRecord]:
        """
        Execute paginated extraction against Sage Intacct using the JSON-POST
        query service.

        Pagination: Intacct start/size cursor (ia::meta.next).
        """
        _logger.info(
            "sage_extraction_started",
            source_id=query_contract.source_id,
            entity_id=query_contract.entity_id,
            run_id=run_id,
            sage_product=self._sage_product,
            object_path=self._object_path,
            load_type=str(query_contract.load_type),
        )

        if query_contract.query_parameters:
            query_body = IntacctQueryEngine.bind_parameters(
                query_body, query_contract.query_parameters
            )

        query_url = f"{self._auth.base_url}{_INTACCT_QUERY_PATH}"
        record_count = 0
        start = 1

        while True:
            page_body = dict(query_body)
            page_body["start"] = start
            page_body["size"] = PAGE_SIZE

            page_response = self._fetch_page(
                query_url=query_url,
                query_body=page_body,
                run_id=run_id,
                entity_id=query_contract.entity_id,
                page_start=start,
            )

            results: list[dict[str, Any]] = page_response.get("ia::result", [])
            meta: dict[str, Any] = page_response.get("ia::meta", {})

            for row in results:
                record_count += 1
                rec = ExtractionRecord(payload=row)
                if (
                    query_contract.watermark_field
                    and query_contract.watermark_field in row
                ):
                    rec.source_timestamp = str(row[query_contract.watermark_field])
                yield rec

            _logger.info(
                "sage_page_fetched",
                sage_product=self._sage_product,
                entity_id=query_contract.entity_id,
                run_id=run_id,
                page_start=start,
                records_in_page=len(results),
                total_so_far=record_count,
            )

            next_start = meta.get("next")
            if not next_start:
                break
            start = int(next_start)

        _logger.info(
            "sage_extraction_completed",
            source_id=query_contract.source_id,
            entity_id=query_contract.entity_id,
            run_id=run_id,
            sage_product=self._sage_product,
            record_count=record_count,
        )

    def _execute_x3_extraction(
        self,
        query_contract: QueryContract,
        query_body: dict[str, Any],
        run_id: str,
    ) -> Iterator[ExtractionRecord]:
        """
        Execute paginated extraction against Sage X3 using OData v4 GET requests.

        Pagination strategy:
          - Primary: follow @odata.nextLink from the response when present.
          - Fallback: $top/$skip offset pagination when nextLink is absent.

        The initial OData parameters ($select, $filter, $orderby) are built by
        X3QueryEngine and stored in query_body.  Watermark placeholders are
        substituted here via X3QueryEngine.bind_parameters() before the first
        page is fetched.

        The endpoint URL is constructed as:
            {auth.base_url}/{endpoint}?$select=...&$filter=...&$orderby=...&$top=N&$skip=N
        """

        _logger.info(
            "sage_extraction_started",
            source_id=query_contract.source_id,
            entity_id=query_contract.entity_id,
            run_id=run_id,
            sage_product=self._sage_product,
            object_path=self._object_path,
            load_type=str(query_contract.load_type),
        )

        # Bind watermark parameter placeholders (ISO-8601 validation enforced).
        if query_contract.query_parameters:
            query_body = X3QueryEngine.bind_parameters(
                query_body, query_contract.query_parameters
            )

        endpoint: str = query_body["endpoint"]
        select_str: str = query_body["select"]
        filter_str: str | None = query_body.get("filter")
        orderby_str: str = query_body["orderby"]
        base_endpoint_url = f"{self._auth.base_url}/{endpoint}"

        # OData query parameters for the initial page.
        odata_params: dict[str, str] = {
            "$select": select_str,
            "$orderby": orderby_str,
            "$top": str(X3_PAGE_SIZE),
        }
        if filter_str:
            odata_params["$filter"] = filter_str

        record_count = 0
        skip = 0
        next_link: str | None = None  # Cursor URL from @odata.nextLink

        while True:
            if next_link:
                # Follow the full nextLink URL — server provides all params.
                page_response = self._fetch_page(
                    query_url=next_link,
                    query_body=None,  # GET request — body is None
                    run_id=run_id,
                    entity_id=query_contract.entity_id,
                    page_start=skip,
                    http_method="GET",
                )
            else:
                page_params = dict(odata_params)
                if skip > 0:
                    page_params["$skip"] = str(skip)
                page_response = self._fetch_page(
                    query_url=base_endpoint_url,
                    query_body=None,
                    run_id=run_id,
                    entity_id=query_contract.entity_id,
                    page_start=skip,
                    http_method="GET",
                    params=page_params,
                )

            records: list[dict[str, Any]] = page_response.get("value", [])
            next_link = page_response.get("@odata.nextLink")

            for row in records:
                record_count += 1
                rec = ExtractionRecord(payload=row)
                if (
                    query_contract.watermark_field
                    and query_contract.watermark_field in row
                ):
                    rec.source_timestamp = str(row[query_contract.watermark_field])
                yield rec

            _logger.info(
                "sage_page_fetched",
                sage_product=self._sage_product,
                entity_id=query_contract.entity_id,
                run_id=run_id,
                page_start=skip,
                records_in_page=len(records),
                total_so_far=record_count,
            )

            if not records:
                # Empty page — extraction complete regardless of pagination mode.
                break
            if next_link:
                # Server has more pages — follow nextLink on the next iteration.
                # Do NOT break on partial page here: the last nextLink page may
                # legitimately contain fewer than X3_PAGE_SIZE records.
                skip = 0  # skip is irrelevant when following nextLink
            else:
                if len(records) < X3_PAGE_SIZE:
                    # Partial page with no nextLink — last page of skip pagination.
                    break
                skip += X3_PAGE_SIZE

        _logger.info(
            "sage_extraction_completed",
            source_id=query_contract.source_id,
            entity_id=query_contract.entity_id,
            run_id=run_id,
            sage_product=self._sage_product,
            record_count=record_count,
        )

    def _fetch_page(
        self,
        query_url: str,
        query_body: dict[str, Any] | None,
        run_id: str,
        entity_id: str,
        page_start: int,
        http_method: Literal["GET", "POST"] = "POST",
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Execute a single Sage query page request and return the parsed response.

        Supports both POST (Intacct JSON query service) and GET (X3 OData v4).

        Handles HTTP 401 with a single token invalidation + retry — this recovers
        from mid-run token expiry without aborting the extraction.

        Raises typed SageHttpError subclasses on unrecoverable errors.
        """
        headers = self._auth.build_auth_headers()
        try:
            if http_method == "GET":
                return self._http_client.get(
                    url=query_url,
                    headers=headers,
                    params=params,
                )
            return self._http_client.post(
                url=query_url,
                headers=headers,
                json_body=query_body,
            )
        except SageAuthenticationError:
            # Token may have just expired — invalidate and retry once.
            _logger.info(
                "sage_token_expired_mid_extraction_retry",
                sage_product=self._sage_product,
                entity_id=entity_id,
                run_id=run_id,
                page_start=page_start,
            )
            self._auth.invalidate_token()
            refreshed_headers = self._auth.build_auth_headers()
            if http_method == "GET":
                return self._http_client.get(
                    url=query_url,
                    headers=refreshed_headers,
                    params=params,
                )
            return self._http_client.post(
                url=query_url,
                headers=refreshed_headers,
                json_body=query_body,
            )


# ---------------------------------------------------------------------------
# Builder function — wires connector + writer, registered with ConnectorRegistry
# ---------------------------------------------------------------------------


def _build_sage(
    environment: str,
    region_name: str,
    connector_params: dict[str, str],
    raw_s3_bucket: str,
) -> tuple[SageConnector, SageRawLayerWriter]:
    """
    Factory function registered with ConnectorRegistry for source_id="sage".

    Validates connector_params, instantiates SageConnector and SageRawLayerWriter
    with shared configuration.  Called once per extraction run by the platform's
    ExtractionWorkflow.

    Args:
        environment:      Deployment environment (dev/staging/prod).
        region_name:      AWS region name.
        connector_params: Step Functions input dict — must contain sage_product
                          and object_path.
        raw_s3_bucket:    Name of the raw layer S3 bucket.

    Returns:
        Tuple of (SageConnector, SageRawLayerWriter).

    Raises:
        ValueError: if required connector_params keys are missing or sage_product
                    is not in the supported products whitelist.
    """
    missing = _REQUIRED_CONNECTOR_PARAMS - connector_params.keys()
    if missing:
        raise ValueError(
            f"connector_params is missing required keys for source_id='sage': "
            f"{sorted(missing)}. "
            f"Required: {sorted(_REQUIRED_CONNECTOR_PARAMS)}."
        )

    sage_product = connector_params["sage_product"]
    object_path = connector_params["object_path"]

    if sage_product not in SUPPORTED_SAGE_PRODUCTS:
        raise ValueError(
            f"connector_params.sage_product={sage_product!r} is not supported. "
            f"Supported values: {sorted(SUPPORTED_SAGE_PRODUCTS)}."
        )

    connector = SageConnector(
        environment=environment,
        region_name=region_name,
        sage_product=sage_product,
        object_path=object_path,
    )
    writer = SageRawLayerWriter(
        s3_bucket=raw_s3_bucket,
        s3_prefix="sage",
        sage_product=sage_product,
        region_name=region_name,
    )
    return connector, writer


connector_registry.register_builder(_SOURCE_ID, _build_sage)
