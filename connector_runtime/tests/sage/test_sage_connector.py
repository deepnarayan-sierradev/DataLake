"""
Tests for SageConnector — integration and unit coverage.

Coverage:
  - SageConnector registered as "sage" in ConnectorRegistry
  - Builder "_build_sage" registered in ConnectorRegistry
  - Constructor rejects unsupported sage_product → ValueError
  - get_capability_declaration() returns correct values for all fields
  - discover_queryable_fields() delegates to the metadata strategy
  - build_extraction_query() delegates to the query engine strategy
  - execute_extraction() single-page result → yields all records (Intacct)
  - execute_extraction() multi-page result (ia::meta.next cursor) (Intacct)
  - execute_extraction() stops when ia::meta.next is null (Intacct)
  - execute_extraction() sets source_timestamp from watermark field
  - execute_extraction() source_timestamp not set when field absent
  - execute_extraction() empty results page → iterator ends cleanly
  - execute_extraction() 401 mid-run → invalidate_token + single retry
  - execute_extraction() second 401 after retry → propagates SageAuthenticationError
  - execute_extraction() dispatches to X3 OData path on discriminant key
  - execute_extraction() X3 single-page no nextLink
  - execute_extraction() X3 multi-page skip-based pagination
  - execute_extraction() X3 follows @odata.nextLink when present
  - execute_extraction() X3 partial page with nextLink does NOT stop early (regression)
  - execute_extraction() X3 empty page stops iteration
  - classify_extraction_error() all typed exception → correct classification
  - classify_extraction_error() IntacctAuthError → DETERMINISTIC_INVALID_CREDENTIALS
  - classify_extraction_error() IntacctCredentialError → DETERMINISTIC_INVALID_CREDENTIALS
  - classify_extraction_error() X3AuthError → DETERMINISTIC_INVALID_CREDENTIALS
  - classify_extraction_error() X3CredentialError → DETERMINISTIC_INVALID_CREDENTIALS
  - classify_extraction_error() X3QueryBuildError → DETERMINISTIC_INVALID_CONFIGURATION
  - classify_extraction_error() SageMetadataError → UNKNOWN
  - classify_extraction_error() SageInvalidRequestError → DETERMINISTIC_INVALID_CONFIGURATION
  - classify_extraction_error() unknown exception → UNKNOWN
  - _build_sage() factory: creates SageConnector + SageRawLayerWriter
  - _build_sage() missing connector_params keys → ValueError
  - _build_sage() invalid sage_product → ValueError
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import MagicMock, call, patch

import pytest

from connector_runtime.adapters.sage.common.sage_http_client import (
    SageAuthenticationError,
    SageNetworkError,
    SageObjectNotFoundError,
    SageRateLimitError,
    SageServiceUnavailableError,
    SageTimeoutError,
    SageInvalidRequestError,
)
from connector_runtime.adapters.sage.common.sage_raw_layer_writer import SageRawLayerWriter
from connector_runtime.adapters.sage.products.intacct.intacct_auth import (
    IntacctAuthError,
    IntacctCredentialError,
)
from connector_runtime.adapters.sage.products.intacct.intacct_metadata_client import SageMetadataError
from connector_runtime.adapters.sage.products.intacct.intacct_query_engine import (
    PAGE_SIZE,
    SageQueryBuildError,
)
from connector_runtime.adapters.sage.products.x3.x3_auth import X3AuthError, X3CredentialError
from connector_runtime.adapters.sage.products.x3.x3_query_engine import (
    X3_PAGE_SIZE,
    X3QueryBuildError,
    X3_ODATA_DISCRIMINANT,
)
from connector_runtime.adapters.sage.sage_connector import (
    SageConnector,
    _build_sage,
)
from connector_runtime.interfaces.connector_interface import (
    ExtractionErrorClassification,
    FieldContract,
    FieldDescriptor,
    QueryContract,
)
from connector_runtime.registry import connector_registry
from contracts.entity_configuration_contract import FieldMode, LoadType

_ENV = "dev"
_REGION = "us-east-1"
_PRODUCT = "intacct"
_OBJECT_PATH = "accounts-receivable/customer"
_BASE_URL = "https://api.intacct.com/ia/api/v1"
_QUERY_URL = f"{_BASE_URL}/services/v1/query"
_ENTITY_ID = "sage-intacct-customer"
_RUN_ID = "run-20260701-120000000000-ab12cd34"
_LOWER = "2026-01-01T00:00:00Z"
_UPPER = "2026-07-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_connector() -> SageConnector:
    """
    Build a SageConnector with fully mocked internals — no AWS or HTTP calls.
    Follows the same pattern as test_netsuite_connector._make_connector().
    """
    connector = SageConnector.__new__(SageConnector)
    connector._sage_product = _PRODUCT  # type: ignore[attr-defined]
    connector._object_path = _OBJECT_PATH  # type: ignore[attr-defined]

    mock_auth = MagicMock()
    mock_auth.base_url = _BASE_URL
    mock_auth.get_access_token.return_value = "test-bearer-token"
    mock_auth.build_auth_headers.return_value = {"Authorization": "Bearer test-bearer-token"}
    connector._auth = mock_auth  # type: ignore[attr-defined]

    connector._http_client = MagicMock()
    connector._metadata_client = MagicMock()
    connector._query_engine = MagicMock()
    connector._credential_manager = MagicMock()
    return connector


def _make_query_contract(
    load_type: LoadType = LoadType.FULL,
    watermark_field: str | None = None,
    query_parameters: dict | None = None,
) -> QueryContract:
    body = {
        "object": _OBJECT_PATH,
        "fields": ["key", "id", "name"],
        "orderBy": [{"key": "asc"}],
    }
    return QueryContract(
        source_id="sage",
        entity_id=_ENTITY_ID,
        query_text=json.dumps(body),
        query_parameters=query_parameters or {},
        load_type=load_type,
        watermark_lower=_LOWER if load_type == LoadType.INCREMENTAL else None,
        watermark_upper=_UPPER if load_type == LoadType.INCREMENTAL else None,
        watermark_field=watermark_field,
    )


def _make_field_contract(field_names: list[str] | None = None) -> FieldContract:
    names = field_names or ["key", "id", "name"]
    descriptors = tuple(
        FieldDescriptor(name=n, data_type="string", is_nullable=True, is_queryable=True)
        for n in names
    )
    return FieldContract(
        source_id="sage",
        entity_id=_ENTITY_ID,
        fields=descriptors,
        discovery_timestamp=datetime.now(UTC),
        schema_fingerprint=FieldContract.compute_fingerprint(descriptors),
    )


def _page_response(
    rows: list[dict],
    next_start: int | None = None,
) -> dict:
    """Build an Intacct-format page response."""
    return {
        "ia::result": rows,
        "ia::meta": {
            "totalCount": 100,
            "start": 1,
            "pageSize": PAGE_SIZE,
            "next": next_start,
        },
    }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_sage_registered_in_connector_registry(self) -> None:
        # Import ensures the module is loaded and registration happened.
        import connector_runtime.adapters.sage.sage_connector  # noqa: F401
        assert "sage" in connector_registry.registered_source_ids

    def test_builder_registered_for_sage(self) -> None:
        assert "sage" in connector_registry._builders

    def test_registered_class_is_sage_connector(self) -> None:
        assert connector_registry._registry["sage"] is SageConnector


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    def test_unsupported_sage_product_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unsupported sage_product"):
            SageConnector(
                environment=_ENV,
                region_name=_REGION,
                sage_product="hacked_product",
                object_path=_OBJECT_PATH,
            )

    def test_injection_attempt_in_sage_product_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unsupported sage_product"):
            SageConnector(
                environment=_ENV,
                region_name=_REGION,
                sage_product="'; DROP TABLE users; --",
                object_path=_OBJECT_PATH,
            )

    def test_valid_sage_product_accepted(self) -> None:
        """Valid product should instantiate without reaching AWS (mocked)."""
        with (
            patch("connector_runtime.adapters.sage.sage_connector.SageCredentialManager"),
            patch("connector_runtime.adapters.sage.sage_connector.SageHttpClient"),
        ):
            connector = SageConnector(
                environment=_ENV,
                region_name=_REGION,
                sage_product="intacct",
                object_path=_OBJECT_PATH,
            )
        assert connector._sage_product == "intacct"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


class TestCapabilityDeclaration:
    def test_source_id_is_sage(self) -> None:
        connector = _make_connector()
        caps = connector.get_capability_declaration()
        assert caps.source_id == "sage"

    def test_supports_incremental_and_full(self) -> None:
        connector = _make_connector()
        caps = connector.get_capability_declaration()
        assert caps.supports_incremental is True
        assert caps.supports_full_load is True

    def test_bulk_extraction_not_supported(self) -> None:
        connector = _make_connector()
        caps = connector.get_capability_declaration()
        assert caps.supports_bulk_extraction is False

    def test_supports_metadata_discovery(self) -> None:
        connector = _make_connector()
        connector._metadata_client.supports_live_discovery = True  # type: ignore[attr-defined]
        caps = connector.get_capability_declaration()
        assert caps.supports_metadata_discovery is True

    def test_all_field_modes_supported(self) -> None:
        connector = _make_connector()
        caps = connector.get_capability_declaration()
        assert FieldMode.ALL in caps.supported_field_modes
        assert FieldMode.STANDARD in caps.supported_field_modes
        assert FieldMode.CUSTOM in caps.supported_field_modes
        assert FieldMode.INCLUDE_ONLY in caps.supported_field_modes

    def test_max_concurrent_jobs_is_one(self) -> None:
        connector = _make_connector()
        assert connector.get_capability_declaration().max_concurrent_jobs == 1


# ---------------------------------------------------------------------------
# discover_queryable_fields
# ---------------------------------------------------------------------------


class TestDiscoverQueryableFields:
    def test_delegates_to_metadata_client(self) -> None:
        connector = _make_connector()
        expected_fc = _make_field_contract()
        connector._metadata_client.discover_fields.return_value = expected_fc  # type: ignore[attr-defined]

        result = connector.discover_queryable_fields(
            source_id="sage",
            entity_id=_ENTITY_ID,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        assert result is expected_fc
        connector._metadata_client.discover_fields.assert_called_once_with(  # type: ignore[attr-defined]
            source_id="sage",
            entity_id=_ENTITY_ID,
            object_path=_OBJECT_PATH,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )


# ---------------------------------------------------------------------------
# build_extraction_query
# ---------------------------------------------------------------------------


class TestBuildExtractionQuery:
    def test_delegates_to_query_engine(self) -> None:
        connector = _make_connector()
        expected_qc = _make_query_contract()
        connector._query_engine.build.return_value = expected_qc  # type: ignore[attr-defined]
        fc = _make_field_contract()

        result = connector.build_extraction_query(
            field_contract=fc,
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        assert result is expected_qc
        connector._query_engine.build.assert_called_once()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# execute_extraction
# ---------------------------------------------------------------------------


class TestExecuteExtraction:
    """
    execute_extraction tests mock _http_client.post directly (not requests_mock)
    because _make_connector() replaces _http_client with a MagicMock.
    Using requests_mock here would intercept real HTTP calls, but the mock
    client never makes real HTTP calls.
    """

    def test_single_page_yields_all_records(self) -> None:
        connector = _make_connector()
        rows = [{"key": str(i), "id": f"C{i:03d}", "name": f"Corp {i}"} for i in range(5)]
        connector._http_client.post.return_value = _page_response(rows, next_start=None)  # type: ignore[attr-defined]

        qc = _make_query_contract()
        records = list(connector.execute_extraction(qc, run_id=_RUN_ID))
        assert len(records) == 5
        assert records[0].payload["key"] == "0"

    def test_multi_page_yields_all_records(self) -> None:
        """ia::meta.next drives the cursor to the next page."""
        connector = _make_connector()
        page1 = [{"key": str(i)} for i in range(4)]
        page2 = [{"key": str(i + 4)} for i in range(3)]
        connector._http_client.post.side_effect = [  # type: ignore[attr-defined]
            _page_response(page1, next_start=5),
            _page_response(page2, next_start=None),
        ]

        qc = _make_query_contract()
        records = list(connector.execute_extraction(qc, run_id=_RUN_ID))
        assert len(records) == 7

    def test_empty_results_page_stops_iteration(self) -> None:
        connector = _make_connector()
        connector._http_client.post.return_value = _page_response([], next_start=None)  # type: ignore[attr-defined]
        qc = _make_query_contract()
        records = list(connector.execute_extraction(qc, run_id=_RUN_ID))
        assert records == []

    def test_source_timestamp_set_from_watermark_field(self) -> None:
        connector = _make_connector()
        rows = [{"key": "1", "auditInfo.modifiedAt": "2026-06-10T12:00:00Z"}]
        connector._http_client.post.return_value = _page_response(rows, next_start=None)  # type: ignore[attr-defined]

        qc = _make_query_contract(
            load_type=LoadType.INCREMENTAL,
            watermark_field="auditInfo.modifiedAt",
        )
        records = list(connector.execute_extraction(qc, run_id=_RUN_ID))
        assert records[0].source_timestamp == "2026-06-10T12:00:00Z"

    def test_source_timestamp_not_set_when_field_absent(self) -> None:
        connector = _make_connector()
        rows = [{"key": "1", "name": "Corp"}]  # watermark field not in payload
        connector._http_client.post.return_value = _page_response(rows, next_start=None)  # type: ignore[attr-defined]

        qc = _make_query_contract(
            load_type=LoadType.INCREMENTAL,
            watermark_field="auditInfo.modifiedAt",
        )
        records = list(connector.execute_extraction(qc, run_id=_RUN_ID))
        assert records[0].source_timestamp is None

    def test_401_mid_extraction_triggers_token_invalidation_and_retry(self) -> None:
        """HTTP 401 mid-run: invalidate token, refresh, retry once — succeeds."""
        connector = _make_connector()
        rows = [{"key": "1"}]
        # First call → SageAuthenticationError (401); second call → success.
        connector._http_client.post.side_effect = [  # type: ignore[attr-defined]
            SageAuthenticationError("401"),
            _page_response(rows, next_start=None),
        ]

        qc = _make_query_contract()
        records = list(connector.execute_extraction(qc, run_id=_RUN_ID))
        assert len(records) == 1
        # Auth token should have been invalidated once.
        connector._auth.invalidate_token.assert_called_once()  # type: ignore[attr-defined]

    def test_401_on_retry_propagates_exception(self) -> None:
        """HTTP 401 on both attempts must propagate as SageAuthenticationError."""
        connector = _make_connector()
        connector._http_client.post.side_effect = SageAuthenticationError("401")  # type: ignore[attr-defined]

        qc = _make_query_contract()
        with pytest.raises(SageAuthenticationError):
            list(connector.execute_extraction(qc, run_id=_RUN_ID))

    def test_query_parameters_bound_before_sending(self) -> None:
        """Watermark placeholders in query_text must be substituted before the POST."""
        import dataclasses

        connector = _make_connector()
        captured_calls: list[dict] = []

        def capture_post(url: str, headers: dict, json_body: dict) -> dict:
            captured_calls.append(json_body)
            return _page_response([], next_start=None)

        connector._http_client.post.side_effect = capture_post  # type: ignore[attr-defined]

        body_with_placeholders = {
            "object": _OBJECT_PATH,
            "fields": ["key"],
            "filters": [
                {"$gte": {"auditInfo.modifiedAt": "__SAGE_LOWER_BOUND__"}},
                {"$lt": {"auditInfo.modifiedAt": "__SAGE_UPPER_BOUND__"}},
            ],
            "orderBy": [{"key": "asc"}],
        }
        qc = _make_query_contract(
            load_type=LoadType.INCREMENTAL,
            watermark_field="auditInfo.modifiedAt",
            query_parameters={"lower_bound": _LOWER, "upper_bound": _UPPER},
        )
        qc_with_placeholders = dataclasses.replace(
            qc, query_text=json.dumps(body_with_placeholders)
        )
        list(connector.execute_extraction(qc_with_placeholders, run_id=_RUN_ID))

        assert len(captured_calls) == 1
        sent = captured_calls[0]
        # Placeholders should have been substituted.
        assert sent["filters"][0]["$gte"]["auditInfo.modifiedAt"] == _LOWER
        assert sent["filters"][1]["$lt"]["auditInfo.modifiedAt"] == _UPPER


# ---------------------------------------------------------------------------
# classify_extraction_error
# ---------------------------------------------------------------------------


class TestClassifyExtractionError:
    # Transient errors — retry eligible
    def test_rate_limit_is_transient_throttle(self) -> None:
        c = _make_connector()
        assert c.classify_extraction_error(SageRateLimitError("429")) == \
            ExtractionErrorClassification.TRANSIENT_THROTTLE

    def test_timeout_is_transient_timeout(self) -> None:
        c = _make_connector()
        assert c.classify_extraction_error(SageTimeoutError("timeout")) == \
            ExtractionErrorClassification.TRANSIENT_TIMEOUT

    def test_network_error_is_transient_network(self) -> None:
        c = _make_connector()
        assert c.classify_extraction_error(SageNetworkError("net")) == \
            ExtractionErrorClassification.TRANSIENT_NETWORK

    def test_service_unavailable_is_transient_network(self) -> None:
        c = _make_connector()
        assert c.classify_extraction_error(SageServiceUnavailableError("503")) == \
            ExtractionErrorClassification.TRANSIENT_NETWORK

    # Deterministic errors — fail-fast
    def test_auth_error_is_deterministic_invalid_credentials(self) -> None:
        c = _make_connector()
        assert c.classify_extraction_error(SageAuthenticationError("401")) == \
            ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS

    def test_intacct_auth_error_is_deterministic_invalid_credentials(self) -> None:
        c = _make_connector()
        assert c.classify_extraction_error(IntacctAuthError("rejected")) == \
            ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS

    def test_intacct_credential_error_is_deterministic_invalid_credentials(self) -> None:
        c = _make_connector()
        assert c.classify_extraction_error(IntacctCredentialError("no secret")) == \
            ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS

    def test_object_not_found_is_deterministic_invalid_object(self) -> None:
        c = _make_connector()
        assert c.classify_extraction_error(SageObjectNotFoundError("404")) == \
            ExtractionErrorClassification.DETERMINISTIC_INVALID_OBJECT

    def test_query_build_error_is_deterministic_invalid_configuration(self) -> None:
        c = _make_connector()
        assert c.classify_extraction_error(SageQueryBuildError("bad field")) == \
            ExtractionErrorClassification.DETERMINISTIC_INVALID_CONFIGURATION

    def test_invalid_request_is_deterministic_invalid_configuration(self) -> None:
        c = _make_connector()
        assert c.classify_extraction_error(SageInvalidRequestError("400")) == \
            ExtractionErrorClassification.DETERMINISTIC_INVALID_CONFIGURATION

    def test_metadata_error_is_unknown(self) -> None:
        c = _make_connector()
        assert c.classify_extraction_error(SageMetadataError("unknown")) == \
            ExtractionErrorClassification.UNKNOWN

    def test_unrecognised_exception_is_unknown(self) -> None:
        c = _make_connector()
        assert c.classify_extraction_error(RuntimeError("unexpected")) == \
            ExtractionErrorClassification.UNKNOWN

    def test_value_error_is_unknown(self) -> None:
        c = _make_connector()
        assert c.classify_extraction_error(ValueError("bad input")) == \
            ExtractionErrorClassification.UNKNOWN


# ---------------------------------------------------------------------------
# _build_sage factory function
# ---------------------------------------------------------------------------


class TestBuildSageFactory:
    def test_missing_connector_params_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="missing required keys"):
            _build_sage(
                environment=_ENV,
                region_name=_REGION,
                connector_params={"sage_product": "intacct"},  # missing object_path
                raw_s3_bucket="test-bucket",
            )

    def test_missing_both_params_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="missing required keys"):
            _build_sage(
                environment=_ENV,
                region_name=_REGION,
                connector_params={},
                raw_s3_bucket="test-bucket",
            )

    def test_invalid_sage_product_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="is not supported"):
            _build_sage(
                environment=_ENV,
                region_name=_REGION,
                connector_params={
                    "sage_product": "nonexistent-erp",
                    "object_path": _OBJECT_PATH,
                },
                raw_s3_bucket="test-bucket",
            )

    def test_valid_params_returns_connector_and_writer(self) -> None:
        with (
            patch("connector_runtime.adapters.sage.sage_connector.SageCredentialManager"),
            patch("connector_runtime.adapters.sage.sage_connector.SageHttpClient"),
        ):
            connector, writer = _build_sage(
                environment=_ENV,
                region_name=_REGION,
                connector_params={
                    "sage_product": "intacct",
                    "object_path": _OBJECT_PATH,
                },
                raw_s3_bucket="test-raw-bucket",
            )
        assert isinstance(connector, SageConnector)
        assert isinstance(writer, SageRawLayerWriter)

    def test_writer_has_correct_product(self) -> None:
        with (
            patch("connector_runtime.adapters.sage.sage_connector.SageCredentialManager"),
            patch("connector_runtime.adapters.sage.sage_connector.SageHttpClient"),
        ):
            _, writer = _build_sage(
                environment=_ENV,
                region_name=_REGION,
                connector_params={
                    "sage_product": "intacct",
                    "object_path": _OBJECT_PATH,
                },
                raw_s3_bucket="test-raw-bucket",
            )
        assert writer._sage_product == "intacct"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# X3 helpers
# ---------------------------------------------------------------------------

_X3_ENDPOINT = "BPCUSTOMER"
_X3_BASE_URL = "https://x3.company.com/api/SEED"
_X3_ENTITY_ID = "sage-x3-customer"


def _make_x3_connector() -> SageConnector:
    """Build an X3 SageConnector with mocked internals."""
    connector = SageConnector.__new__(SageConnector)
    connector._sage_product = "x3"  # type: ignore[attr-defined]
    connector._object_path = _X3_ENDPOINT  # type: ignore[attr-defined]

    mock_auth = MagicMock()
    mock_auth.base_url = _X3_BASE_URL
    mock_auth.get_access_token.return_value = "x3-bearer-token"
    mock_auth.build_auth_headers.return_value = {"Authorization": "Bearer x3-bearer-token"}
    connector._auth = mock_auth  # type: ignore[attr-defined]

    connector._http_client = MagicMock()
    connector._metadata_client = MagicMock()
    connector._query_engine = MagicMock()
    connector._credential_manager = MagicMock()
    return connector


def _make_x3_query_contract(
    watermark_field: str | None = "MODDAT_0",
    load_type: LoadType = LoadType.INCREMENTAL,
) -> QueryContract:
    """Build a QueryContract with the X3 OData discriminant."""
    body = {
        X3_ODATA_DISCRIMINANT: True,
        "endpoint": _X3_ENDPOINT,
        "select": "BPCNUM_0,BPCNAM_0,MODDAT_0",
        "filter": f"{watermark_field} ge __X3_LOWER_BOUND__ and {watermark_field} lt __X3_UPPER_BOUND__"
        if watermark_field else None,
        "orderby": f"{watermark_field} asc" if watermark_field else "BPCNUM_0 asc",
    }
    return QueryContract(
        source_id="sage",
        entity_id=_X3_ENTITY_ID,
        query_text=json.dumps(body),
        query_parameters={"lower_bound": _LOWER, "upper_bound": _UPPER}
        if watermark_field else {},
        load_type=load_type,
        watermark_lower=_LOWER if watermark_field else None,
        watermark_upper=_UPPER if watermark_field else None,
        watermark_field=watermark_field,
    )


def _x3_page(records: list[dict], next_link: str | None = None) -> dict:
    """Build a minimal OData v4 page response."""
    resp: dict = {"value": records}
    if next_link:
        resp["@odata.nextLink"] = next_link
    return resp


# ---------------------------------------------------------------------------
# X3 execution path tests
# ---------------------------------------------------------------------------


class TestX3ExecuteExtraction:
    """Tests for the Sage X3 OData GET execution path."""

    def test_discriminant_routes_to_x3_path(self) -> None:
        """Query body with _x3_odata:true must use GET, not POST."""
        connector = _make_x3_connector()
        connector._http_client.get.return_value = _x3_page([{"BPCNUM_0": "C001"}])

        qc = _make_x3_query_contract()
        list(connector.execute_extraction(qc, run_id=_RUN_ID))

        connector._http_client.get.assert_called_once()
        connector._http_client.post.assert_not_called()

    def test_x3_single_page_yields_all_records(self) -> None:
        connector = _make_x3_connector()
        rows = [{"BPCNUM_0": f"C{i:03d}", "BPCNAM_0": f"Corp {i}"} for i in range(5)]
        connector._http_client.get.return_value = _x3_page(rows)

        qc = _make_x3_query_contract()
        records = list(connector.execute_extraction(qc, run_id=_RUN_ID))
        assert len(records) == 5
        assert records[0].payload["BPCNUM_0"] == "C000"

    def test_x3_empty_page_stops_iteration(self) -> None:
        connector = _make_x3_connector()
        connector._http_client.get.return_value = _x3_page([])

        qc = _make_x3_query_contract()
        records = list(connector.execute_extraction(qc, run_id=_RUN_ID))
        assert records == []
        connector._http_client.get.assert_called_once()

    def test_x3_multipage_skip_based_pagination(self) -> None:
        """Full pages trigger $skip-based pagination when nextLink is absent."""
        connector = _make_x3_connector()
        page1 = [{"BPCNUM_0": f"C{i:04d}"} for i in range(X3_PAGE_SIZE)]
        page2 = [{"BPCNUM_0": f"C{i + X3_PAGE_SIZE:04d}"} for i in range(3)]
        connector._http_client.get.side_effect = [
            _x3_page(page1),   # full page → continue with $skip
            _x3_page(page2),   # partial page, no nextLink → stop
        ]

        qc = _make_x3_query_contract()
        records = list(connector.execute_extraction(qc, run_id=_RUN_ID))
        assert len(records) == X3_PAGE_SIZE + 3

        # Second call must include $skip=1000
        second_call_params = connector._http_client.get.call_args_list[1][1]["params"]
        assert second_call_params.get("$skip") == str(X3_PAGE_SIZE)

    def test_x3_follows_next_link(self) -> None:
        """@odata.nextLink in response is followed directly on the next request."""
        connector = _make_x3_connector()
        next_link = f"{_X3_BASE_URL}/BPCUSTOMER?$skiptoken=abc123"
        page1 = [{"BPCNUM_0": f"C{i:04d}"} for i in range(X3_PAGE_SIZE)]
        page2 = [{"BPCNUM_0": "C9999"}]

        connector._http_client.get.side_effect = [
            _x3_page(page1, next_link=next_link),
            _x3_page(page2),
        ]

        qc = _make_x3_query_contract()
        records = list(connector.execute_extraction(qc, run_id=_RUN_ID))
        assert len(records) == X3_PAGE_SIZE + 1

        # Second call must use the nextLink URL directly.
        second_url = connector._http_client.get.call_args_list[1][1]["url"]
        assert second_url == next_link

    def test_x3_partial_page_with_nextlink_does_not_stop_early(self) -> None:
        """
        Regression test: a partial page (<X3_PAGE_SIZE records) MUST NOT cause
        the loop to break when @odata.nextLink is present.  The server dictates
        continuation — not the page size.
        """
        connector = _make_x3_connector()
        next_link = f"{_X3_BASE_URL}/BPCUSTOMER?$skiptoken=partial"
        # First page: partial (only 500 records) but server still has more.
        page1 = [{"BPCNUM_0": f"C{i:04d}"} for i in range(500)]
        page2 = [{"BPCNUM_0": f"C{i + 500:04d}"} for i in range(200)]

        connector._http_client.get.side_effect = [
            _x3_page(page1, next_link=next_link),
            _x3_page(page2),  # no nextLink — done
        ]

        qc = _make_x3_query_contract()
        records = list(connector.execute_extraction(qc, run_id=_RUN_ID))
        # Must yield ALL records from both pages, not just the first 500.
        assert len(records) == 700

    def test_x3_source_timestamp_set_from_watermark(self) -> None:
        connector = _make_x3_connector()
        rows = [{"BPCNUM_0": "C001", "MODDAT_0": "2026-06-15T00:00:00Z"}]
        connector._http_client.get.return_value = _x3_page(rows)

        qc = _make_x3_query_contract(watermark_field="MODDAT_0")
        records = list(connector.execute_extraction(qc, run_id=_RUN_ID))
        assert records[0].source_timestamp == "2026-06-15T00:00:00Z"

    def test_x3_401_mid_extraction_triggers_token_invalidation_and_retry(self) -> None:
        connector = _make_x3_connector()
        rows = [{"BPCNUM_0": "C001"}]
        connector._http_client.get.side_effect = [
            SageAuthenticationError("401"),
            _x3_page(rows),
        ]

        qc = _make_x3_query_contract()
        records = list(connector.execute_extraction(qc, run_id=_RUN_ID))
        assert len(records) == 1
        connector._auth.invalidate_token.assert_called_once()


# ---------------------------------------------------------------------------
# X3 error classification
# ---------------------------------------------------------------------------


class TestX3ClassifyExtractionError:
    def test_x3_auth_error_is_deterministic_invalid_credentials(self) -> None:
        c = _make_x3_connector()
        assert c.classify_extraction_error(X3AuthError("rejected")) == \
            ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS

    def test_x3_credential_error_is_deterministic_invalid_credentials(self) -> None:
        c = _make_x3_connector()
        assert c.classify_extraction_error(X3CredentialError("no secret")) == \
            ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS

    def test_x3_query_build_error_is_deterministic_invalid_configuration(self) -> None:
        c = _make_x3_connector()
        assert c.classify_extraction_error(X3QueryBuildError("bad endpoint")) == \
            ExtractionErrorClassification.DETERMINISTIC_INVALID_CONFIGURATION
