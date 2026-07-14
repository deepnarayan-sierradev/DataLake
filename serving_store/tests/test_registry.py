"""Tests for the serving store loader registry."""

from __future__ import annotations

import pytest

from serving_store.interfaces.loader_interface import ServingStoreLoaderInterface
from serving_store.registry import ServingStoreLoaderRegistry


class _StubLoader(ServingStoreLoaderInterface):
    def _connect(self, credentials, connection_database):
        raise NotImplementedError

    def _ensure_tenant_container(self, connection, container_name):
        raise NotImplementedError

    def _select_container(self, connection, container_name):
        raise NotImplementedError

    def _provision_reader_credential(self, connection, tenant_code, container_name, writer_creds):
        raise NotImplementedError

    def _ensure_table(self, connection, container_name, table_name, columns, sample, primary_keys):
        raise NotImplementedError

    def _read_existing_hashes(
        self, connection, container_name, table_name, primary_keys, pk_tuples
    ):
        raise NotImplementedError

    def _bulk_upsert(self, connection, container_name, table_name, columns, primary_keys, changed):
        raise NotImplementedError


class TestServingStoreLoaderRegistry:
    def test_register_and_resolve(self):
        registry = ServingStoreLoaderRegistry()
        registry.register("stub")(_StubLoader)

        loader = registry.resolve("stub", secret_arn="arn:x", region_name="us-east-1")
        assert isinstance(loader, _StubLoader)

    def test_duplicate_registration_raises(self):
        registry = ServingStoreLoaderRegistry()
        registry.register("stub")(_StubLoader)
        with pytest.raises(ValueError, match="already registered"):
            registry.register("stub")(_StubLoader)

    def test_resolve_unknown_engine_raises(self):
        registry = ServingStoreLoaderRegistry()
        with pytest.raises(KeyError, match="No serving store loader registered"):
            registry.resolve("unknown-engine", secret_arn="arn:x", region_name="us-east-1")

    def test_real_engines_are_registered_via_module_import(self):
        import serving_store.loaders.mysql_rds_loader
        import serving_store.loaders.postgresql_loader
        import serving_store.loaders.sqlserver_loader  # noqa: F401
        from serving_store.registry import serving_store_registry

        for engine_id in ("mysql_rds", "postgresql", "sqlserver", "azure_sql"):
            loader = serving_store_registry.resolve(
                engine_id, secret_arn="arn:x", region_name="us-east-1"
            )
            assert isinstance(loader, ServingStoreLoaderInterface)
