"""Source connection model tests (DL-SCOPE-03, DL-SCOPE-06, DL-SCOPE-08)."""

from __future__ import annotations

import pytest

from tenancy.connection_keys import (
    connection_scoped_key,
    curated_glue_table_name,
    raw_layer_path_segments,
    resolve_connection_id,
    schedule_name_parts,
)
from tenancy.scope_contract import AttributionMode
from tenancy.source_connection import (
    ConnectionOwnerType,
    ConnectionState,
    ConnectionStateTransitionError,
    SourceConnection,
    connection_credential_path,
    connection_writeback_credential_path,
    default_connection_for_source,
    validate_connection_transition,
)


def _connection(**overrides) -> SourceConnection:
    base = {
        "tenant_code": "evive",
        "connection_id": "hubspot-brothers-gutters",
        "source_id": "hubspot",
        "display_name": "Brothers Gutters HubSpot",
        "state": ConnectionState.ACTIVE,
    }
    return SourceConnection(**{**base, **overrides})


class TestOwnership:
    def test_tenant_owned_connection_has_no_scope_unit(self):
        assert _connection().owning_scope_unit_id() is None

    def test_scope_unit_owned_connection_attributes_provenance(self):
        connection = _connection(
            owner_type=ConnectionOwnerType.SCOPE_UNIT, owner_id="franchisee-0042"
        )
        assert connection.owning_scope_unit_id() == "franchisee-0042"
        assert connection.attribution_mode is AttributionMode.PROVENANCE_DERIVED

    def test_scope_unit_owner_requires_owner_id(self):
        with pytest.raises(ValueError, match="owner_id is required"):
            _connection(owner_type=ConnectionOwnerType.SCOPE_UNIT)

    def test_tenant_owner_must_not_carry_owner_id(self):
        with pytest.raises(ValueError, match="must be absent"):
            _connection(owner_type=ConnectionOwnerType.TENANT, owner_id="franchisee-0042")

    def test_scope_unit_owner_cannot_be_field_derived(self):
        with pytest.raises(ValueError, match="provenance-derived by construction"):
            _connection(
                owner_type=ConnectionOwnerType.SCOPE_UNIT,
                owner_id="franchisee-0042",
                attribution_mode=AttributionMode.FIELD_DERIVED,
                scope_attribution_field="franchise_code",
            )

    def test_field_derived_requires_a_mapped_field(self):
        with pytest.raises(ValueError, match="requires scope_attribution_field"):
            _connection(attribution_mode=AttributionMode.FIELD_DERIVED)


class TestLifecycle:
    def test_pending_to_active_is_permitted(self):
        assert (
            _connection(state=ConnectionState.PENDING).transitioned_to(ConnectionState.ACTIVE).state
            is ConnectionState.ACTIVE
        )

    def test_retirement_stamps_a_timestamp(self):
        retired = _connection().transitioned_to(ConnectionState.RETIRED)
        assert retired.state is ConnectionState.RETIRED
        assert retired.retired_at is not None

    def test_retirement_is_terminal(self):
        retired = _connection().transitioned_to(ConnectionState.RETIRED)
        with pytest.raises(ConnectionStateTransitionError):
            retired.transitioned_to(ConnectionState.ACTIVE)

    def test_pending_cannot_jump_to_failing(self):
        with pytest.raises(ConnectionStateTransitionError, match="not permitted"):
            validate_connection_transition(ConnectionState.PENDING, ConnectionState.FAILING)

    def test_only_active_and_failing_are_extractable(self):
        assert _connection(state=ConnectionState.ACTIVE).is_extractable is True
        assert _connection(state=ConnectionState.FAILING).is_extractable is True
        assert _connection(state=ConnectionState.SUSPENDED).is_extractable is False
        assert _connection(state=ConnectionState.PENDING).is_extractable is False


class TestCredentialPaths:
    def test_read_path_is_per_connection(self):
        assert (
            connection_credential_path("evive", "hubspot-grasons")
            == "edl/tenants/evive/connections/hubspot-grasons/credentials"
        )

    def test_writeback_path_is_a_separate_secret(self):
        read = connection_credential_path("evive", "hubspot-grasons")
        write = connection_writeback_credential_path("evive", "hubspot-grasons")
        assert write != read
        assert write.startswith(read)

    def test_paths_are_exposed_on_the_model(self):
        connection = _connection()
        assert connection.credential_path.endswith("/credentials")
        assert connection.writeback_credential_path.endswith("-writeback")


class TestConnectionKeys:
    def test_default_connection_keeps_the_pre_dl12_key(self):
        assert connection_scoped_key("demo", "salesforce", None) == "demo#salesforce"

    def test_explicit_connection_partitions_the_key(self):
        assert (
            connection_scoped_key("evive", "hubspot", "hubspot-grasons") == "evive#hubspot-grasons"
        )

    def test_twelve_connections_never_collide(self):
        keys = {
            connection_scoped_key("evive", "hubspot", f"hubspot-franchisee-{i:04d}")
            for i in range(12)
        }
        assert len(keys) == 12

    def test_default_connection_keeps_the_existing_raw_path(self):
        assert raw_layer_path_segments("salesforce", None) == ["salesforce"]
        assert raw_layer_path_segments("salesforce", "salesforce") == ["salesforce"]

    def test_non_default_connection_gets_its_own_raw_prefix(self):
        assert raw_layer_path_segments("hubspot", "hubspot-grasons") == [
            "hubspot",
            "hubspot-grasons",
        ]

    def test_schedule_name_parts_use_the_connection(self):
        assert schedule_name_parts("evive", "hubspot", "hubspot-grasons") == (
            "evive",
            "hubspot-grasons",
        )
        assert schedule_name_parts("demo", "salesforce", None) == ("demo", "salesforce")

    def test_curated_glue_table_name_is_glue_safe(self):
        name = curated_glue_table_name("acme-corp", "hubspot-grasons", "hubspot-company", "crm")
        assert name == "acme_corp_hubspot_grasons_hubspot_company_crm_curated"

    def test_resolve_connection_id_falls_back_to_source(self):
        assert resolve_connection_id("salesforce", None) == "salesforce"
        assert resolve_connection_id("salesforce", "") == "salesforce"
        assert resolve_connection_id("hubspot", "hubspot-shine") == "hubspot-shine"


class TestMigrationDefault:
    def test_default_connection_is_tenant_owned_and_active(self):
        connection = default_connection_for_source("demo", "salesforce")
        assert connection.connection_id == "salesforce"
        assert connection.owner_type is ConnectionOwnerType.TENANT
        assert connection.state is ConnectionState.ACTIVE
