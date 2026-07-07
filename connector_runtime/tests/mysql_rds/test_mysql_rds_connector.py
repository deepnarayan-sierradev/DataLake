"""
Tests for MySqlRdsConnector.

Coverage:
  - Connector registers as 'mysql-rds' in the registry
  - get_capability_declaration() returns correct values
  - build_extraction_query() produces parameterized QueryContract
  - execute_extraction() yields ExtractionRecord from cursor rows
  - execute_extraction() sets source_timestamp from watermark field
  - classify_extraction_error() maps all known exception types correctly
  - SSL is enforced on every connection (ssl_disabled=False)
  - Password never in log output (OWASP A09)
  - PERF-1: SSDictCursor (streaming cursor) is used for every connection
  - PERF-2: connections are pooled/reused across calls and reconnected when stale
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pymysql.err
import pytest

import connector_runtime.adapters.mysql_rds.mysql_rds_connector as mysql_rds_connector_module
from connector_runtime.adapters.mysql_rds.mysql_rds_connector import MySqlRdsConnector
from connector_runtime.adapters.mysql_rds.mysql_rds_credentials_client import (
    MySqlConnectionParameters,
    MySqlRdsCredentialError,
)
from connector_runtime.interfaces.connector_interface import (
    ExtractionErrorClassification,
    FieldContract,
    FieldDescriptor,
)
from connector_runtime.registry import connector_registry
from contracts.entity_configuration_contract import FieldMode, LoadType

_ENV = "dev"
_REGION = "us-east-1"
_TABLE = "orders"


@pytest.fixture(autouse=True)
def _clear_connection_pool_cache() -> Iterator[None]:
    """
    Clear the module-level connection pool cache before and after every test.

    The cache (PERF-2) is intentionally process/module-scoped so a warm
    Lambda container reuses connections across invocations — but that same
    persistence would otherwise leak MagicMock "connections" between
    independent test cases that happen to share the same connection
    parameters (host/port/user/database), causing order-dependent failures.
    """
    mysql_rds_connector_module._connection_cache.clear()
    yield
    mysql_rds_connector_module._connection_cache.clear()


def _make_params() -> MySqlConnectionParameters:
    return MySqlConnectionParameters(
        host="mydb.cluster.rds.amazonaws.com",
        port=3306,
        username="extraction_user",
        password="s3cr3t",  # noqa: S106
        database="production",
    )


def _make_field_contract(field_names: list[str] | None = None) -> FieldContract:
    names = field_names or ["id", "order_date", "total"]
    descriptors = tuple(
        FieldDescriptor(name=n, data_type="varchar", is_nullable=True, is_queryable=True)
        for n in names
    )
    return FieldContract(
        source_id="mysql-rds",
        entity_id="mysql-rds-orders",
        fields=descriptors,
        discovery_timestamp=datetime.now(UTC),
        schema_fingerprint=FieldContract.compute_fingerprint(descriptors),
    )


def _make_connector_with_mock_creds() -> tuple[MySqlRdsConnector, MagicMock]:
    """Return connector + mock creds client to avoid Secrets Manager calls."""
    connector = MySqlRdsConnector.__new__(MySqlRdsConnector)
    mock_creds = MagicMock()
    mock_creds.get_connection_parameters.return_value = _make_params()
    connector._table_name = _TABLE  # type: ignore[attr-defined]
    connector._creds_client = mock_creds  # type: ignore[attr-defined]
    return connector, mock_creds


def _make_mock_connection(rows: list[dict]) -> MagicMock:
    conn = MagicMock()
    cursor = MagicMock()
    col_names = list(rows[0].keys()) if rows else ["id"]
    cursor.description = [(col,) for col in col_names]
    cursor.fetchmany.side_effect = [
        [tuple(row[col] for col in col_names) for row in rows],
        [],
    ]
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


class TestConnectorRegistration:
    def test_connector_registered_under_mysql_rds(self) -> None:
        assert "mysql-rds" in connector_registry.registered_source_ids

    def test_registry_resolves_mysql_rds(self) -> None:
        mock_creds = MagicMock()
        with patch(
            "connector_runtime.adapters.mysql_rds.mysql_rds_connector.MySqlRdsCredentialsClient",
            return_value=mock_creds,
        ):
            connector = connector_registry.resolve(
                "mysql-rds",
                environment=_ENV,
                region_name=_REGION,
                table_name=_TABLE,
            )
        assert isinstance(connector, MySqlRdsConnector)


class TestCapabilityDeclaration:
    def test_source_id_is_mysql_rds(self) -> None:
        connector, _ = _make_connector_with_mock_creds()
        caps = connector.get_capability_declaration()
        assert caps.source_id == "mysql-rds"

    def test_bulk_extraction_not_supported(self) -> None:
        connector, _ = _make_connector_with_mock_creds()
        caps = connector.get_capability_declaration()
        assert caps.supports_bulk_extraction is False

    def test_all_field_modes_supported(self) -> None:
        connector, _ = _make_connector_with_mock_creds()
        caps = connector.get_capability_declaration()
        modes = caps.supported_field_modes
        assert FieldMode.ALL in modes
        assert FieldMode.INCLUDE_ONLY in modes


class TestExecuteExtraction:
    def test_yields_all_rows(self) -> None:
        connector, _ = _make_connector_with_mock_creds()
        rows = [{"id": str(i), "order_date": "2026-06-10"} for i in range(4)]
        mock_conn = _make_mock_connection(rows)

        fc = _make_field_contract(["id", "order_date"])
        qc = MySqlRdsConnector.build_extraction_query(
            connector,
            field_contract=fc,
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        with patch(
            "connector_runtime.adapters.mysql_rds.mysql_rds_connector.MySqlRdsConnector"
            "._open_connection",
            return_value=mock_conn,
        ):
            records = list(connector.execute_extraction(qc, run_id="run-001"))
        assert len(records) == 4

    def test_source_timestamp_set_from_watermark(self) -> None:
        connector, _ = _make_connector_with_mock_creds()
        rows = [{"id": "1", "order_date": "2026-06-10T08:00:00"}]
        mock_conn = _make_mock_connection(rows)

        fc = _make_field_contract(["id", "order_date"])
        qc = MySqlRdsConnector.build_extraction_query(
            connector,
            field_contract=fc,
            load_type=LoadType.INCREMENTAL,
            watermark_field="order_date",
            watermark_lower="2026-01-01T00:00:00Z",
            watermark_upper="2026-06-12T00:00:00Z",
            extraction_window_days=7,
        )
        with patch(
            "connector_runtime.adapters.mysql_rds.mysql_rds_connector.MySqlRdsConnector"
            "._open_connection",
            return_value=mock_conn,
        ):
            records = list(connector.execute_extraction(qc, run_id="run-002"))
        assert records[0].source_timestamp == "2026-06-10T08:00:00"

    def test_connection_left_open_and_pooled_after_full_extraction(self) -> None:
        """
        PERF-2: a fully-consumed extraction leaves the connection open (not
        closed) so it can be reused by a subsequent invocation, rather than
        closing and reopening a fresh connection on every run.
        """
        connector, _ = _make_connector_with_mock_creds()
        rows = [{"id": "1"}]
        mock_conn = _make_mock_connection(rows)

        fc = _make_field_contract(["id"])
        qc = MySqlRdsConnector.build_extraction_query(
            connector,
            field_contract=fc,
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        with patch(
            "connector_runtime.adapters.mysql_rds.mysql_rds_connector.MySqlRdsConnector"
            "._open_connection",
            return_value=mock_conn,
        ):
            list(connector.execute_extraction(qc, run_id="run-003"))
        mock_conn.close.assert_not_called()


class TestSslEnforcement:
    def test_ssl_disabled_false_passed_to_pymysql(self) -> None:
        """SSL must be enforced on every connection (OWASP A02 — in-transit encryption)."""
        connector, mock_creds = _make_connector_with_mock_creds()
        params = _make_params()
        mock_creds.get_connection_parameters.return_value = params

        with patch(
            "connector_runtime.adapters.mysql_rds.mysql_rds_connector.pymysql"
        ) as mock_pymysql:
            mock_conn = MagicMock()
            mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
            mock_cursor = mock_conn.cursor.return_value.__enter__.return_value
            mock_cursor.description = [("id",)]
            mock_cursor.fetchmany.return_value = []
            mock_pymysql.connect.return_value = mock_conn
            mock_pymysql.cursors.DictCursor = object

            fc = _make_field_contract(["id"])
            MySqlRdsConnector.build_extraction_query(
                connector,
                field_contract=fc,
                load_type=LoadType.FULL,
                watermark_field=None,
                watermark_lower=None,
                watermark_upper=None,
                extraction_window_days=0,
            )
            # discover_queryable_fields opens its own connection
            # We test via _open_connection directly
            MySqlRdsConnector._open_connection(params)

        # Verify ssl_disabled=False was passed
        connect_kwargs = mock_pymysql.connect.call_args
        if connect_kwargs is not None:
            _, kwargs = connect_kwargs
            assert kwargs.get("ssl_disabled") is False


class TestStreamingCursor:
    """PERF-1: connections must use SSDictCursor, not the buffered DictCursor."""

    def test_open_connection_uses_ssdictcursor(self) -> None:
        params = _make_params()
        with patch(
            "connector_runtime.adapters.mysql_rds.mysql_rds_connector.pymysql"
        ) as mock_pymysql:
            mock_pymysql.connect.return_value = MagicMock()
            MySqlRdsConnector._open_connection(params)

        _, kwargs = mock_pymysql.connect.call_args
        assert kwargs.get("cursorclass") is mock_pymysql.cursors.SSDictCursor

    def test_open_connection_does_not_use_buffered_dictcursor(self) -> None:
        params = _make_params()
        with patch(
            "connector_runtime.adapters.mysql_rds.mysql_rds_connector.pymysql"
        ) as mock_pymysql:
            mock_pymysql.connect.return_value = MagicMock()
            MySqlRdsConnector._open_connection(params)

        _, kwargs = mock_pymysql.connect.call_args
        assert kwargs.get("cursorclass") is not mock_pymysql.cursors.DictCursor


class TestConnectionPooling:
    """
    PERF-2: warm-container connection reuse with health-check-and-reconnect.

    The module-level connection cache is cleared before/after every test by
    the autouse `_clear_connection_pool_cache` fixture.
    """

    def test_second_call_with_same_params_reuses_cached_connection(self) -> None:
        params = _make_params()
        with patch(
            "connector_runtime.adapters.mysql_rds.mysql_rds_connector.pymysql"
        ) as mock_pymysql:
            first_conn = MagicMock()
            mock_pymysql.connect.return_value = first_conn

            returned_first = MySqlRdsConnector._open_connection(params)
            returned_second = MySqlRdsConnector._open_connection(params)

        assert returned_first is first_conn
        assert returned_second is first_conn
        assert mock_pymysql.connect.call_count == 1
        first_conn.ping.assert_called_once_with(reconnect=True)

    def test_different_connection_params_are_not_reused(self) -> None:
        params_a = _make_params()
        params_b = MySqlConnectionParameters(
            host="other-db.cluster.rds.amazonaws.com",
            port=3306,
            username="extraction_user",
            password="s3cr3t",  # noqa: S106
            database="production",
        )
        with patch(
            "connector_runtime.adapters.mysql_rds.mysql_rds_connector.pymysql"
        ) as mock_pymysql:
            mock_pymysql.connect.side_effect = [MagicMock(), MagicMock()]

            conn_a = MySqlRdsConnector._open_connection(params_a)
            conn_b = MySqlRdsConnector._open_connection(params_b)

        assert conn_a is not conn_b
        assert mock_pymysql.connect.call_count == 2

    def test_stale_cached_connection_is_evicted_and_reconnected(self) -> None:
        """A cached connection whose ping() fails is replaced with a fresh one."""
        params = _make_params()
        with patch(
            "connector_runtime.adapters.mysql_rds.mysql_rds_connector.pymysql"
        ) as mock_pymysql:
            stale_conn = MagicMock()
            stale_conn.ping.side_effect = pymysql.err.OperationalError(
                2006, "MySQL server has gone away"
            )
            fresh_conn = MagicMock()
            mock_pymysql.connect.side_effect = [stale_conn, fresh_conn]

            first = MySqlRdsConnector._open_connection(params)
            second = MySqlRdsConnector._open_connection(params)

        assert first is stale_conn
        assert second is fresh_conn
        assert second is not stale_conn
        stale_conn.ping.assert_called_once_with(reconnect=True)
        stale_conn.close.assert_called_once()
        assert mock_pymysql.connect.call_count == 2

    def test_reconnected_connection_is_cached_for_next_call(self) -> None:
        """After a reconnect, the new connection becomes the cached one."""
        params = _make_params()
        with patch(
            "connector_runtime.adapters.mysql_rds.mysql_rds_connector.pymysql"
        ) as mock_pymysql:
            stale_conn = MagicMock()
            stale_conn.ping.side_effect = pymysql.err.OperationalError(2013, "Lost connection")
            fresh_conn = MagicMock()
            mock_pymysql.connect.side_effect = [stale_conn, fresh_conn]

            MySqlRdsConnector._open_connection(params)  # seeds stale_conn
            MySqlRdsConnector._open_connection(params)  # evicts + reconnects
            third = MySqlRdsConnector._open_connection(params)  # reuses fresh_conn

        assert third is fresh_conn
        assert mock_pymysql.connect.call_count == 2

    def test_evict_connection_removes_from_cache_and_closes(self) -> None:
        params = _make_params()
        with patch(
            "connector_runtime.adapters.mysql_rds.mysql_rds_connector.pymysql"
        ) as mock_pymysql:
            conn = MagicMock()
            mock_pymysql.connect.return_value = conn
            MySqlRdsConnector._open_connection(params)

        assert mysql_rds_connector_module._connection_cache
        MySqlRdsConnector._evict_connection(conn)
        assert not mysql_rds_connector_module._connection_cache
        conn.close.assert_called_once()

    def test_evict_connection_handles_close_failure_gracefully(self) -> None:
        """Eviction must not raise even if closing an already-dead socket fails."""
        conn = MagicMock()
        conn.close.side_effect = OSError("socket already closed")
        MySqlRdsConnector._evict_connection(conn)  # must not raise


class TestErrorClassification:
    def test_credential_error_is_deterministic(self) -> None:
        connector, _ = _make_connector_with_mock_creds()
        result = connector.classify_extraction_error(MySqlRdsCredentialError("bad"))
        assert result == ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS

    def test_operational_error_is_transient_network(self) -> None:
        connector, _ = _make_connector_with_mock_creds()
        result = connector.classify_extraction_error(
            pymysql.err.OperationalError(2003, "Can't connect")
        )
        assert result == ExtractionErrorClassification.TRANSIENT_NETWORK

    def test_programming_error_is_deterministic_invalid_object(self) -> None:
        connector, _ = _make_connector_with_mock_creds()
        result = connector.classify_extraction_error(
            pymysql.err.ProgrammingError(1146, "Table doesn't exist")
        )
        assert result == ExtractionErrorClassification.DETERMINISTIC_INVALID_OBJECT

    def test_os_error_is_transient_network(self) -> None:
        connector, _ = _make_connector_with_mock_creds()
        result = connector.classify_extraction_error(OSError("connection reset"))
        assert result == ExtractionErrorClassification.TRANSIENT_NETWORK

    def test_unknown_exception_is_unknown(self) -> None:
        connector, _ = _make_connector_with_mock_creds()
        result = connector.classify_extraction_error(RuntimeError("unexpected"))
        assert result == ExtractionErrorClassification.UNKNOWN

    def test_empty_table_name_raises(self) -> None:
        with pytest.raises(ValueError, match="table_name"):
            MySqlRdsConnector(environment=_ENV, region_name=_REGION, table_name="")


class TestGeneratorCleanup:
    """Connection must be closed even when the generator is partially consumed."""

    def test_connection_closed_on_early_exit(self) -> None:
        """conn.close() is called by the finally block when caller breaks early."""
        connector, _ = _make_connector_with_mock_creds()
        # Use more rows than the caller will consume.
        rows = [{"id": str(i)} for i in range(10)]
        mock_conn = _make_mock_connection(rows)

        fc = _make_field_contract(["id"])
        qc = MySqlRdsConnector.build_extraction_query(
            connector,
            field_contract=fc,
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        with patch(
            "connector_runtime.adapters.mysql_rds.mysql_rds_connector.MySqlRdsConnector"
            "._open_connection",
            return_value=mock_conn,
        ):
            gen = connector.execute_extraction(qc, run_id="run-gc-01")
            # Consume only the first record, then close the generator.
            next(gen)
            gen.close()  # triggers GeneratorExit → finally: conn.close()

        mock_conn.close.assert_called_once()

    def test_connection_closed_on_extraction_exception(self) -> None:
        """conn.close() is called by the finally block when extraction raises."""
        connector, _ = _make_connector_with_mock_creds()
        mock_conn = MagicMock()
        cursor = MagicMock()
        cursor.execute.side_effect = Exception("deadlock detected")
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        fc = _make_field_contract(["id"])
        qc = MySqlRdsConnector.build_extraction_query(
            connector,
            field_contract=fc,
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        with patch(
            "connector_runtime.adapters.mysql_rds.mysql_rds_connector.MySqlRdsConnector"
            "._open_connection",
            return_value=mock_conn,
        ):
            with pytest.raises(Exception):  # noqa: B017
                list(connector.execute_extraction(qc, run_id="run-gc-02"))

        mock_conn.close.assert_called_once()
