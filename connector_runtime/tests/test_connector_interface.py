"""
Tests for the connector interface contracts, value objects, and registry.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from connector_runtime.interfaces.connector_interface import (
    ConnectorCapabilities,
    ConnectorInterface,
    ExtractionErrorClassification,
    ExtractionRecord,
    FieldContract,
    FieldDescriptor,
    QueryContract,
)
from connector_runtime.registry import ConnectorRegistry

# Canonical enums are imported from contracts — same source used by the interface
from contracts.entity_configuration_contract import FieldMode, LoadType

# ---------------------------------------------------------------------------
# FieldDescriptor
# ---------------------------------------------------------------------------


class TestFieldDescriptor:
    def test_frozen_field_descriptor_is_immutable(self) -> None:
        descriptor = FieldDescriptor(
            name="SystemModstamp",
            data_type="datetime",
            is_nullable=False,
            is_queryable=True,
        )
        with pytest.raises(AttributeError):
            descriptor.name = "Modified"  # type: ignore[misc]

    def test_custom_field_flag(self) -> None:
        descriptor = FieldDescriptor(
            name="CustomScore__c",
            data_type="double",
            is_nullable=True,
            is_queryable=True,
            is_custom=True,
        )
        assert descriptor.is_custom is True
        assert descriptor.name == "CustomScore__c"

    def test_standard_field_defaults(self) -> None:
        descriptor = FieldDescriptor(
            name="Id",
            data_type="id",
            is_nullable=False,
            is_queryable=True,
        )
        assert descriptor.is_custom is False
        assert descriptor.length is None
        assert descriptor.precision is None


# ---------------------------------------------------------------------------
# FieldContract fingerprint
# ---------------------------------------------------------------------------


class TestFieldContractFingerprint:
    def _make_fields(self, names: list[str]) -> tuple[FieldDescriptor, ...]:
        return tuple(
            FieldDescriptor(name=n, data_type="string", is_nullable=True, is_queryable=True)
            for n in names
        )

    def test_same_fields_produce_same_fingerprint(self) -> None:
        fields = self._make_fields(["Id", "Name", "SystemModstamp"])
        fp1 = FieldContract.compute_fingerprint(fields)
        fp2 = FieldContract.compute_fingerprint(fields)
        assert fp1 == fp2

    def test_added_field_changes_fingerprint(self) -> None:
        fields_before = self._make_fields(["Id", "Name"])
        fields_after = self._make_fields(["Id", "Name", "NewField__c"])
        assert FieldContract.compute_fingerprint(
            fields_before
        ) != FieldContract.compute_fingerprint(fields_after)

    def test_removed_field_changes_fingerprint(self) -> None:
        fields_before = self._make_fields(["Id", "Name", "OldField__c"])
        fields_after = self._make_fields(["Id", "Name"])
        assert FieldContract.compute_fingerprint(
            fields_before
        ) != FieldContract.compute_fingerprint(fields_after)

    def test_field_order_does_not_affect_fingerprint(self) -> None:
        fields_a = self._make_fields(["Id", "Name", "SystemModstamp"])
        fields_b = self._make_fields(["SystemModstamp", "Id", "Name"])
        assert FieldContract.compute_fingerprint(fields_a) == FieldContract.compute_fingerprint(
            fields_b
        )

    def test_type_change_changes_fingerprint(self) -> None:
        fields_before = (
            FieldDescriptor(name="Score", data_type="integer", is_nullable=True, is_queryable=True),
        )
        fields_after = (
            FieldDescriptor(name="Score", data_type="double", is_nullable=True, is_queryable=True),
        )
        assert FieldContract.compute_fingerprint(
            fields_before
        ) != FieldContract.compute_fingerprint(fields_after)

    def test_fingerprint_is_sha256_hex(self) -> None:
        fields = self._make_fields(["Id"])
        fingerprint = FieldContract.compute_fingerprint(fields)
        # SHA-256 hex digest is always 64 chars
        assert len(fingerprint) == 64
        assert all(c in "0123456789abcdef" for c in fingerprint)


# ---------------------------------------------------------------------------
# QueryContract — parameterization requirement
# ---------------------------------------------------------------------------


class TestQueryContract:
    def test_query_text_is_stored_as_given(self) -> None:
        contract = QueryContract(
            source_id="salesforce",
            entity_id="salesforce-account",
            query_text=(
                "SELECT Id, Name FROM Account WHERE "
                "SystemModstamp >= :lower_bound AND SystemModstamp < :upper_bound"
            ),
            query_parameters={
                "lower_bound": "2026-06-10T00:00:00Z",
                "upper_bound": "2026-06-11T00:00:00Z",
            },
            load_type=LoadType.INCREMENTAL,
            watermark_lower="2026-06-10T00:00:00Z",
            watermark_upper="2026-06-11T00:00:00Z",
        )
        assert ":lower_bound" in contract.query_text
        assert ":upper_bound" in contract.query_text
        assert "2026-06-10T00:00:00Z" not in contract.query_text  # not interpolated


# ---------------------------------------------------------------------------
# ConnectorCapabilities
# ---------------------------------------------------------------------------


class TestConnectorCapabilities:
    def test_default_bulk_threshold(self) -> None:
        caps = ConnectorCapabilities(source_id="salesforce")
        assert caps.bulk_threshold_records == 2_000

    def test_salesforce_full_capability_declaration(self) -> None:
        caps = ConnectorCapabilities(
            source_id="salesforce",
            supports_bulk_extraction=True,
            supports_incremental=True,
            bulk_threshold_records=2_000,
            supported_field_modes=(
                FieldMode.ALL,
                FieldMode.STANDARD,
                FieldMode.CUSTOM,
                FieldMode.INCLUDE_ONLY,
            ),
        )
        assert caps.supports_bulk_extraction is True
        assert FieldMode.ALL in caps.supported_field_modes
        assert FieldMode.INCLUDE_ONLY in caps.supported_field_modes

    def test_mysql_capability_no_bulk(self) -> None:
        caps = ConnectorCapabilities(
            source_id="mysql-rds",
            supports_bulk_extraction=False,
            supports_incremental=True,
        )
        assert caps.supports_bulk_extraction is False


# ---------------------------------------------------------------------------
# ExtractionErrorClassification — taxonomy integrity
# ---------------------------------------------------------------------------


class TestExtractionErrorClassification:
    def test_all_values_are_unique(self) -> None:
        values = [c.value for c in ExtractionErrorClassification]
        assert len(values) == len(set(values))

    def test_transient_errors_start_with_transient_prefix(self) -> None:
        transient = [
            ExtractionErrorClassification.TRANSIENT_TIMEOUT,
            ExtractionErrorClassification.TRANSIENT_THROTTLE,
            ExtractionErrorClassification.TRANSIENT_NETWORK,
        ]
        for err in transient:
            assert err.value.startswith("transient_"), f"Expected {err} to start with 'transient_'"

    def test_deterministic_errors_start_with_deterministic_prefix(self) -> None:
        deterministic = [
            ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS,
            ExtractionErrorClassification.DETERMINISTIC_INVALID_OBJECT,
            ExtractionErrorClassification.DETERMINISTIC_INVALID_CONFIGURATION,
            ExtractionErrorClassification.DETERMINISTIC_SCHEMA_VIOLATION,
        ]
        for err in deterministic:
            assert err.value.startswith("deterministic_"), (
                f"Expected {err} to start with 'deterministic_'"
            )


# ---------------------------------------------------------------------------
# ConnectorRegistry
# ---------------------------------------------------------------------------


class TestConnectorRegistry:
    def _make_stub_connector(self) -> type[ConnectorInterface]:
        """Create a minimal stub ConnectorInterface implementation for testing."""

        class _StubConnector(ConnectorInterface):
            def get_capability_declaration(self) -> ConnectorCapabilities:
                return ConnectorCapabilities(source_id="stub-source")

            def discover_queryable_fields(
                self,
                source_id: str,
                entity_id: str,
                field_mode: FieldMode,
                include_fields: list[str],
                exclude_fields: list[str],
            ) -> FieldContract:
                raise NotImplementedError

            def build_extraction_query(
                self,
                field_contract: FieldContract,
                load_type: LoadType,
                watermark_field: str | None,
                watermark_lower: str | None,
                watermark_upper: str | None,
                extraction_window_days: int,
            ) -> QueryContract:
                raise NotImplementedError

            def execute_extraction(
                self,
                query_contract: QueryContract,
                run_id: str,
            ) -> Iterator[ExtractionRecord]:
                return iter([])

            def classify_extraction_error(
                self,
                exc: Exception,
            ) -> ExtractionErrorClassification:
                return ExtractionErrorClassification.UNKNOWN

        return _StubConnector

    def test_register_and_resolve_connector(self) -> None:
        registry = ConnectorRegistry()
        stub_cls = self._make_stub_connector()
        registry.register("stub-source")(stub_cls)
        connector = registry.resolve("stub-source")
        assert isinstance(connector, ConnectorInterface)

    def test_resolve_unknown_source_raises_key_error(self) -> None:
        registry = ConnectorRegistry()
        with pytest.raises(KeyError, match="No connector registered for source_id 'unknown'"):
            registry.resolve("unknown")

    def test_duplicate_registration_raises_value_error(self) -> None:
        registry = ConnectorRegistry()
        stub_cls = self._make_stub_connector()
        registry.register("dup-source")(stub_cls)
        with pytest.raises(ValueError, match="already registered"):
            registry.register("dup-source")(stub_cls)

    def test_registered_source_ids_sorted(self) -> None:
        registry = ConnectorRegistry()
        stub_cls = self._make_stub_connector()

        class _StubB(stub_cls):  # type: ignore[valid-type, misc]
            pass

        registry.register("beta-source")(stub_cls)
        registry.register("alpha-source")(_StubB)
        assert registry.registered_source_ids == ["alpha-source", "beta-source"]

    def test_register_builder_and_resolve_builder(self) -> None:
        registry = ConnectorRegistry()
        stub_cls = self._make_stub_connector()
        registry.register("builder-source")(stub_cls)

        def _build(env, region, params, bucket):  # type: ignore[no-untyped-def]
            return stub_cls(), object()

        registry.register_builder("builder-source", _build)
        resolved = registry.resolve_builder("builder-source")
        assert resolved is _build

    def test_register_builder_duplicate_raises(self) -> None:
        registry = ConnectorRegistry()
        stub_cls = self._make_stub_connector()
        registry.register("dup-builder")(stub_cls)

        def _build(env, region, params, bucket):  # type: ignore[no-untyped-def]
            return stub_cls(), object()

        registry.register_builder("dup-builder", _build)
        with pytest.raises(ValueError, match="already registered"):
            registry.register_builder("dup-builder", _build)

    def test_resolve_builder_unknown_raises_key_error(self) -> None:
        registry = ConnectorRegistry()
        with pytest.raises(KeyError, match="No connector builder registered"):
            registry.resolve_builder("nonexistent-builder-source")

    def test_reset_clears_all_registrations(self) -> None:
        registry = ConnectorRegistry()
        stub_cls = self._make_stub_connector()
        registry.register("reset-source")(stub_cls)

        def _build(env, region, params, bucket):  # type: ignore[no-untyped-def]
            return stub_cls(), object()

        registry.register_builder("reset-source", _build)
        assert "reset-source" in registry.registered_source_ids

        registry.reset()
        assert registry.registered_source_ids == []
        with pytest.raises(KeyError):
            registry.resolve("reset-source")
