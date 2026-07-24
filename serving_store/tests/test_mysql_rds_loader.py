"""Tests for ServingStoreLoader (MySQL RDS engine)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from serving_store.interfaces.loader_interface import compute_row_hash, reader_username
from serving_store.loaders.mysql_rds_loader import ServingStoreError, ServingStoreLoader

_REGION = "us-east-1"
_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-db-creds"
_TABLE_NAME = "salesforce_account"
_TENANT_CODE = "acme-corp"
_OTHER_TENANT_CODE = "globex-eu"


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


def _make_connection(rowcount: int, fetchall_return: list | None = None):
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.rowcount = rowcount
    mock_cursor.fetchall.return_value = fetchall_return or []
    mock_conn.cursor.return_value = mock_cursor
    return mock_conn, mock_cursor


_HOSTLESS_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:rds!db-managed"


def _make_hostless_creds():
    """An AWS-managed RDS master secret — only username/password, no host/port."""
    return json.dumps({"username": "edl_serving_admin", "password": "dbpass"})


@mock_aws
class TestServingStoreLoaderEndpointInjection:
    def setup_method(self, method=None):
        sm = boto3.client("secretsmanager", region_name=_REGION)
        sm.create_secret(Name=_HOSTLESS_SECRET_ARN, SecretString=_make_hostless_creds())

    def test_endpoint_injected_when_secret_omits_host(self):
        mock_conn, _ = _make_connection(rowcount=2)
        loader = ServingStoreLoader(
            _HOSTLESS_SECRET_ARN,
            _REGION,
            db_host="edl-serving-store-mysql-dev.example.rds.amazonaws.com",
            db_port=3306,
        )

        with patch("pymysql.connect", return_value=mock_conn) as connect_mock:
            loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)

        assert (
            connect_mock.call_args.kwargs["host"]
            == "edl-serving-store-mysql-dev.example.rds.amazonaws.com"
        )
        assert connect_mock.call_args.kwargs["port"] == 3306

    def test_missing_host_and_no_db_host_fails(self):
        mock_conn, _ = _make_connection(rowcount=2)
        loader = ServingStoreLoader(_HOSTLESS_SECRET_ARN, _REGION)  # no db_host supplied

        with patch("pymysql.connect", return_value=mock_conn):
            with pytest.raises(ServingStoreError):
                loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)


@mock_aws
class TestServingStoreLoaderSecretRetrieval:
    def setup_method(self, method=None):
        sm = boto3.client("secretsmanager", region_name=_REGION)
        sm.create_secret(Name=_SECRET_ARN, SecretString=_make_creds())

    def test_invalid_table_name_raises(self):
        loader = ServingStoreLoader(_SECRET_ARN, _REGION)
        with pytest.raises(ValueError, match="Invalid table name"):
            loader.load(_make_records(), "INVALID TABLE NAME", ("account_id",), _TENANT_CODE)

    def test_empty_records_raises(self):
        loader = ServingStoreLoader(_SECRET_ARN, _REGION)
        with pytest.raises(ServingStoreError):
            loader.load([], _TABLE_NAME, ("account_id",), _TENANT_CODE)

    def test_missing_tenant_code_raises(self):
        loader = ServingStoreLoader(_SECRET_ARN, _REGION)
        with pytest.raises(ValueError, match="tenant_code"):
            loader.load(_make_records(), _TABLE_NAME, ("account_id",), "")

    def test_invalid_tenant_code_raises(self):
        loader = ServingStoreLoader(_SECRET_ARN, _REGION)
        with pytest.raises(ValueError, match="tenant_code"):
            loader.load(_make_records(), _TABLE_NAME, ("account_id",), "Not_Valid!")

    def test_successful_load_with_mocked_connection(self):
        mock_conn, _ = _make_connection(rowcount=2)
        loader = ServingStoreLoader(_SECRET_ARN, _REGION)

        with patch(
            "serving_store.loaders.mysql_rds_loader.pymysql.connect", return_value=mock_conn
        ):
            result = loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)

        assert result.records_loaded == 2
        assert result.records_skipped == 0
        assert result.table_name == _TABLE_NAME
        assert result.database_name == "acme_corp"
        mock_conn.commit.assert_called()

    def test_two_tenants_produce_distinct_databases_not_tables(self):
        mock_conn, _ = _make_connection(rowcount=2)
        loader = ServingStoreLoader(_SECRET_ARN, _REGION)

        with patch(
            "serving_store.loaders.mysql_rds_loader.pymysql.connect", return_value=mock_conn
        ):
            result_a = loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)
            result_b = loader.load(
                _make_records(), _TABLE_NAME, ("account_id",), _OTHER_TENANT_CODE
            )

        assert result_a.database_name != result_b.database_name
        assert result_a.database_name == "acme_corp"
        assert result_b.database_name == "globex_eu"
        assert result_a.table_name == result_b.table_name == _TABLE_NAME

    def test_connection_error_raises_serving_store_error(self):
        loader = ServingStoreLoader(_SECRET_ARN, _REGION)
        with patch(
            "serving_store.loaders.mysql_rds_loader.pymysql.connect",
            side_effect=Exception("Connection refused"),
        ):
            with pytest.raises(ServingStoreError):
                loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)

    def test_missing_secret_raises_serving_store_error(self):
        loader = ServingStoreLoader(
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:nonexistent",
            _REGION,
        )
        with pytest.raises(ServingStoreError, match="Failed to retrieve database credentials"):
            loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)

    def test_reader_credential_provisioned_and_scoped_to_tenant_database(self):
        mock_conn, mock_cursor = _make_connection(rowcount=2)
        loader = ServingStoreLoader(_SECRET_ARN, _REGION)

        with patch(
            "serving_store.loaders.mysql_rds_loader.pymysql.connect", return_value=mock_conn
        ):
            loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)

        executed_sql = " ".join(str(c.args[0]) for c in mock_cursor.execute.call_args_list)
        assert "CREATE USER IF NOT EXISTS" in executed_sql
        assert "GRANT SELECT ON `acme_corp`.*" in executed_sql

        sm = boto3.client("secretsmanager", region_name=_REGION)
        secret = json.loads(
            sm.get_secret_value(
                SecretId="edl/serving-store/acme-corp/mysql_rds/reader-credentials"
            )["SecretString"]
        )
        assert secret["database"] == "acme_corp"
        assert secret["username"].endswith(tuple("0123456789abcdef"))  # deterministic hash suffix

    def test_reader_password_stable_across_loads(self):
        mock_conn, _ = _make_connection(rowcount=2)
        loader = ServingStoreLoader(_SECRET_ARN, _REGION)

        with patch(
            "serving_store.loaders.mysql_rds_loader.pymysql.connect", return_value=mock_conn
        ):
            loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)
            loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)

        sm = boto3.client("secretsmanager", region_name=_REGION)
        secret_id = "edl/serving-store/acme-corp/mysql_rds/reader-credentials"
        # create_secret would fail on a second call for the same name if the
        # loader tried to regenerate it — reaching here proves reuse.
        secret = json.loads(sm.get_secret_value(SecretId=secret_id)["SecretString"])
        assert secret["password"]

    def test_second_load_skips_unchanged_rows_and_upserts_changed_ones(self):
        records = _make_records()
        unchanged_hash = compute_row_hash(records[0], ["account_id", "name", "revenue"])

        mock_conn, mock_cursor = _make_connection(rowcount=2, fetchall_return=[])
        loader = ServingStoreLoader(_SECRET_ARN, _REGION)

        with patch(
            "serving_store.loaders.mysql_rds_loader.pymysql.connect", return_value=mock_conn
        ):
            first = loader.load(records, _TABLE_NAME, ("account_id",), _TENANT_CODE)
            assert first.records_loaded == 2

            # Second run: DB already has record 001 unchanged; 002 is new/changed.
            mock_cursor.fetchall.return_value = [{"account_id": "001", "_row_hash": unchanged_hash}]
            mock_cursor.rowcount = 1
            second = loader.load(records, _TABLE_NAME, ("account_id",), _TENANT_CODE)

        assert second.records_loaded == 1
        assert second.records_skipped == 1


class TestMysqlTypeInference:
    """Test the MySQL type inference helper."""

    def test_int_maps_to_bigint(self):
        from serving_store.loaders.mysql_rds_loader import _infer_mysql_type

        assert _infer_mysql_type(42) == "BIGINT"

    def test_float_maps_to_double(self):
        from serving_store.loaders.mysql_rds_loader import _infer_mysql_type

        assert _infer_mysql_type(3.14) == "DOUBLE"

    def test_bool_maps_to_tinyint(self):
        from serving_store.loaders.mysql_rds_loader import _infer_mysql_type

        assert _infer_mysql_type(True) == "TINYINT(1)"

    def test_str_maps_to_text(self):
        from serving_store.loaders.mysql_rds_loader import _infer_mysql_type

        assert _infer_mysql_type("hello") == "TEXT"

    def test_dict_maps_to_json(self):
        from serving_store.loaders.mysql_rds_loader import _infer_mysql_type

        assert _infer_mysql_type({"k": "v"}) == "JSON"


class TestReaderUsername:
    def test_deterministic_and_within_mysql_length_limit(self):
        username = reader_username(_TENANT_CODE, 32)
        assert username == reader_username(_TENANT_CODE, 32)
        assert len(username) <= 32

    def test_long_tenant_code_still_within_limit(self):
        long_tenant = "a" * 47  # near TENANT_CODE_PATTERN's 48-char max
        assert len(reader_username(long_tenant, 32)) <= 32
