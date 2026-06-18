"""
Tests for MySqlSchemaIntrospectionClient.

Coverage:
  - Discovers all columns from information_schema (happy path)
  - FieldMode filtering: ALL, STANDARD/CUSTOM (treated as ALL for MySQL), INCLUDE_ONLY
  - exclude_fields removes specified columns
  - Non-queryable types excluded from FieldContract
  - Empty result (table not found) → MySqlSchemaIntrospectionClientError
  - database/table_name are NOT interpolated into query text (OWASP A03 regression)
  - Fingerprint is deterministic
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from connector_runtime.adapters.mysql_rds.mysql_schema_introspection_client import (
    _INTROSPECT_QUERY,
    MySqlSchemaIntrospectionClient,
    MySqlSchemaIntrospectionClientError,
)
from contracts.entity_configuration_contract import FieldMode

_SOURCE_ID = "mysql-rds"
_ENTITY_ID = "mysql-rds-orders"
_DATABASE = "production"
_TABLE = "orders"

_MOCK_ROWS = [
    {
        "COLUMN_NAME": "id",
        "DATA_TYPE": "bigint",
        "IS_NULLABLE": "NO",
        "CHARACTER_MAXIMUM_LENGTH": None,
        "NUMERIC_PRECISION": 19,
        "NUMERIC_SCALE": 0,
    },
    {
        "COLUMN_NAME": "customer_id",
        "DATA_TYPE": "bigint",
        "IS_NULLABLE": "YES",
        "CHARACTER_MAXIMUM_LENGTH": None,
        "NUMERIC_PRECISION": 19,
        "NUMERIC_SCALE": 0,
    },
    {
        "COLUMN_NAME": "order_date",
        "DATA_TYPE": "datetime",
        "IS_NULLABLE": "YES",
        "CHARACTER_MAXIMUM_LENGTH": None,
        "NUMERIC_PRECISION": None,
        "NUMERIC_SCALE": None,
    },
    {
        "COLUMN_NAME": "notes",
        "DATA_TYPE": "json",  # non-queryable
        "IS_NULLABLE": "YES",
        "CHARACTER_MAXIMUM_LENGTH": None,
        "NUMERIC_PRECISION": None,
        "NUMERIC_SCALE": None,
    },
]


def _make_mock_connection(rows: list[dict] | None = None) -> MagicMock:
    """Return a pymysql connection mock whose cursor returns the given rows."""
    effective_rows = rows if rows is not None else _MOCK_ROWS
    conn = MagicMock()
    cursor = MagicMock()
    # cursor.description returns list of (column_name, ...) tuples
    col_names = list(effective_rows[0].keys()) if effective_rows else []
    cursor.description = [(col,) for col in col_names]
    cursor.fetchall.return_value = [
        tuple(row[col] for col in col_names)  # type: ignore[index]
        for row in effective_rows
    ]
    # Support context manager protocol (with conn.cursor() as cursor)
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


class TestFieldDiscovery:
    def test_discovers_all_queryable_columns(self) -> None:
        conn = _make_mock_connection()
        client = MySqlSchemaIntrospectionClient(connection=conn)
        contract = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            database=_DATABASE,
            table_name=_TABLE,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        field_names = {f.name for f in contract.fields}
        # 'notes' is json type — non-queryable, excluded
        assert "notes" not in field_names
        assert "id" in field_names
        assert "customer_id" in field_names
        assert "order_date" in field_names

    def test_field_mode_include_only_returns_exact_set(self) -> None:
        conn = _make_mock_connection()
        client = MySqlSchemaIntrospectionClient(connection=conn)
        contract = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            database=_DATABASE,
            table_name=_TABLE,
            field_mode=FieldMode.INCLUDE_ONLY,
            include_fields=["id", "order_date"],
            exclude_fields=[],
        )
        field_names = {f.name for f in contract.fields}
        assert field_names == {"id", "order_date"}

    def test_exclude_fields_removes_columns(self) -> None:
        conn = _make_mock_connection()
        client = MySqlSchemaIntrospectionClient(connection=conn)
        contract = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            database=_DATABASE,
            table_name=_TABLE,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=["customer_id"],
        )
        field_names = {f.name for f in contract.fields}
        assert "customer_id" not in field_names

    def test_standard_mode_treated_as_all_for_mysql(self) -> None:
        """MySQL has no custom/standard distinction — STANDARD returns all queryable fields."""
        conn = _make_mock_connection()
        client = MySqlSchemaIntrospectionClient(connection=conn)
        contract = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            database=_DATABASE,
            table_name=_TABLE,
            field_mode=FieldMode.STANDARD,
            include_fields=[],
            exclude_fields=[],
        )
        assert "id" in {f.name for f in contract.fields}

    def test_empty_result_raises_error(self) -> None:
        conn = _make_mock_connection(rows=[])
        # Override to avoid empty dict key error on column_name
        cursor = MagicMock()
        cursor.description = []
        cursor.fetchall.return_value = []
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        client = MySqlSchemaIntrospectionClient(connection=conn)
        with pytest.raises(MySqlSchemaIntrospectionClientError, match="No columns found"):
            client.discover_fields(
                source_id=_SOURCE_ID,
                entity_id=_ENTITY_ID,
                database=_DATABASE,
                table_name=_TABLE,
                field_mode=FieldMode.ALL,
                include_fields=[],
                exclude_fields=[],
            )

    def test_is_nullable_parsing(self) -> None:
        conn = _make_mock_connection()
        client = MySqlSchemaIntrospectionClient(connection=conn)
        contract = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            database=_DATABASE,
            table_name=_TABLE,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        field_by_name = {f.name: f for f in contract.fields}
        assert field_by_name["id"].is_nullable is False  # IS_NULLABLE=NO
        assert field_by_name["customer_id"].is_nullable is True  # IS_NULLABLE=YES


class TestSecurityRequirements:
    def test_query_uses_parameterized_placeholders(self) -> None:
        """OWASP A03: database and table_name must not be in the literal query string."""
        # The introspect query uses %(database)s and %(table_name)s placeholders.
        assert "%(database)s" in _INTROSPECT_QUERY
        assert "%(table_name)s" in _INTROSPECT_QUERY
        # Raw values must not be embedded.
        assert "production" not in _INTROSPECT_QUERY
        assert "orders" not in _INTROSPECT_QUERY

    def test_fingerprint_deterministic(self) -> None:
        conn1 = _make_mock_connection()
        conn2 = _make_mock_connection()
        c1 = MySqlSchemaIntrospectionClient(connection=conn1).discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            database=_DATABASE,
            table_name=_TABLE,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        c2 = MySqlSchemaIntrospectionClient(connection=conn2).discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            database=_DATABASE,
            table_name=_TABLE,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        assert c1.schema_fingerprint == c2.schema_fingerprint
