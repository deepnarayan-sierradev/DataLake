"""Tests for SqlServerLoader (SQL Server / Azure SQL engine)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from serving_store.interfaces.loader_interface import compute_row_hash
from serving_store.loaders.sqlserver_loader import ServingStoreError, SqlServerLoader
from serving_store.registry import serving_store_registry

_REGION = "us-east-1"
_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-db-creds"
_TABLE_NAME = "salesforce_account"
_TENANT_CODE = "acme-corp"
_OTHER_TENANT_CODE = "globex-eu"


def _make_creds():
    return json.dumps(
        {
            "host": "test-sqlserver.us-east-1.rds.amazonaws.com",
            "port": "1433",
            "username": "dbuser",
            "password": "dbpass",
        }
    )


def _make_records():
    return [
        {"account_id": "001", "name": "Acme Corp", "revenue": 1_000_000},
        {"account_id": "002", "name": "Beta Ltd", "revenue": 500_000},
    ]


def _fetchone_sequence(
    schema_exists=False, login_exists=False, user_exists=False, table_exists=False
):
    """The 5 sequential fetchone() calls one load() makes: schema, DB_NAME(), login,
    user, table existence checks — in that order."""
    return [
        {"exists": 1} if schema_exists else None,
        {"db_name": "edl_serving"},
        {"exists": 1} if login_exists else None,
        {"exists": 1} if user_exists else None,
        {"exists": 1} if table_exists else None,
    ]


def _make_connection(fetchall_return: list | None = None, **exists_flags):
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchall.return_value = fetchall_return or []
    mock_cursor.fetchone.side_effect = _fetchone_sequence(**exists_flags)
    mock_conn.cursor.return_value = mock_cursor
    return mock_conn, mock_cursor


def _make_bootstrap_connection(db_exists: bool = False):
    """A master-connection mock whose single fetchone answers the sys.databases check."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchone.side_effect = [{"exists": 1} if db_exists else None]
    mock_conn.cursor.return_value = mock_cursor
    return mock_conn, mock_cursor


@mock_aws
class TestSqlServerLoader:
    def setup_method(self, method=None):
        sm = boto3.client("secretsmanager", region_name=_REGION)
        sm.create_secret(Name=_SECRET_ARN, SecretString=_make_creds())

    def test_invalid_table_name_raises(self):
        loader = SqlServerLoader(_SECRET_ARN, _REGION)
        with pytest.raises(ValueError, match="Invalid table name"):
            loader.load(_make_records(), "INVALID TABLE NAME", ("account_id",), _TENANT_CODE)

    def test_empty_records_raises(self):
        loader = SqlServerLoader(_SECRET_ARN, _REGION)
        with pytest.raises(ServingStoreError):
            loader.load([], _TABLE_NAME, ("account_id",), _TENANT_CODE)

    def test_successful_load_with_mocked_connection(self):
        mock_conn, _ = _make_connection()
        loader = SqlServerLoader(_SECRET_ARN, _REGION)

        with patch("pymssql.connect", return_value=mock_conn):
            result = loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)

        assert result.records_loaded == 2
        assert result.records_skipped == 0
        assert result.table_name == _TABLE_NAME
        assert result.database_name == "acme_corp"

    def test_two_tenants_produce_distinct_schemas(self):
        loader = SqlServerLoader(_SECRET_ARN, _REGION)

        mock_conn_a, _ = _make_connection()
        with patch("pymssql.connect", return_value=mock_conn_a):
            result_a = loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)

        mock_conn_b, _ = _make_connection()
        with patch("pymssql.connect", return_value=mock_conn_b):
            result_b = loader.load(
                _make_records(), _TABLE_NAME, ("account_id",), _OTHER_TENANT_CODE
            )

        assert result_a.database_name != result_b.database_name
        assert result_a.database_name == "acme_corp"
        assert result_b.database_name == "globex_eu"

    def test_connection_error_raises_serving_store_error(self):
        loader = SqlServerLoader(_SECRET_ARN, _REGION)
        with patch(
            "pymssql.connect",
            side_effect=Exception("Connection refused"),
        ):
            with pytest.raises(ServingStoreError):
                loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)

    def test_reader_login_scoped_to_schema_not_db_datareader(self):
        mock_conn, mock_cursor = _make_connection()
        loader = SqlServerLoader(_SECRET_ARN, _REGION)

        with patch("pymssql.connect", return_value=mock_conn):
            loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)

        executed_sql = " ".join(str(c.args[0]) for c in mock_cursor.execute.call_args_list)
        assert "CREATE LOGIN" in executed_sql
        assert "CREATE USER" in executed_sql
        assert "GRANT SELECT ON SCHEMA::[acme_corp]" in executed_sql
        assert "db_datareader" not in executed_sql

    def test_existing_login_and_user_are_not_recreated(self):
        mock_conn, mock_cursor = _make_connection(login_exists=True, user_exists=True)
        loader = SqlServerLoader(_SECRET_ARN, _REGION)

        with patch("pymssql.connect", return_value=mock_conn):
            loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)

        executed_sql = " ".join(str(c.args[0]) for c in mock_cursor.execute.call_args_list)
        assert "CREATE LOGIN" not in executed_sql
        assert "CREATE USER" not in executed_sql

    def test_platform_sqlserver_bootstraps_connection_database(self):
        # Resolved via the registry so engine_id == "sqlserver" (platform-provisioned).
        admin_conn, admin_cursor = _make_bootstrap_connection(db_exists=False)
        main_conn, _ = _make_connection()
        loader = serving_store_registry.resolve(
            "sqlserver", secret_arn=_SECRET_ARN, region_name=_REGION
        )

        with patch("pymssql.connect", side_effect=[admin_conn, main_conn]):
            loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)

        admin_sql = " ".join(str(c.args[0]) for c in admin_cursor.execute.call_args_list)
        assert "CREATE DATABASE [edl_serving]" in admin_sql

    def test_azure_sql_skips_connection_database_bootstrap(self):
        # engine_id == "azure_sql" is always BYO-DB — bootstrap must never connect to master.
        main_conn, _ = _make_connection()
        loader = serving_store_registry.resolve(
            "azure_sql", secret_arn=_SECRET_ARN, region_name=_REGION
        )

        with patch("pymssql.connect", return_value=main_conn) as connect_mock:
            loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)

        assert all(c.kwargs.get("database") != "master" for c in connect_mock.call_args_list)

    def test_second_load_skips_unchanged_rows_via_merge(self):
        records = _make_records()
        unchanged_hash = compute_row_hash(records[0], ["account_id", "name", "revenue"])

        mock_conn, mock_cursor = _make_connection()
        loader = SqlServerLoader(_SECRET_ARN, _REGION)

        with patch("pymssql.connect", return_value=mock_conn):
            first = loader.load(records, _TABLE_NAME, ("account_id",), _TENANT_CODE)
            assert first.records_loaded == 2

            mock_cursor.fetchall.return_value = [{"account_id": "001", "_row_hash": unchanged_hash}]
            mock_cursor.fetchone.side_effect = _fetchone_sequence(
                schema_exists=True, login_exists=True, user_exists=True, table_exists=True
            )
            second = loader.load(records, _TABLE_NAME, ("account_id",), _TENANT_CODE)

        assert second.records_loaded == 1
        assert second.records_skipped == 1
        merge_calls = [
            c for c in mock_cursor.execute.call_args_list if "MERGE INTO" in str(c.args[0])
        ]
        assert len(merge_calls) >= 1
