"""
Tests for EntityTypeRegistryClient (ARCH-2).

Covers:
  - Fallback to module-level constants when no DynamoDB record exists
    (backward compatibility for the default tenant / unmigrated entities).
  - Tenant-specific registration overrides the fallback for that tenant only.
  - Two tenants can register different types for the same entity_id without
    colliding (each tenant's items live under its own PK).
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from conftest import RESOURCE_NAME_ENVIRONMENT
from entity_resolution.entity_type_registry import (
    ENTITY_ID_TO_TYPE,
    ENTITY_TYPE_PK_FIELD,
    ENTITY_TYPE_SOURCES,
    EntityTypeRecord,
    EntityTypeRegistryClient,
)

_REGION = "us-east-1"
_ENV = "dev"
_TABLE = RESOURCE_NAME_ENVIRONMENT["ENTITY_TYPE_REGISTRY_TABLE"]


def _create_table() -> None:
    dynamodb = boto3.resource("dynamodb", region_name=_REGION)
    dynamodb.create_table(
        TableName=_TABLE,
        KeySchema=[
            {"AttributeName": "tenant_code", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "tenant_code", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _client() -> EntityTypeRegistryClient:
    return EntityTypeRegistryClient(environment=_ENV, region_name=_REGION)


class TestFallbackToConstants:
    @mock_aws
    def test_get_entity_type_falls_back_when_no_record(self) -> None:
        _create_table()
        client = _client()
        expected = ENTITY_ID_TO_TYPE["salesforce-account"]
        assert client.get_entity_type("salesforce-account") == expected

    @mock_aws
    def test_get_pk_field_falls_back_when_no_record(self) -> None:
        _create_table()
        client = _client()
        assert client.get_pk_field("company") == ENTITY_TYPE_PK_FIELD["company"]

    @mock_aws
    def test_get_contributing_sources_falls_back_when_no_record(self) -> None:
        _create_table()
        client = _client()
        assert client.get_contributing_sources("company") == ENTITY_TYPE_SOURCES["company"]

    @mock_aws
    def test_unknown_entity_id_returns_none(self) -> None:
        _create_table()
        client = _client()
        assert client.get_entity_type("does-not-exist") is None

    @mock_aws
    def test_unknown_entity_type_contributing_sources_returns_empty_list(self) -> None:
        _create_table()
        client = _client()
        assert client.get_contributing_sources("does-not-exist") == []


class TestTenantRegistration:
    @mock_aws
    def test_registered_entity_type_overrides_fallback_for_that_tenant(self) -> None:
        _create_table()
        client = _client()
        client.register_entity_type(
            EntityTypeRecord(
                entity_id="acme-custom-entity",
                entity_type="custom_widget",
                pk_field="widget_id",
                contributing_sources=(("acme-source", "acme-custom-entity"),),
            ),
            tenant_code="acme-corp",
        )

        entity_type = client.get_entity_type("acme-custom-entity", tenant_code="acme-corp")
        assert entity_type == "custom_widget"
        assert client.get_pk_field("custom_widget", tenant_code="acme-corp") == "widget_id"
        assert client.get_contributing_sources("custom_widget", tenant_code="acme-corp") == [
            ("acme-source", "acme-custom-entity")
        ]

    @mock_aws
    def test_default_tenant_unaffected_by_other_tenants_registration(self) -> None:
        _create_table()
        client = _client()
        client.register_entity_type(
            EntityTypeRecord(
                entity_id="salesforce-account",
                entity_type="overridden_type",
                pk_field="overridden_pk",
                contributing_sources=(),
            ),
            tenant_code="acme-corp",
        )

        default_expected = ENTITY_ID_TO_TYPE["salesforce-account"]
        assert client.get_entity_type("salesforce-account") == default_expected
        acme_type = client.get_entity_type("salesforce-account", tenant_code="acme-corp")
        assert acme_type == "overridden_type"

    @mock_aws
    def test_two_tenants_can_register_different_types_for_same_entity_id(self) -> None:
        _create_table()
        client = _client()
        client.register_entity_type(
            EntityTypeRecord(
                entity_id="shared-entity-id",
                entity_type="type_a",
                pk_field="id_a",
                contributing_sources=(),
            ),
            tenant_code="acme-corp",
        )
        client.register_entity_type(
            EntityTypeRecord(
                entity_id="shared-entity-id",
                entity_type="type_b",
                pk_field="id_b",
                contributing_sources=(),
            ),
            tenant_code="globex-eu",
        )

        assert client.get_entity_type("shared-entity-id", tenant_code="acme-corp") == "type_a"
        assert client.get_entity_type("shared-entity-id", tenant_code="globex-eu") == "type_b"

    def test_invalid_tenant_code_raises(self) -> None:
        client = _client()
        with pytest.raises(ValueError, match="tenant_code"):
            client.get_entity_type("salesforce-account", tenant_code="BAD_CODE")

    def test_constructor_requires_environment(self) -> None:
        with pytest.raises(ValueError, match="environment"):
            EntityTypeRegistryClient(environment="", region_name=_REGION)


class TestDeregisterEntityType:
    @mock_aws
    def test_deregister_reverts_to_fallback(self) -> None:
        _create_table()
        client = _client()
        client.register_entity_type(
            EntityTypeRecord(
                entity_id="salesforce-account",
                entity_type="overridden_type",
                pk_field="overridden_pk",
                contributing_sources=(),
            ),
            tenant_code="acme-corp",
        )
        overridden = client.get_entity_type("salesforce-account", tenant_code="acme-corp")
        assert overridden == "overridden_type"

        client.deregister_entity_type("salesforce-account", tenant_code="acme-corp")

        default_expected = ENTITY_ID_TO_TYPE["salesforce-account"]
        reverted = client.get_entity_type("salesforce-account", tenant_code="acme-corp")
        assert reverted == default_expected

    @mock_aws
    def test_deregister_unregistered_entity_id_is_a_no_op(self) -> None:
        _create_table()
        client = _client()
        client.deregister_entity_type("never-registered", tenant_code="acme-corp")
        assert client.get_entity_type("never-registered", tenant_code="acme-corp") is None

    @mock_aws
    def test_deregister_does_not_affect_other_tenants(self) -> None:
        _create_table()
        client = _client()
        record = EntityTypeRecord(
            entity_id="shared-entity-id",
            entity_type="type_a",
            pk_field="id_a",
            contributing_sources=(),
        )
        client.register_entity_type(record, tenant_code="acme-corp")
        client.register_entity_type(record, tenant_code="globex-eu")

        client.deregister_entity_type("shared-entity-id", tenant_code="acme-corp")

        assert client.get_entity_type("shared-entity-id", tenant_code="acme-corp") is None
        assert client.get_entity_type("shared-entity-id", tenant_code="globex-eu") == "type_a"

    @mock_aws
    def test_deregister_leaves_entity_type_descriptor_item_intact(self) -> None:
        _create_table()
        client = _client()
        client.register_entity_type(
            EntityTypeRecord(
                entity_id="acme-custom-entity",
                entity_type="custom_widget",
                pk_field="widget_id",
                contributing_sources=(("acme-source", "acme-custom-entity"),),
            ),
            tenant_code="acme-corp",
        )

        client.deregister_entity_type("acme-custom-entity", tenant_code="acme-corp")

        assert client.get_entity_type("acme-custom-entity", tenant_code="acme-corp") is None
        assert client.get_pk_field("custom_widget", tenant_code="acme-corp") == "widget_id"


class TestListKnownEntityTypes:
    def test_returns_sorted_fallback_entity_types(self) -> None:
        client = _client()
        assert client.list_known_entity_types() == sorted(ENTITY_TYPE_PK_FIELD)
