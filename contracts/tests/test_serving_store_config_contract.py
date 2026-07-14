"""Tests for the ServingStoreLoadConfig configuration contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from contracts.serving_store_config_contract import ServingStoreEngine, ServingStoreLoadConfig


def _base() -> dict[str, object]:
    return {
        "tenant_code": "acme-corp",
        "entity_type": "company",
        "table_name": "salesforce_account",
        "primary_keys": ("account_id",),
        "secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
        "region_name": "us-east-1",
    }


class TestServingStoreLoadConfigValidConstruction:
    def test_valid_config_with_defaults(self) -> None:
        config = ServingStoreLoadConfig(**_base())
        assert config.target_engine == ServingStoreEngine.MYSQL_RDS
        assert config.enabled is True
        assert config.primary_keys == ("account_id",)

    def test_composite_primary_keys(self) -> None:
        config = ServingStoreLoadConfig(**{**_base(), "primary_keys": ("tenant_id", "record_id")})
        assert config.primary_keys == ("tenant_id", "record_id")

    def test_connection_database_defaults_to_none(self) -> None:
        config = ServingStoreLoadConfig(**_base())
        assert config.connection_database is None

    @pytest.mark.parametrize(
        "engine", [ServingStoreEngine.POSTGRESQL, ServingStoreEngine.SQLSERVER,
                   ServingStoreEngine.AZURE_SQL]
    )
    def test_non_mysql_engines_accept_connection_database(self, engine) -> None:
        config = ServingStoreLoadConfig(
            **{**_base(), "target_engine": engine, "connection_database": "edl_serving"}
        )
        assert config.target_engine == engine
        assert config.connection_database == "edl_serving"

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ServingStoreLoadConfig(**{**_base(), "unexpected_field": "x"})

    def test_frozen_model_rejects_mutation(self) -> None:
        config = ServingStoreLoadConfig(**_base())
        with pytest.raises(ValidationError):
            config.enabled = False  # type: ignore[misc]


class TestServingStoreLoadConfigValidation:
    def test_invalid_tenant_code_rejected(self) -> None:
        with pytest.raises(ValidationError, match="tenant code format"):
            ServingStoreLoadConfig(**{**_base(), "tenant_code": "Not_Valid!"})

    def test_invalid_entity_type_rejected(self) -> None:
        with pytest.raises(ValidationError, match="entity type format"):
            ServingStoreLoadConfig(**{**_base(), "entity_type": "Invalid Entity"})

    @pytest.mark.parametrize("entity_type", ["ar_invoice", "ap_bill"])
    def test_underscore_entity_type_accepted(self, entity_type: str) -> None:
        config = ServingStoreLoadConfig(**{**_base(), "entity_type": entity_type})
        assert config.entity_type == entity_type

    def test_unsafe_table_name_rejected(self) -> None:
        with pytest.raises(ValidationError, match="safe SQL identifier"):
            ServingStoreLoadConfig(**{**_base(), "table_name": "bad name; DROP TABLE x"})

    def test_unsafe_primary_key_rejected(self) -> None:
        with pytest.raises(ValidationError, match="safe SQL identifier"):
            ServingStoreLoadConfig(**{**_base(), "primary_keys": ("ok_col", "bad col")})

    def test_empty_primary_keys_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ServingStoreLoadConfig(**{**_base(), "primary_keys": ()})

    def test_unsafe_connection_database_rejected(self) -> None:
        with pytest.raises(ValidationError, match="safe SQL identifier"):
            ServingStoreLoadConfig(**{**_base(), "connection_database": "bad; DROP TABLE x"})
