"""Connection and scope-unit repository tests against moto DynamoDB."""

from __future__ import annotations

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from tenancy.scope_contract import (
    IMPLICIT_SCOPE_UNIT_ID,
    PartitionKind,
    PartitionModel,
    ScopeUnit,
    TenantPartitionProfile,
)
from tenancy.scope_unit_repository import (
    ScopeStoreUnavailableError,
    ScopeUnitNotFoundError,
    ScopeUnitRepository,
    ScopeWideningNotApprovedError,
)
from tenancy.source_connection import ConnectionOwnerType, ConnectionState, SourceConnection
from tenancy.source_connection_repository import (
    ConnectionAlreadyExistsError,
    ConnectionNotFoundError,
    SourceConnectionRepository,
)

_REGION = "us-east-1"


def _create_table(name: str, sort_key: str) -> None:
    boto3.client("dynamodb", region_name=_REGION).create_table(
        TableName=name,
        KeySchema=[
            {"AttributeName": "tenant_code", "KeyType": "HASH"},
            {"AttributeName": sort_key, "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "tenant_code", "AttributeType": "S"},
            {"AttributeName": sort_key, "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _connection(**overrides) -> SourceConnection:
    base = {
        "tenant_code": "evive",
        "connection_id": "hubspot-grasons",
        "source_id": "hubspot",
        "display_name": "Grasons HubSpot",
        "state": ConnectionState.ACTIVE,
    }
    return SourceConnection(**{**base, **overrides})


@mock_aws
class TestSourceConnectionRepository:
    def _repo(self) -> SourceConnectionRepository:
        _create_table("EdlSourceConnection", "connection_id")
        return SourceConnectionRepository(environment="dev", region_name=_REGION)

    def test_register_and_get_round_trip(self):
        repo = self._repo()
        repo.register_connection(_connection())
        loaded = repo.get_connection("evive", "hubspot-grasons")
        assert loaded.source_id == "hubspot"
        assert loaded.state is ConnectionState.ACTIVE

    def test_duplicate_registration_is_rejected(self):
        repo = self._repo()
        repo.register_connection(_connection())
        with pytest.raises(ConnectionAlreadyExistsError):
            repo.register_connection(_connection())

    def test_missing_connection_raises(self):
        repo = self._repo()
        with pytest.raises(ConnectionNotFoundError):
            repo.get_connection("evive", "hubspot-nowhere")

    def test_resolve_synthesises_the_migration_default(self):
        repo = self._repo()
        resolved = repo.resolve_connection("demo", "salesforce")
        assert resolved.connection_id == "salesforce"
        assert resolved.owner_type is ConnectionOwnerType.TENANT
        assert resolved.state is ConnectionState.ACTIVE

    def test_twelve_franchisee_connections_coexist(self):
        repo = self._repo()
        for index in range(12):
            repo.register_connection(
                _connection(
                    connection_id=f"hubspot-franchisee-{index:04d}",
                    owner_type=ConnectionOwnerType.SCOPE_UNIT,
                    owner_id=f"franchisee-{index:04d}",
                    display_name=f"Franchisee {index}",
                )
            )
        listed = repo.list_connections("evive", source_id="hubspot")
        assert len(listed) == 12
        assert len({c.owner_id for c in listed}) == 12

    def test_retired_connections_are_hidden_by_default(self):
        repo = self._repo()
        repo.register_connection(_connection())
        repo.transition_state("evive", "hubspot-grasons", ConnectionState.RETIRED)
        assert repo.list_connections("evive") == []
        assert len(repo.list_connections("evive", include_retired=True)) == 1

    def test_retired_connection_is_not_extractable(self):
        repo = self._repo()
        repo.register_connection(_connection())
        repo.transition_state("evive", "hubspot-grasons", ConnectionState.SUSPENDED)
        assert repo.list_extractable_connections("evive") == []

    def test_health_signals_persist(self):
        repo = self._repo()
        repo.register_connection(_connection())
        repo.record_successful_run("evive", "hubspot-grasons")
        repo.record_credential_verified("evive", "hubspot-grasons")
        loaded = repo.get_connection("evive", "hubspot-grasons")
        assert loaded.last_successful_run_at is not None
        assert loaded.credential_verified_at is not None

    def test_another_tenant_cannot_read_the_connection(self):
        repo = self._repo()
        repo.register_connection(_connection())
        with pytest.raises(ConnectionNotFoundError):
            repo.get_connection("acme-corp", "hubspot-grasons")


@mock_aws
class TestScopeUnitRepository:
    def _repo(self) -> ScopeUnitRepository:
        _create_table("EdlScopeUnit", "scope_unit_id")
        return ScopeUnitRepository(environment="dev", region_name=_REGION)

    def _partition(self, repo: ScopeUnitRepository) -> None:
        repo.save_partition_profile(
            TenantPartitionProfile(
                tenant_code="evive",
                partition_model=PartitionModel.PARTITIONED,
                partition_kind=PartitionKind.FRANCHISE,
            )
        )

    def test_absent_profile_defaults_to_single(self):
        repo = self._repo()
        profile = repo.get_partition_profile("brand-new")
        assert profile.partition_model is PartitionModel.SINGLE
        assert repo.known_unit_ids("brand-new") == frozenset({IMPLICIT_SCOPE_UNIT_ID})

    def test_a_failed_read_is_not_reported_as_an_absent_profile(self):
        """
        The defect this pins: `_get_item` swallowed ClientError and returned None, which
        `get_partition_profile` read as "no record" and answered `single`. For a partitioned
        tenant that is a match-all predicate on every read surface and a `__tenant__` stamp on
        every row at ingestion — so a DynamoDB throttle silently widened a franchisee boundary.
        """
        repo = self._repo()
        self._partition(repo)

        def _throttle(**_kwargs: object) -> None:
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "GetItem"
            )

        repo._table.get_item = _throttle  # type: ignore[method-assign]
        with pytest.raises(ScopeStoreUnavailableError, match="not a single-partition tenant"):
            repo.get_partition_profile("evive")

    def test_positive_control_the_read_still_succeeds_when_dynamodb_is_healthy(self):
        # Without this, a repository that always raised would pass the test above.
        repo = self._repo()
        self._partition(repo)
        assert repo.get_partition_profile("evive").partition_model is PartitionModel.PARTITIONED

    def test_profile_round_trip(self):
        repo = self._repo()
        self._partition(repo)
        profile = repo.get_partition_profile("evive")
        assert profile.partition_model is PartitionModel.PARTITIONED
        assert profile.partition_kind is PartitionKind.FRANCHISE

    def test_widening_requires_an_approver(self):
        repo = self._repo()
        self._partition(repo)
        with pytest.raises(ScopeWideningNotApprovedError):
            repo.save_partition_profile(TenantPartitionProfile(tenant_code="evive"))

    def test_widening_with_approver_is_audited(self):
        repo = self._repo()
        self._partition(repo)
        repo.save_partition_profile(
            TenantPartitionProfile(tenant_code="evive"), widening_approved_by="ciso@example.com"
        )
        assert repo.get_partition_profile("evive").partition_model is PartitionModel.SINGLE

    def test_scope_unit_requires_a_partitioned_tenant(self):
        repo = self._repo()
        unit = ScopeUnit(
            tenant_code="demo",
            scope_unit_id="franchisee-0001",
            partition_kind=PartitionKind.FRANCHISE,
            display_name="One",
        )
        with pytest.raises(ValueError, match="declare the tenant partitioned"):
            repo.save_scope_unit(unit)

    def test_scope_unit_kind_must_match_the_tenant(self):
        repo = self._repo()
        self._partition(repo)
        unit = ScopeUnit(
            tenant_code="evive",
            scope_unit_id="region-north",
            partition_kind=PartitionKind.REGION,
            display_name="North",
        )
        with pytest.raises(ValueError, match="declares partition_kind"):
            repo.save_scope_unit(unit)

    def test_units_round_trip_and_exclude_the_profile_row(self):
        repo = self._repo()
        self._partition(repo)
        for index in range(3):
            repo.save_scope_unit(
                ScopeUnit(
                    tenant_code="evive",
                    scope_unit_id=f"franchisee-{index:04d}",
                    partition_kind=PartitionKind.FRANCHISE,
                    display_name=f"Franchisee {index}",
                )
            )
        units = repo.list_scope_units("evive")
        assert [u.scope_unit_id for u in units] == [
            "franchisee-0000",
            "franchisee-0001",
            "franchisee-0002",
        ]
        assert repo.known_unit_ids("evive") == frozenset(
            {"franchisee-0000", "franchisee-0001", "franchisee-0002"}
        )

    def test_missing_unit_raises(self):
        repo = self._repo()
        self._partition(repo)
        with pytest.raises(ScopeUnitNotFoundError):
            repo.get_scope_unit("evive", "franchisee-9999")

    def test_get_scope_unit_round_trip(self):
        repo = self._repo()
        self._partition(repo)
        repo.save_scope_unit(
            ScopeUnit(
                tenant_code="evive",
                scope_unit_id="franchisee-0001",
                partition_kind=PartitionKind.FRANCHISE,
                display_name="One",
                external_reference="FR-0001",
            )
        )
        assert repo.get_scope_unit("evive", "franchisee-0001").external_reference == "FR-0001"
