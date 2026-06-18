"""Tests for ServingStoreLoader — Phase 8."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from transformation.serving_store_loader import ServingStoreError, ServingStoreLoader

_REGION = "us-east-1"
_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-db-creds"  # noqa: S105
_DB_NAME = "analytics_db"
_TABLE_NAME = "salesforce_account"


def _make_creds():
    return json.dumps(
        {
            "host": "test-rds.us-east-1.rds.amazonaws.com",
            "port": "3306",
            "username": "dbuser",
            "password": "dbpass",
        }
    )


def _make_records():
    return [
        {"account_id": "001", "name": "Acme Corp", "revenue": 1_000_000},
        {"account_id": "002", "name": "Beta Ltd", "revenue": 500_000},
    ]


@mock_aws
class TestServingStoreLoaderSecretRetrieval:
    def setup_method(self, method=None):
        sm = boto3.client("secretsmanager", region_name=_REGION)
        sm.create_secret(Name=_SECRET_ARN, SecretString=_make_creds())

    def test_invalid_table_name_raises(self):
        loader = ServingStoreLoader(_SECRET_ARN, _DB_NAME, _REGION)
        with pytest.raises(ValueError, match="Invalid table name"):
            loader.load(_make_records(), "INVALID TABLE NAME", ("account_id",))

    def test_empty_records_raises(self):
        loader = ServingStoreLoader(_SECRET_ARN, _DB_NAME, _REGION)
        with pytest.raises(ServingStoreError):
            loader.load([], _TABLE_NAME, ("account_id",))

    def test_successful_load_with_mocked_connection(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.rowcount = 2
        mock_conn.cursor.return_value = mock_cursor

        loader = ServingStoreLoader(_SECRET_ARN, _DB_NAME, _REGION)

        with patch("transformation.serving_store_loader.pymysql.connect", return_value=mock_conn):
            result = loader.load(_make_records(), _TABLE_NAME, ("account_id",))

        assert result.records_loaded == 2
        assert result.table_name == _TABLE_NAME
        mock_conn.commit.assert_called_once()

    def test_connection_error_raises_serving_store_error(self):
        loader = ServingStoreLoader(_SECRET_ARN, _DB_NAME, _REGION)
        with patch(
            "transformation.serving_store_loader.pymysql.connect",
            side_effect=Exception("Connection refused"),
        ):
            with pytest.raises(ServingStoreError):
                loader.load(_make_records(), _TABLE_NAME, ("account_id",))

    def test_missing_secret_raises_serving_store_error(self):
        loader = ServingStoreLoader(
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:nonexistent",
            _DB_NAME,
            _REGION,
        )
        with pytest.raises(ServingStoreError, match="Failed to retrieve database credentials"):
            loader.load(_make_records(), _TABLE_NAME, ("account_id",))


class TestMysqlTypeInference:
    """Test the MySQL type inference helper."""

    def test_int_maps_to_bigint(self):
        from transformation.serving_store_loader import _infer_mysql_type

        assert _infer_mysql_type(42) == "BIGINT"

    def test_float_maps_to_double(self):
        from transformation.serving_store_loader import _infer_mysql_type

        assert _infer_mysql_type(3.14) == "DOUBLE"

    def test_bool_maps_to_tinyint(self):
        from transformation.serving_store_loader import _infer_mysql_type

        assert _infer_mysql_type(True) == "TINYINT(1)"

    def test_str_maps_to_text(self):
        from transformation.serving_store_loader import _infer_mysql_type

        assert _infer_mysql_type("hello") == "TEXT"

    def test_dict_maps_to_json(self):
        from transformation.serving_store_loader import _infer_mysql_type

        assert _infer_mysql_type({"k": "v"}) == "JSON"
