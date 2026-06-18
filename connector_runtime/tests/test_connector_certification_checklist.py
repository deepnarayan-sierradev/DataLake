"""
Tests for connector_runtime/certification/connector_certification_checklist.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from connector_runtime.certification.connector_certification_checklist import (
    ConnectorCertificationChecklist,
    ConnectorCertificationReport,
)
from connector_runtime.interfaces.connector_interface import (
    ConnectorCapabilities,
    ConnectorInterface,
    ExtractionErrorClassification,
    ExtractionRecord,
    FieldContract,
    FieldDescriptor,
    QueryContract,
)
from contracts.entity_configuration_contract import FieldMode, LoadType

_EMPTY_FIELDS: tuple[FieldDescriptor, ...] = ()
_SAMPLE_FIELDS: tuple[FieldDescriptor, ...] = (
    FieldDescriptor(name="id", data_type="string", is_nullable=False, is_queryable=True),
    FieldDescriptor(name="name", data_type="string", is_nullable=True, is_queryable=True),
)


def _minimal_field_contract(source_id: str, entity_id: str) -> FieldContract:
    fields = _SAMPLE_FIELDS
    return FieldContract(
        source_id=source_id,
        entity_id=entity_id,
        fields=fields,
        discovery_timestamp=datetime.now(UTC),
        schema_fingerprint=FieldContract.compute_fingerprint(fields),
    )


# ---------------------------------------------------------------------------
# Minimal valid connector implementation for tests
# ---------------------------------------------------------------------------


class _ValidConnector(ConnectorInterface):
    """A connector that satisfies all certification requirements."""

    def get_capability_declaration(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            source_id="postgres-primary",
            supports_bulk_extraction=True,
        )

    def discover_queryable_fields(
        self,
        source_id: str,
        entity_id: str,
        field_mode: FieldMode,
        include_fields: list[str],
        exclude_fields: list[str],
    ) -> FieldContract:
        return _minimal_field_contract(source_id, entity_id)

    def build_extraction_query(
        self,
        field_contract: FieldContract,
        load_type: LoadType,
        watermark_field: str | None,
        watermark_lower: str | None,
        watermark_upper: str | None,
        extraction_window_days: int,
    ) -> QueryContract:
        return QueryContract(
            source_id=field_contract.source_id,
            entity_id=field_contract.entity_id,
            query_text="SELECT :id, :name FROM table WHERE updated > :wm",
            query_parameters={"wm": watermark_lower or ""},
            load_type=load_type,
            watermark_lower=watermark_lower,
            watermark_upper=watermark_upper,
        )

    def execute_extraction(
        self, query_contract: QueryContract, run_id: str
    ) -> Iterator[ExtractionRecord]:
        yield ExtractionRecord(payload={"id": "1"})

    def classify_extraction_error(self, exc: Exception) -> ExtractionErrorClassification:
        return ExtractionErrorClassification.TRANSIENT_NETWORK


class _ConnectorWithEnvironAccess(ConnectorInterface):
    """Connector that illegally reads os.environ."""

    def get_capability_declaration(self) -> ConnectorCapabilities:
        import os

        _ = os.environ.get("PASSWORD")
        return ConnectorCapabilities(source_id="bad-connector")

    def discover_queryable_fields(  # type: ignore[override]
        self,
        source_id: str,
        entity_id: str,
        field_mode: FieldMode,
        include_fields: list[str],
        exclude_fields: list[str],
    ) -> FieldContract:
        return _minimal_field_contract(source_id, entity_id)

    def build_extraction_query(  # type: ignore[override]
        self,
        field_contract: FieldContract,
        load_type: LoadType,
        watermark_field: str | None,
        watermark_lower: str | None,
        watermark_upper: str | None,
        extraction_window_days: int,
    ) -> QueryContract:
        return QueryContract(
            source_id="bad",
            entity_id="entity",
            query_text="SELECT 1",
            query_parameters={},
            load_type=load_type,
            watermark_lower=None,
            watermark_upper=None,
        )

    def execute_extraction(  # type: ignore[override]
        self,
        query_contract: QueryContract,
        run_id: str,
    ) -> Iterator[ExtractionRecord]:
        return iter([])

    def classify_extraction_error(self, exc: Exception) -> ExtractionErrorClassification:
        return ExtractionErrorClassification.UNKNOWN


class _ProhibitedNameConnector(ConnectorInterface):
    """Uses a prohibited name 'Helper' in the class name — detected via module-level class."""

    def get_capability_declaration(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(source_id="test")

    def discover_queryable_fields(  # type: ignore[override]
        self,
        source_id: str,
        entity_id: str,
        field_mode: FieldMode,
        include_fields: list[str],
        exclude_fields: list[str],
    ) -> FieldContract:
        return _minimal_field_contract(source_id, entity_id)

    def build_extraction_query(  # type: ignore[override]
        self,
        field_contract: FieldContract,
        load_type: LoadType,
        watermark_field: str | None,
        watermark_lower: str | None,
        watermark_upper: str | None,
        extraction_window_days: int,
    ) -> QueryContract:
        return QueryContract(
            source_id="test",
            entity_id="entity",
            query_text="SELECT 1",
            query_parameters={},
            load_type=load_type,
            watermark_lower=None,
            watermark_upper=None,
        )

    def execute_extraction(  # type: ignore[override]
        self,
        query_contract: QueryContract,
        run_id: str,
    ) -> Iterator[ExtractionRecord]:
        return iter([])

    def classify_extraction_error(self, exc: Exception) -> ExtractionErrorClassification:
        return ExtractionErrorClassification.UNKNOWN


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConnectorCertificationChecklist:
    def test_valid_connector_passes_all_checks(self) -> None:
        checklist = ConnectorCertificationChecklist()
        report = checklist.certify(_ValidConnector, source_id="postgres-primary")
        assert isinstance(report, ConnectorCertificationReport)
        assert report.passed is True
        assert report.failure_summary == ""

    def test_report_has_expected_fields(self) -> None:
        checklist = ConnectorCertificationChecklist()
        report = checklist.certify(_ValidConnector, source_id="postgres-primary")
        assert report.connector_class_name == "_ValidConnector"
        assert report.source_id == "postgres-primary"
        assert len(report.checks) > 0
        assert "T" in report.certified_at  # ISO-8601

    def test_invalid_source_id_fails_check(self) -> None:
        checklist = ConnectorCertificationChecklist()
        report = checklist.certify(_ValidConnector, source_id="INVALID SOURCE")
        assert report.passed is False
        source_id_check = next(c for c in report.checks if c.check_name == "source_id_format")
        assert source_id_check.passed is False

    def test_os_environ_access_fails_check(self) -> None:
        checklist = ConnectorCertificationChecklist()
        report = checklist.certify(_ConnectorWithEnvironAccess, source_id="bad-connector")
        no_env_check = next(c for c in report.checks if c.check_name == "no_os_environ_access")
        assert no_env_check.passed is False

    def test_non_connector_subclass_fails_check(self) -> None:
        checklist = ConnectorCertificationChecklist()

        class NotAConnector:
            pass

        report = checklist.certify(NotAConnector, source_id="not-a-connector")  # type: ignore[arg-type]
        subclass_check = next(
            c for c in report.checks if c.check_name == "is_connector_interface_subclass"
        )
        assert subclass_check.passed is False

    def test_prohibited_name_in_class_fails_check(self) -> None:
        checklist = ConnectorCertificationChecklist()

        class AccountHelperConnector(_ValidConnector):
            pass

        # 'helper' is a prohibited term
        report = checklist.certify(AccountHelperConnector, source_id="account-connector")
        prohibited_check = next(c for c in report.checks if c.check_name == "no_prohibited_names")
        assert prohibited_check.passed is False

    def test_valid_source_id_formats(self) -> None:
        checklist = ConnectorCertificationChecklist()
        for source_id in ["postgres-primary", "sf-crm", "mysql-orders", "ab"]:
            report = checklist.certify(_ValidConnector, source_id=source_id)
            check = next(c for c in report.checks if c.check_name == "source_id_format")
            assert check.passed is True, f"Expected {source_id!r} to pass"

    def test_invalid_source_id_formats(self) -> None:
        checklist = ConnectorCertificationChecklist()
        for source_id in ["", "1starts-with-number", "has space", "UPPERCASE"]:
            report = checklist.certify(_ValidConnector, source_id=source_id)
            check = next(c for c in report.checks if c.check_name == "source_id_format")
            assert check.passed is False, f"Expected {source_id!r} to fail"
