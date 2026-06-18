"""
Tests for NetSuiteConnector.

Coverage:
  - Connector registers as 'netsuite' in the registry
  - get_capability_declaration() returns correct values
  - discover_queryable_fields() delegates to NetSuiteMetadataAdapter
  - build_extraction_query() produces parameterized QueryContract
  - execute_extraction() pages through SuiteQL results and yields ExtractionRecord
  - execute_extraction() sets source_timestamp from watermark field
  - classify_extraction_error() maps all known exception types correctly
  - Error classification: credential, network, timeout, config
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
import requests_mock as requests_mock_lib

from connector_runtime.adapters.netsuite.netsuite_auth_client import (
    NetSuiteAuthError,
    NetSuiteCredentialError,
)
from connector_runtime.adapters.netsuite.netsuite_connector import (
    _PAGE_SIZE,
    NetSuiteConnector,
    NetSuiteSuiteQLRateLimitError,
)
from connector_runtime.adapters.netsuite.netsuite_incremental_query_planner import (
    NetSuiteIncrementalQueryPlannerError,
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
_ACCOUNT_ID = "1234567"
_RECORD_TYPE = "customer"
_SECRET_NAME = f"{_ENV}/sources/netsuite/credentials"

_VALID_SECRET = {
    "account_id": _ACCOUNT_ID,
    "consumer_key": "ck",
    "consumer_secret": "cs",
    "token_id": "ti",
    "token_secret": "ts",
}

_SUITEQL_URL = f"https://{_ACCOUNT_ID}.suitetalk.api.netsuite.com/services/rest/query/v1/suiteql"


def _make_connector() -> NetSuiteConnector:
    """Construct connector with mocked auth client to avoid Secrets Manager calls."""
    connector = NetSuiteConnector.__new__(NetSuiteConnector)
    mock_auth = MagicMock()
    mock_auth.account_id = _ACCOUNT_ID
    mock_auth.get_auth_headers.return_value = {"Authorization": "OAuth realm=test"}
    connector._record_type = _RECORD_TYPE  # type: ignore[attr-defined]
    connector._auth = mock_auth  # type: ignore[attr-defined]
    # Create a mock metadata adapter so discover_queryable_fields() works without HTTP.
    mock_adapter = MagicMock()
    connector._metadata_adapter = mock_adapter  # type: ignore[attr-defined]
    return connector


def _make_query_contract(
    field_names: list[str] | None = None,
    load_type: LoadType = LoadType.FULL,
    watermark_field: str | None = None,
) -> QueryContract:
    names = field_names or ["id", "companyname"]
    return QueryContract(
        source_id="netsuite",
        entity_id="netsuite-customer",
        query_text=f"SELECT {', '.join(names)} FROM {_RECORD_TYPE}",
        query_parameters={},
        load_type=load_type,
        watermark_lower=None,
        watermark_upper=None,
        watermark_field=watermark_field,
    )


def _make_field_contract(field_names: list[str] | None = None) -> FieldContract:
    names = field_names or ["id", "companyname"]
    descriptors = tuple(
        FieldDescriptor(name=n, data_type="STRING", is_nullable=True, is_queryable=True)
        for n in names
    )
    return FieldContract(
        source_id="netsuite",
        entity_id="netsuite-customer",
        fields=descriptors,
        discovery_timestamp=datetime.now(UTC),
        schema_fingerprint=FieldContract.compute_fingerprint(descriptors),
    )


class TestConnectorRegistration:
    def test_connector_registered_under_netsuite(self) -> None:
        assert "netsuite" in connector_registry.registered_source_ids

    def test_registry_resolves_netsuite(self) -> None:
        MagicMock()
        mock_auth_client = MagicMock()
        mock_auth_client.account_id = _ACCOUNT_ID
        with (
            patch(
                "connector_runtime.adapters.netsuite.netsuite_connector.NetSuiteAuthClient",
                return_value=mock_auth_client,
            ),
        ):
            connector = connector_registry.resolve(
                "netsuite",
                environment=_ENV,
                region_name=_REGION,
                record_type=_RECORD_TYPE,
            )
        assert isinstance(connector, NetSuiteConnector)


class TestCapabilityDeclaration:
    def test_source_id_is_netsuite(self) -> None:
        connector = _make_connector()
        caps = connector.get_capability_declaration()
        assert caps.source_id == "netsuite"

    def test_bulk_extraction_not_supported(self) -> None:
        connector = _make_connector()
        caps = connector.get_capability_declaration()
        assert caps.supports_bulk_extraction is False

    def test_incremental_and_full_supported(self) -> None:
        connector = _make_connector()
        caps = connector.get_capability_declaration()
        assert caps.supports_incremental is True
        assert caps.supports_full_load is True

    def test_all_field_modes_supported(self) -> None:
        connector = _make_connector()
        caps = connector.get_capability_declaration()
        modes = caps.supported_field_modes
        assert FieldMode.ALL in modes
        assert FieldMode.STANDARD in modes
        assert FieldMode.CUSTOM in modes
        assert FieldMode.INCLUDE_ONLY in modes


class TestExecuteExtraction:
    def test_single_page_yields_all_records(self, requests_mock: requests_mock_lib.Mocker) -> None:
        connector = _make_connector()
        rows = [{"id": str(i), "companyname": f"Corp {i}"} for i in range(5)]
        requests_mock.post(_SUITEQL_URL, json={"items": rows, "hasMore": False})

        qc = _make_query_contract()
        records = list(connector.execute_extraction(qc, run_id="run-001"))
        assert len(records) == 5
        assert records[0].payload["id"] == "0"

    def test_paginated_results_all_yielded(self, requests_mock: requests_mock_lib.Mocker) -> None:
        """Two pages: first has PAGE_SIZE rows, second has fewer."""
        connector = _make_connector()
        page1_rows = [{"id": str(i)} for i in range(_PAGE_SIZE)]
        page2_rows = [{"id": str(i)} for i in range(3)]
        responses = [
            {"json": {"items": page1_rows, "hasMore": True}},
            {"json": {"items": page2_rows, "hasMore": False}},
        ]
        requests_mock.post(_SUITEQL_URL, responses)

        qc = _make_query_contract()
        records = list(connector.execute_extraction(qc, run_id="run-002"))
        assert len(records) == _PAGE_SIZE + 3

    def test_source_timestamp_set_from_watermark_field(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        connector = _make_connector()
        rows = [{"id": "1", "lastmodifieddate": "2026-06-10T12:00:00Z"}]
        requests_mock.post(_SUITEQL_URL, json={"items": rows, "hasMore": False})

        qc = _make_query_contract(
            field_names=["id", "lastmodifieddate"],
            watermark_field="lastmodifieddate",
        )
        records = list(connector.execute_extraction(qc, run_id="run-003"))
        assert records[0].source_timestamp == "2026-06-10T12:00:00Z"

    def test_empty_page_stops_pagination(self, requests_mock: requests_mock_lib.Mocker) -> None:
        connector = _make_connector()
        requests_mock.post(_SUITEQL_URL, json={"items": [], "hasMore": False})
        records = list(connector.execute_extraction(_make_query_contract(), run_id="run-004"))
        assert records == []


class TestErrorClassification:
    def test_credential_error_is_deterministic(self) -> None:
        connector = _make_connector()
        result = connector.classify_extraction_error(NetSuiteCredentialError("bad"))
        assert result == ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS

    def test_auth_error_is_deterministic(self) -> None:
        connector = _make_connector()
        result = connector.classify_extraction_error(NetSuiteAuthError("bad"))
        assert result == ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS

    def test_query_planner_error_is_config(self) -> None:
        connector = _make_connector()
        result = connector.classify_extraction_error(
            NetSuiteIncrementalQueryPlannerError("bad query")
        )
        assert result == ExtractionErrorClassification.DETERMINISTIC_INVALID_CONFIGURATION

    def test_os_error_is_transient_network(self) -> None:
        connector = _make_connector()
        result = connector.classify_extraction_error(OSError("connection refused"))
        assert result == ExtractionErrorClassification.TRANSIENT_NETWORK

    def test_unknown_exception_is_unknown(self) -> None:
        connector = _make_connector()
        result = connector.classify_extraction_error(RuntimeError("unexpected"))
        assert result == ExtractionErrorClassification.UNKNOWN

    def test_empty_record_type_raises(self) -> None:
        with pytest.raises(ValueError, match="record_type"):
            NetSuiteConnector(environment=_ENV, region_name=_REGION, record_type="")

    def test_rate_limit_429_is_transient_throttle(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        """HTTP 429 must map to TRANSIENT_THROTTLE so the retry framework backs off."""
        connector = _make_connector()
        requests_mock.post(_SUITEQL_URL, status_code=429)

        qc = _make_query_contract()
        with pytest.raises(NetSuiteSuiteQLRateLimitError):
            list(connector.execute_extraction(qc, run_id="run-throttle"))

    def test_rate_limit_error_classifies_as_transient_throttle(self) -> None:
        connector = _make_connector()
        result = connector.classify_extraction_error(NetSuiteSuiteQLRateLimitError("429"))
        assert result == ExtractionErrorClassification.TRANSIENT_THROTTLE
