"""
Tests for per-connector connector_params Pydantic models (§2.2).

Validates that each adapter's params model correctly accepts valid inputs,
rejects invalid inputs, and forbids unexpected extra keys.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from connector_runtime.adapters.mysql_rds.mysql_rds_params import MySqlRdsConnectorParams
from connector_runtime.adapters.netsuite.netsuite_params import NetSuiteConnectorParams
from connector_runtime.adapters.sage.sage_params import SageConnectorParams
from connector_runtime.adapters.salesforce.salesforce_params import SalesforceConnectorParams


class TestSalesforceConnectorParams:
    def test_valid_object_name(self) -> None:
        p = SalesforceConnectorParams.model_validate({"object_name": "Account"})
        assert p.object_name == "Account"

    def test_custom_object_name(self) -> None:
        p = SalesforceConnectorParams.model_validate({"object_name": "MyCustomObject__c"})
        assert p.object_name == "MyCustomObject__c"

    def test_missing_object_name_raises(self) -> None:
        with pytest.raises(ValidationError):
            SalesforceConnectorParams.model_validate({})

    def test_extra_key_raises(self) -> None:
        with pytest.raises(ValidationError):
            SalesforceConnectorParams.model_validate({"object_name": "Account", "extra": "bad"})

    @pytest.mark.parametrize(
        "bad_name",
        [
            "1BadObject",  # starts with digit
            "has space",  # space
            "has-hyphen",  # hyphen
            "",  # empty
            "a" * 81,  # too long
        ],
    )
    def test_invalid_object_name_raises(self, bad_name: str) -> None:
        with pytest.raises(ValidationError):
            SalesforceConnectorParams.model_validate({"object_name": bad_name})


class TestMySqlRdsConnectorParams:
    def test_valid_table_name(self) -> None:
        p = MySqlRdsConnectorParams.model_validate({"table_name": "Contracts"})
        assert p.table_name == "Contracts"

    def test_table_name_with_underscores(self) -> None:
        p = MySqlRdsConnectorParams.model_validate({"table_name": "order_items"})
        assert p.table_name == "order_items"

    def test_missing_table_name_raises(self) -> None:
        with pytest.raises(ValidationError):
            MySqlRdsConnectorParams.model_validate({})

    def test_extra_key_raises(self) -> None:
        with pytest.raises(ValidationError):
            MySqlRdsConnectorParams.model_validate(
                {"table_name": "orders", "injection": "DROP TABLE"}
            )

    @pytest.mark.parametrize(
        "bad_name",
        [
            "1bad",  # starts with digit
            "has-hyphen",  # hyphen not allowed
            "has space",  # space
            "",  # empty
            "a" * 65,  # too long
        ],
    )
    def test_invalid_table_name_raises(self, bad_name: str) -> None:
        with pytest.raises(ValidationError):
            MySqlRdsConnectorParams.model_validate({"table_name": bad_name})


class TestNetSuiteConnectorParams:
    def test_valid_record_type(self) -> None:
        p = NetSuiteConnectorParams.model_validate({"record_type": "customer"})
        assert p.record_type == "customer"
        assert p.page_size == 10_000  # default

    def test_custom_page_size(self) -> None:
        p = NetSuiteConnectorParams.model_validate({"record_type": "customer", "page_size": 5000})
        assert p.page_size == 5000

    def test_missing_record_type_raises(self) -> None:
        with pytest.raises(ValidationError):
            NetSuiteConnectorParams.model_validate({})

    def test_page_size_above_max_raises(self) -> None:
        with pytest.raises(ValidationError):
            NetSuiteConnectorParams.model_validate({"record_type": "customer", "page_size": 10_001})

    def test_page_size_zero_raises(self) -> None:
        with pytest.raises(ValidationError):
            NetSuiteConnectorParams.model_validate({"record_type": "customer", "page_size": 0})

    def test_extra_key_raises(self) -> None:
        with pytest.raises(ValidationError):
            NetSuiteConnectorParams.model_validate({"record_type": "customer", "bad_key": "value"})

    @pytest.mark.parametrize(
        "bad_name",
        [
            "1bad",
            "has-hyphen",
            "has space",
            "",
        ],
    )
    def test_invalid_record_type_raises(self, bad_name: str) -> None:
        with pytest.raises(ValidationError):
            NetSuiteConnectorParams.model_validate({"record_type": bad_name})


class TestSageConnectorParams:
    def test_valid_intacct_params(self) -> None:
        p = SageConnectorParams.model_validate(
            {
                "sage_product": "intacct",
                "object_path": "accounts-receivable/customer",
            }
        )
        assert p.sage_product == "intacct"
        assert p.object_path == "accounts-receivable/customer"

    def test_valid_x3_params(self) -> None:
        p = SageConnectorParams.model_validate(
            {
                "sage_product": "x3",
                "object_path": "BPCUSTOMER",
            }
        )
        assert p.sage_product == "x3"

    def test_missing_sage_product_raises(self) -> None:
        with pytest.raises(ValidationError):
            SageConnectorParams.model_validate({"object_path": "some/path"})

    def test_missing_object_path_raises(self) -> None:
        with pytest.raises(ValidationError):
            SageConnectorParams.model_validate({"sage_product": "intacct"})

    def test_extra_key_raises(self) -> None:
        with pytest.raises(ValidationError):
            SageConnectorParams.model_validate(
                {
                    "sage_product": "intacct",
                    "object_path": "foo/bar",
                    "injection": "../../../etc/passwd",
                }
            )

    @pytest.mark.parametrize(
        "bad_path",
        [
            "../etc/passwd",  # path traversal
            "/absolute/path",  # leading slash
            "has space",  # space
            "",  # empty
        ],
    )
    def test_unsafe_object_path_raises(self, bad_path: str) -> None:
        with pytest.raises(ValidationError):
            SageConnectorParams.model_validate(
                {
                    "sage_product": "intacct",
                    "object_path": bad_path,
                }
            )

    def test_sage_product_uppercase_raises(self) -> None:
        with pytest.raises(ValidationError):
            SageConnectorParams.model_validate(
                {
                    "sage_product": "Intacct",  # uppercase not allowed
                    "object_path": "some/path",
                }
            )
