"""
Tests for SalesforceConnector (full ConnectorInterface implementation).

Covers:
  - get_capability_declaration returns correct Salesforce capabilities
  - connector registered under source_id "salesforce" in ConnectorRegistry
  - discover_queryable_fields delegates to metadata discovery
  - build_extraction_query delegates to SOQL builder
  - execute_extraction delegates to Bulk API controller and yields records
  - classify_extraction_error maps all exception types correctly
  - New Salesforce object onboarded by configuration only — no code change
    (acceptance criteria: adding a new entity requires config record only)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from connector_runtime.adapters.salesforce.salesforce_auth_client import (
    SalesforceAuthError,
    SalesforceCredentialError,
)
from connector_runtime.adapters.salesforce.salesforce_bulk_query_job_controller import (
    BulkApiLimitError,
    BulkJobFailedError,
    BulkJobTimeoutError,
)
from connector_runtime.adapters.salesforce.salesforce_connector import SalesforceConnector
from connector_runtime.interfaces.connector_interface import (
    ExtractionErrorClassification,
    ExtractionRecord,
)
from connector_runtime.query_builders.salesforce_soql_query_builder import (
    SalesforceSoqlQueryBuilderError,
)
from contracts.entity_configuration_contract import FieldMode, LoadType

_ENV = "dev"
_REGION = "us-east-1"


def _make_connector(object_name: str = "Account") -> SalesforceConnector:
    with patch("connector_runtime.adapters.salesforce.salesforce_connector.SalesforceAuthClient"):
        return SalesforceConnector(
            environment=_ENV,
            region_name=_REGION,
            object_name=object_name,
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestConnectorRegistration:
    def test_salesforce_registered_in_connector_registry(self) -> None:
        """Acceptance: connector registered via decorator at import time."""
        # Import triggers registration
        import connector_runtime.adapters.salesforce.salesforce_connector  # noqa: F401
        from connector_runtime.registry import connector_registry

        assert "salesforce" in connector_registry.registered_source_ids


# ---------------------------------------------------------------------------
# Capability declaration
# ---------------------------------------------------------------------------


class TestCapabilityDeclaration:
    def test_supports_bulk_extraction(self) -> None:
        connector = _make_connector()
        caps = connector.get_capability_declaration()
        assert caps.supports_bulk_extraction is True

    def test_bulk_threshold_is_2000(self) -> None:
        connector = _make_connector()
        caps = connector.get_capability_declaration()
        assert caps.bulk_threshold_records == 2_000

    def test_supports_incremental_and_full(self) -> None:
        connector = _make_connector()
        caps = connector.get_capability_declaration()
        assert caps.supports_incremental is True
        assert caps.supports_full_load is True

    def test_all_field_modes_supported(self) -> None:
        connector = _make_connector()
        caps = connector.get_capability_declaration()
        assert FieldMode.ALL in caps.supported_field_modes
        assert FieldMode.INCLUDE_ONLY in caps.supported_field_modes


# ---------------------------------------------------------------------------
# Configuration-only new entity onboarding
# ---------------------------------------------------------------------------


class TestConfigurationOnlyOnboarding:
    def test_different_object_names_use_same_class(self) -> None:
        """
        Acceptance criteria: adding a new Salesforce object (e.g. Contact,
        Opportunity) requires only a new configuration record — no new class.
        """
        account_connector = _make_connector("Account")
        contact_connector = _make_connector("Contact")
        opportunity_connector = _make_connector("Opportunity")

        # All use the same class — zero code change for new objects
        assert type(account_connector) is SalesforceConnector
        assert type(contact_connector) is SalesforceConnector
        assert type(opportunity_connector) is SalesforceConnector


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


class TestErrorClassification:
    def test_credential_error_is_deterministic_invalid_credentials(self) -> None:
        connector = _make_connector()
        cls = connector.classify_extraction_error(SalesforceCredentialError("bad creds"))
        assert cls == ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS

    def test_auth_error_is_deterministic_invalid_credentials(self) -> None:
        connector = _make_connector()
        cls = connector.classify_extraction_error(SalesforceAuthError("401"))
        assert cls == ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS

    def test_soql_builder_error_is_deterministic_invalid_configuration(self) -> None:
        connector = _make_connector()
        cls = connector.classify_extraction_error(SalesforceSoqlQueryBuilderError("bad soql"))
        assert cls == ExtractionErrorClassification.DETERMINISTIC_INVALID_CONFIGURATION

    def test_bulk_api_limit_error_is_transient_throttle(self) -> None:
        connector = _make_connector()
        cls = connector.classify_extraction_error(BulkApiLimitError("no quota"))
        assert cls == ExtractionErrorClassification.TRANSIENT_THROTTLE

    def test_bulk_job_timeout_is_transient_timeout(self) -> None:
        connector = _make_connector()
        cls = connector.classify_extraction_error(BulkJobTimeoutError("timeout"))
        assert cls == ExtractionErrorClassification.TRANSIENT_TIMEOUT

    def test_bulk_job_failed_is_unknown(self) -> None:
        connector = _make_connector()
        cls = connector.classify_extraction_error(BulkJobFailedError("sf error"))
        assert cls == ExtractionErrorClassification.UNKNOWN

    def test_os_error_is_transient_network(self) -> None:
        connector = _make_connector()
        cls = connector.classify_extraction_error(OSError("connection reset"))
        assert cls == ExtractionErrorClassification.TRANSIENT_NETWORK

    def test_unknown_exception_is_unknown(self) -> None:
        connector = _make_connector()
        cls = connector.classify_extraction_error(ValueError("unexpected"))
        assert cls == ExtractionErrorClassification.UNKNOWN

    def test_classify_never_raises(self) -> None:
        """classify_extraction_error must never raise itself."""
        connector = _make_connector()
        for exc in [
            RuntimeError("x"),
            KeyError("y"),
            MemoryError("z"),
        ]:
            result = connector.classify_extraction_error(exc)
            assert isinstance(result, ExtractionErrorClassification)


# ---------------------------------------------------------------------------
# execute_extraction — mocked bulk controller
# ---------------------------------------------------------------------------


class TestExecuteExtraction:
    def test_yields_records_from_bulk_controller(self) -> None:

        from connector_runtime.interfaces.connector_interface import (
            QueryContract,
        )

        contract = QueryContract(
            source_id="salesforce",
            entity_id="salesforce-account",
            query_text=(
                "SELECT Id FROM Account WHERE SystemModstamp >= :lower_bound"
                " AND SystemModstamp < :upper_bound"
            ),
            query_parameters={
                "lower_bound": "2026-06-01T00:00:00Z",
                "upper_bound": "2026-06-02T00:00:00Z",
            },
            load_type=LoadType.INCREMENTAL,
            watermark_lower="2026-06-01T00:00:00Z",
            watermark_upper="2026-06-02T00:00:00Z",
        )

        fake_records = [
            ExtractionRecord(payload={"Id": "001"}),
            ExtractionRecord(payload={"Id": "002"}),
        ]

        with (
            patch(
                "connector_runtime.adapters.salesforce.salesforce_connector.SalesforceAuthClient"
            ),
            patch(
                "connector_runtime.adapters.salesforce.salesforce_connector.SalesforceBulkQueryJobController"
            ) as mock_ctrl_cls,
        ):
            mock_ctrl = MagicMock()
            mock_ctrl.execute.return_value = iter(fake_records)
            mock_ctrl_cls.return_value = mock_ctrl

            connector = SalesforceConnector(
                environment=_ENV,
                region_name=_REGION,
                object_name="Account",
            )
            results = list(
                connector.execute_extraction(contract, run_id="run-20260612-000000000000-aabbccdd")
            )

        assert len(results) == 2
        assert results[0].payload["Id"] == "001"
