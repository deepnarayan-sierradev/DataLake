"""
MySQL schema introspection client.

Discovers table column definitions from MySQL's information_schema.COLUMNS
view.  No hardcoded column lists — all fields are fetched at runtime,
satisfying the spec requirement: "Handle schema changes without code changes."

Query used:
    SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE,
           CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = %(database)s
      AND TABLE_NAME   = %(table_name)s
    ORDER BY ORDINAL_POSITION

Security (OWASP A03):
  - The information_schema query uses %(name)s named parameters
    (pymysql pyformat style) — database and table_name are never interpolated.
  - Only column metadata is read; no row data is accessed.

Naming per spec: mysql_schema_introspection_client → MySqlSchemaIntrospectionClient
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from connector_runtime.interfaces.connector_interface import FieldContract, FieldDescriptor
from contracts.entity_configuration_contract import FieldMode
from observability.structured_logger import get_platform_logger

if TYPE_CHECKING:
    pass

_logger = get_platform_logger(__name__)

# MySQL types that are not directly extractable as scalar values.
_NON_QUERYABLE_TYPES: Final[frozenset[str]] = frozenset(
    {"geometry", "geomcollection", "point", "linestring", "polygon", "json"}
)

_INTROSPECT_QUERY: Final[str] = (
    "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, "
    "CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE "
    "FROM information_schema.COLUMNS "
    "WHERE TABLE_SCHEMA = %(database)s "
    "AND TABLE_NAME = %(table_name)s "
    "ORDER BY ORDINAL_POSITION"
)


class MySqlSchemaIntrospectionClientError(Exception):
    """Raised when schema discovery fails unexpectedly."""


class MySqlSchemaIntrospectionClient:
    """
    Reads MySQL table schema from information_schema at runtime.

    One instance per connector instance.  Uses a pymysql connection
    provided by the caller — this client does not manage the connection
    lifecycle (open/close is the caller's responsibility).

    Usage::

        client = MySqlSchemaIntrospectionClient(connection=conn)
        field_contract = client.discover_fields(
            source_id="mysql-rds",
            entity_id="mysql-rds-orders",
            database="production",
            table_name="orders",
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
    """

    def __init__(self, connection: Any) -> None:
        self._conn = connection

    def discover_fields(
        self,
        source_id: str,
        entity_id: str,
        database: str,
        table_name: str,
        field_mode: FieldMode,
        include_fields: list[str],
        exclude_fields: list[str],
    ) -> FieldContract:
        """
        Discover all queryable columns for the given database/table.

        Args:
            source_id: Platform stable source identifier.
            entity_id: Platform stable entity identifier.
            database: MySQL database (schema) name.
            table_name: MySQL table name.
            field_mode: Field selection mode (ALL, STANDARD, CUSTOM, INCLUDE_ONLY).
            include_fields: When INCLUDE_ONLY, the exact column names to include.
            exclude_fields: Column names to unconditionally exclude.

        Returns:
            FieldContract with all applicable columns and a computed fingerprint.

        Raises:
            MySqlSchemaIntrospectionClientError: on query failure or empty result.
        """
        rows = self._query_information_schema(database=database, table_name=table_name)

        if not rows:
            raise MySqlSchemaIntrospectionClientError(
                f"No columns found in information_schema for "
                f"database={database!r}, table={table_name!r}. "
                "Verify the table exists and the credentials have SELECT permission."
            )

        filtered = self._apply_field_mode(rows, field_mode, include_fields, exclude_fields)

        descriptors = tuple(
            FieldDescriptor(
                name=str(row["COLUMN_NAME"]),
                data_type=str(row["DATA_TYPE"]).lower(),
                is_nullable=str(row["IS_NULLABLE"]).upper() == "YES",
                is_queryable=str(row["DATA_TYPE"]).lower() not in _NON_QUERYABLE_TYPES,
                length=row.get("CHARACTER_MAXIMUM_LENGTH"),
                precision=row.get("NUMERIC_PRECISION"),
                scale=row.get("NUMERIC_SCALE"),
                is_custom=False,  # MySQL has no concept of custom vs standard columns
                source_label=str(row["COLUMN_NAME"]),
            )
            for row in filtered
            if str(row["DATA_TYPE"]).lower() not in _NON_QUERYABLE_TYPES
        )

        fingerprint = FieldContract.compute_fingerprint(descriptors)
        contract = FieldContract(
            source_id=source_id,
            entity_id=entity_id,
            fields=descriptors,
            discovery_timestamp=datetime.now(UTC),
            schema_fingerprint=fingerprint,
        )

        _logger.info(
            "mysql_rds_fields_discovered",
            source_id=source_id,
            entity_id=entity_id,
            database=database,
            table_name=table_name,
            field_count=len(descriptors),
            field_mode=str(field_mode),
        )
        return contract

    # ── Private ────────────────────────────────────────────────────────────────

    def _query_information_schema(self, database: str, table_name: str) -> list[dict[str, Any]]:
        """
        Execute the parameterized information_schema query.

        Uses %(name)s named parameter style (pymysql pyformat paramstyle).
        database and table_name are bound as parameters — never interpolated.

        Raises:
            MySqlSchemaIntrospectionClientError: on database error.
        """
        try:
            with self._conn.cursor() as cursor:
                cursor.execute(
                    _INTROSPECT_QUERY,
                    {"database": database, "table_name": table_name},
                )
                columns = [col[0] for col in cursor.description]
                normalised_rows: list[dict[str, Any]] = []
                for row in cursor.fetchall():
                    # DictCursor returns dict rows; default cursors return tuples.
                    if isinstance(row, dict):
                        normalised_rows.append(dict(row))
                    else:
                        normalised_rows.append(dict(zip(columns, row, strict=True)))
                return normalised_rows
        except Exception as exc:
            raise MySqlSchemaIntrospectionClientError(
                f"information_schema query failed for "
                f"database={database!r}, table={table_name!r}: {type(exc).__name__}"
            ) from exc

    @staticmethod
    def _apply_field_mode(
        rows: list[dict[str, Any]],
        field_mode: FieldMode,
        include_fields: list[str],
        exclude_fields: list[str],
    ) -> list[dict[str, Any]]:
        """
        Filter information_schema rows according to FieldMode.

        MySQL does not distinguish standard vs custom columns, so
        STANDARD and CUSTOM modes are treated identically to ALL
        (all columns minus exclude_fields).
        """
        exclude_set = set(exclude_fields)

        if field_mode == FieldMode.INCLUDE_ONLY:
            include_set = set(include_fields)
            return [r for r in rows if str(r["COLUMN_NAME"]) in include_set]

        return [r for r in rows if str(r["COLUMN_NAME"]) not in exclude_set]
