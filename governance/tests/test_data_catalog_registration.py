"""Tests for DataCatalogRegistrationClient — Phase 9."""

from __future__ import annotations

import pytest
from moto import mock_aws

from governance.data_catalog_registration import (
    CatalogDatasetSpec,
    CatalogRegistrationError,
    DataCatalogRegistrationClient,
    DataLayer,
    DatasetNotFoundError,
)

_REGION = "us-east-1"


def _make_spec(
    db="edl_analytics",
    table="salesforce_account_analytics",
    layer=DataLayer.ANALYTICS,
    schema=({"Name": "account_id", "Type": "string"}, {"Name": "name", "Type": "string"}),
):
    return CatalogDatasetSpec(
        database_name=db,
        table_name=table,
        s3_location="s3://test-analytics/analytics/customer/salesforce-account/",
        data_layer=layer,
        owner="data-eng-team",
        data_classification="internal",
        retention_days=365,
        source_lineage=("s3://raw/salesforce/",),
        partition_keys=("analytics_date",),
        schema=schema,
        description="Salesforce account analytics dataset",
    )


@mock_aws
class TestDataCatalogRegistrationClient:
    def setup_method(self, method=None):
        self.client = DataCatalogRegistrationClient(_REGION)

    def test_register_creates_database_and_table(self):
        spec = _make_spec()
        result = self.client.register_dataset(spec)
        assert result.operation == "created"
        assert result.database_name == "edl_analytics"
        assert result.table_name == "salesforce_account_analytics"

    def test_register_is_idempotent(self):
        spec = _make_spec()
        r1 = self.client.register_dataset(spec)
        r2 = self.client.register_dataset(spec)
        assert r1.operation == "created"
        assert r2.operation == "updated"

    def test_get_dataset_returns_table(self):
        spec = _make_spec()
        self.client.register_dataset(spec)
        table = self.client.get_dataset("edl_analytics", "salesforce_account_analytics")
        assert table["Name"] == "salesforce_account_analytics"

    def test_get_nonexistent_dataset_raises(self):
        with pytest.raises(DatasetNotFoundError):
            self.client.get_dataset("edl_analytics", "nonexistent_table")

    def test_list_datasets_returns_table_names(self):
        self.client.register_dataset(_make_spec(table="table_a"))
        self.client.register_dataset(_make_spec(table="table_b"))
        tables = self.client.list_datasets("edl_analytics")
        assert "table_a" in tables
        assert "table_b" in tables

    def test_list_datasets_nonexistent_db_returns_empty(self):
        tables = self.client.list_datasets("nonexistent_db")
        assert tables == []

    def test_invalid_database_name_raises(self):
        spec = _make_spec(db="INVALID DB NAME")
        with pytest.raises(CatalogRegistrationError, match="Invalid database name"):
            self.client.register_dataset(spec)

    def test_invalid_table_name_raises(self):
        spec = _make_spec(table="INVALID TABLE NAME")
        with pytest.raises(CatalogRegistrationError, match="Invalid table name"):
            self.client.register_dataset(spec)

    def test_partition_keys_included_in_table_definition(self):
        spec = _make_spec()
        self.client.register_dataset(spec)
        table = self.client.get_dataset("edl_analytics", "salesforce_account_analytics")
        pk_names = [k["Name"] for k in table.get("PartitionKeys", [])]
        assert "analytics_date" in pk_names

    def test_metadata_parameters_stored(self):
        spec = _make_spec()
        self.client.register_dataset(spec)
        table = self.client.get_dataset("edl_analytics", "salesforce_account_analytics")
        params = table.get("Parameters", {})
        assert params.get("owner") == "data-eng-team"
        assert params.get("data_classification") == "internal"
        assert params.get("data_layer") == "analytics"

    def test_all_data_layers_supported(self):
        for layer in DataLayer:
            spec = _make_spec(table=f"test_{layer.value}", layer=layer)
            result = self.client.register_dataset(spec)
            assert result.operation in ("created", "updated")
