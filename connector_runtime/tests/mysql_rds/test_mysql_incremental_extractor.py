"""
Tests for MySqlIncrementalExtractor.

Coverage:
  - FULL load — SELECT all fields, no WHERE clause
  - INCREMENTAL load — WHERE clause with %(lower_bound)s / %(upper_bound)s
  - Watermark values in query_parameters NOT in query_text (OWASP A03)
  - source_timestamp set from watermark field value
  - Batched row fetching
  - build_query() validation: empty fields, invalid table name, invalid watermark field
  - Non-ISO8601 watermark bounds rejected
  - Cursor execution failure → MySqlIncrementalExtractorError
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from connector_runtime.adapters.mysql_rds.mysql_incremental_extractor import (
    MySqlIncrementalExtractor,
    MySqlIncrementalExtractorError,
)
from connector_runtime.interfaces.connector_interface import (
    FieldContract,
    FieldDescriptor,
)
from contracts.entity_configuration_contract import LoadType

_LOWER = "2026-01-01T00:00:00Z"
_UPPER = "2026-06-12T00:00:00Z"
_SOURCE_ID = "mysql-rds"
_ENTITY_ID = "mysql-rds-orders"


def _make_field_contract(field_names: list[str] | None = None) -> FieldContract:
    names = field_names or ["id", "customer_id", "order_date"]
    descriptors = tuple(
        FieldDescriptor(name=n, data_type="varchar", is_nullable=True, is_queryable=True)
        for n in names
    )
    return FieldContract(
        source_id=_SOURCE_ID,
        entity_id=_ENTITY_ID,
        fields=descriptors,
        discovery_timestamp=datetime.now(UTC),
        schema_fingerprint=FieldContract.compute_fingerprint(descriptors),
    )


def _make_mock_connection(rows: list[dict]) -> MagicMock:
    conn = MagicMock()
    cursor = MagicMock()
    col_names = list(rows[0].keys()) if rows else ["id"]
    cursor.description = [(col,) for col in col_names]
    # fetchmany: first call returns all rows, second returns []
    cursor.fetchmany.side_effect = [
        [tuple(row[col] for col in col_names) for row in rows],
        [],
    ]
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


class TestQueryConstruction:
    def test_full_load_no_where_clause(self) -> None:
        fc = _make_field_contract()
        qc = MySqlIncrementalExtractor.build_query(
            field_contract=fc,
            table_name="orders",
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
        )
        assert "WHERE" not in qc.query_text.upper()
        assert qc.query_parameters == {}
        assert qc.watermark_field is None

    def test_incremental_adds_where_clause(self) -> None:
        fc = _make_field_contract()
        qc = MySqlIncrementalExtractor.build_query(
            field_contract=fc,
            table_name="orders",
            load_type=LoadType.INCREMENTAL,
            watermark_field="order_date",
            watermark_lower=_LOWER,
            watermark_upper=_UPPER,
        )
        assert "WHERE" in qc.query_text.upper()
        assert "%(lower_bound)s" in qc.query_text
        assert "%(upper_bound)s" in qc.query_text
        assert qc.watermark_field == "order_date"

    def test_watermark_values_in_parameters_not_in_query_text(self) -> None:
        """OWASP A03: watermark values must not be in query_text."""
        fc = _make_field_contract()
        qc = MySqlIncrementalExtractor.build_query(
            field_contract=fc,
            table_name="orders",
            load_type=LoadType.INCREMENTAL,
            watermark_field="order_date",
            watermark_lower=_LOWER,
            watermark_upper=_UPPER,
        )
        assert _LOWER not in qc.query_text
        assert _UPPER not in qc.query_text
        assert qc.query_parameters["lower_bound"] == _LOWER
        assert qc.query_parameters["upper_bound"] == _UPPER

    def test_field_names_backtick_quoted_in_query(self) -> None:
        fc = _make_field_contract(["id", "order_date"])
        qc = MySqlIncrementalExtractor.build_query(
            field_contract=fc,
            table_name="orders",
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
        )
        assert "`id`" in qc.query_text
        assert "`order_date`" in qc.query_text
        assert "`orders`" in qc.query_text


class TestInputValidation:
    def test_invalid_table_name_raises(self) -> None:
        fc = _make_field_contract()
        with pytest.raises(MySqlIncrementalExtractorError, match="table_name"):
            MySqlIncrementalExtractor.build_query(
                field_contract=fc,
                table_name="orders; DROP TABLE orders; --",
                load_type=LoadType.FULL,
                watermark_field=None,
                watermark_lower=None,
                watermark_upper=None,
            )

    def test_incremental_without_watermark_field_raises(self) -> None:
        fc = _make_field_contract()
        with pytest.raises(MySqlIncrementalExtractorError, match="watermark_field"):
            MySqlIncrementalExtractor.build_query(
                field_contract=fc,
                table_name="orders",
                load_type=LoadType.INCREMENTAL,
                watermark_field=None,
                watermark_lower=_LOWER,
                watermark_upper=_UPPER,
            )

    def test_non_iso8601_lower_bound_raises(self) -> None:
        fc = _make_field_contract()
        with pytest.raises(MySqlIncrementalExtractorError, match="ISO-8601"):
            MySqlIncrementalExtractor.build_query(
                field_contract=fc,
                table_name="orders",
                load_type=LoadType.INCREMENTAL,
                watermark_field="order_date",
                watermark_lower="'; DROP TABLE orders; --",
                watermark_upper=_UPPER,
            )

    def test_empty_field_contract_raises(self) -> None:
        fc = FieldContract(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            fields=(),
            discovery_timestamp=datetime.now(UTC),
            schema_fingerprint="",
        )
        with pytest.raises(MySqlIncrementalExtractorError, match="no queryable fields"):
            MySqlIncrementalExtractor.build_query(
                field_contract=fc,
                table_name="orders",
                load_type=LoadType.FULL,
                watermark_field=None,
                watermark_lower=None,
                watermark_upper=None,
            )


class TestExtraction:
    def test_yields_all_rows(self) -> None:
        rows = [{"id": str(i), "order_date": "2026-06-10"} for i in range(5)]
        conn = _make_mock_connection(rows)
        extractor = MySqlIncrementalExtractor(connection=conn)
        fc = _make_field_contract(["id", "order_date"])
        qc = MySqlIncrementalExtractor.build_query(
            field_contract=fc,
            table_name="orders",
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
        )
        records = list(extractor.extract(qc))
        assert len(records) == 5
        assert records[0].payload["id"] == "0"

    def test_source_timestamp_set_from_watermark_field(self) -> None:
        rows = [{"id": "1", "order_date": "2026-06-10T12:00:00"}]
        conn = _make_mock_connection(rows)
        extractor = MySqlIncrementalExtractor(connection=conn)
        fc = _make_field_contract(["id", "order_date"])
        qc = MySqlIncrementalExtractor.build_query(
            field_contract=fc,
            table_name="orders",
            load_type=LoadType.INCREMENTAL,
            watermark_field="order_date",
            watermark_lower=_LOWER,
            watermark_upper=_UPPER,
        )
        records = list(extractor.extract(qc))
        assert records[0].source_timestamp == "2026-06-10T12:00:00"

    def test_cursor_failure_raises_extractor_error(self) -> None:
        conn = MagicMock()
        cursor = MagicMock()
        cursor.execute.side_effect = Exception("lock wait timeout")
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        extractor = MySqlIncrementalExtractor(connection=conn)
        fc = _make_field_contract()
        qc = MySqlIncrementalExtractor.build_query(
            field_contract=fc,
            table_name="orders",
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
        )
        with pytest.raises(MySqlIncrementalExtractorError, match="execution failed"):
            list(extractor.extract(qc))
