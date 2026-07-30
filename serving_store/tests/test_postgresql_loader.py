"""Tests for PostgreSqlLoader (PostgreSQL engine)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from serving_store.interfaces.loader_interface import compute_row_hash
from serving_store.loaders.postgresql_loader import PostgreSqlLoader, ServingStoreError

_REGION = "us-east-1"
_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-db-creds"
_TABLE_NAME = "salesforce_account"
_TENANT_CODE = "acme-corp"
_OTHER_TENANT_CODE = "globex-eu"


def _make_creds():
    return json.dumps(
        {
            "host": "test-rds.us-east-1.rds.amazonaws.com",
            "port": "5432",
            "username": "dbuser",
            "password": "dbpass",
        }
    )


def _make_records():
    return [
        {"account_id": "001", "name": "Acme Corp", "revenue": 1_000_000},
        {"account_id": "002", "name": "Beta Ltd", "revenue": 500_000},
    ]


def _make_connection(fetchall_return: list | None = None, role_exists: bool = False):
    mock_conn = MagicMock()
    mock_conn.info.dbname = "datalake_serving"
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchall.return_value = fetchall_return or []
    mock_cursor.fetchone.return_value = {"1": 1} if role_exists else None
    mock_conn.cursor.return_value = mock_cursor
    return mock_conn, mock_cursor


@mock_aws
class TestPostgreSqlLoader:
    def setup_method(self, method=None):
        sm = boto3.client("secretsmanager", region_name=_REGION)
        sm.create_secret(Name=_SECRET_ARN, SecretString=_make_creds())

    def test_invalid_table_name_raises(self):
        loader = PostgreSqlLoader(_SECRET_ARN, _REGION)
        with pytest.raises(ValueError, match="Invalid table name"):
            loader.load(_make_records(), "INVALID TABLE NAME", ("account_id",), _TENANT_CODE)

    def test_empty_records_raises(self):
        loader = PostgreSqlLoader(_SECRET_ARN, _REGION)
        with pytest.raises(ServingStoreError):
            loader.load([], _TABLE_NAME, ("account_id",), _TENANT_CODE)

    def test_successful_load_with_mocked_connection(self):
        mock_conn, _ = _make_connection()
        loader = PostgreSqlLoader(_SECRET_ARN, _REGION)

        with patch("psycopg.connect", return_value=mock_conn):
            result = loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)

        assert result.records_loaded == 2
        assert result.records_skipped == 0
        assert result.table_name == _TABLE_NAME
        assert result.database_name == "acme_corp"

    def test_two_tenants_produce_distinct_schemas(self):
        mock_conn, _ = _make_connection()
        loader = PostgreSqlLoader(_SECRET_ARN, _REGION)

        with patch("psycopg.connect", return_value=mock_conn):
            result_a = loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)
            result_b = loader.load(
                _make_records(), _TABLE_NAME, ("account_id",), _OTHER_TENANT_CODE
            )

        assert result_a.database_name != result_b.database_name
        assert result_a.database_name == "acme_corp"
        assert result_b.database_name == "globex_eu"

    def test_connection_error_raises_serving_store_error(self):
        loader = PostgreSqlLoader(_SECRET_ARN, _REGION)
        with patch(
            "psycopg.connect",
            side_effect=Exception("Connection refused"),
        ):
            with pytest.raises(ServingStoreError):
                loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)

    def test_reader_role_provisioned_with_default_privileges_for_future_tables(self):
        mock_conn, mock_cursor = _make_connection()
        loader = PostgreSqlLoader(_SECRET_ARN, _REGION)

        with patch("psycopg.connect", return_value=mock_conn):
            loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)

        executed_sql = " ".join(str(c.args[0]) for c in mock_cursor.execute.call_args_list)
        assert "CREATE ROLE" in executed_sql
        assert 'GRANT USAGE ON SCHEMA "acme_corp"' in executed_sql
        assert 'GRANT SELECT ON ALL TABLES IN SCHEMA "acme_corp"' in executed_sql
        assert "ALTER DEFAULT PRIVILEGES IN SCHEMA" in executed_sql

    def test_existing_role_is_not_recreated(self):
        mock_conn, mock_cursor = _make_connection(role_exists=True)
        loader = PostgreSqlLoader(_SECRET_ARN, _REGION)

        with patch("psycopg.connect", return_value=mock_conn):
            loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)

        executed_sql = " ".join(str(c.args[0]) for c in mock_cursor.execute.call_args_list)
        assert "CREATE ROLE" not in executed_sql

    def test_connection_database_bootstrapped_when_absent(self):
        mock_conn, mock_cursor = _make_connection()  # fetchone None → datalake_serving absent
        loader = PostgreSqlLoader(_SECRET_ARN, _REGION)

        with patch("psycopg.connect", return_value=mock_conn):
            loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)

        executed_sql = " ".join(str(c.args[0]) for c in mock_cursor.execute.call_args_list)
        assert 'CREATE DATABASE "datalake_serving"' in executed_sql

    def test_connection_database_not_recreated_when_present(self):
        mock_conn, mock_cursor = _make_connection(role_exists=True)  # fetchone truthy → present
        loader = PostgreSqlLoader(_SECRET_ARN, _REGION)

        with patch("psycopg.connect", return_value=mock_conn):
            loader.load(_make_records(), _TABLE_NAME, ("account_id",), _TENANT_CODE)

        executed_sql = " ".join(str(c.args[0]) for c in mock_cursor.execute.call_args_list)
        assert "CREATE DATABASE" not in executed_sql

    def test_second_load_skips_unchanged_rows(self):
        records = _make_records()
        unchanged_hash = compute_row_hash(records[0], ["account_id", "name", "revenue"])

        mock_conn, mock_cursor = _make_connection()
        loader = PostgreSqlLoader(_SECRET_ARN, _REGION)

        with patch("psycopg.connect", return_value=mock_conn):
            first = loader.load(records, _TABLE_NAME, ("account_id",), _TENANT_CODE)
            assert first.records_loaded == 2

            mock_cursor.fetchall.return_value = [{"account_id": "001", "_row_hash": unchanged_hash}]
            second = loader.load(records, _TABLE_NAME, ("account_id",), _TENANT_CODE)

        assert second.records_loaded == 1
        assert second.records_skipped == 1
