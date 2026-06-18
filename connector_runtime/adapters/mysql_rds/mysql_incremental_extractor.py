"""
MySQL incremental extractor.

Executes parameterized SQL queries against a MySQL RDS instance and yields
ExtractionRecord objects.  Supports both FULL (table scan) and INCREMENTAL
(watermark-bounded window) load types.

Query parameterization:
  - Uses %(name)s named parameters (pymysql pyformat paramstyle).
  - Watermark values are bound at execution time — NEVER interpolated into
    the query text string (OWASP A03 SQL injection prevention).
  - Field names and table names are validated against a strict SQL identifier
    regex before insertion into the SELECT / FROM / WHERE clauses.

Batching:
  - Rows are fetched in configurable batches using server-side cursor to
    avoid loading the entire result set into memory for large tables.

Naming per spec: mysql_incremental_extractor → MySqlIncrementalExtractor
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, Final

from connector_runtime.interfaces.connector_interface import (
    ExtractionRecord,
    FieldContract,
    QueryContract,
)
from contracts.entity_configuration_contract import LoadType
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# SQL identifier pattern — table names and column names must satisfy this.
_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,254}$")

# ISO-8601 UTC date-time pattern for watermark bound validation.
_ISO8601_UTC_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?Z?([+-]\d{2}:\d{2})?)?$"
)

# Number of rows to fetch per round-trip (server-side cursor batch size).
_FETCH_BATCH_SIZE: Final[int] = 1_000


class MySqlIncrementalExtractorError(Exception):
    """Raised when query construction or execution fails."""


class MySqlIncrementalExtractor:
    """
    Executes parameterized SQL queries against a MySQL RDS instance.

    Accepts a live pymysql connection — connection management (open/close/SSL)
    is the caller's responsibility.  This class only executes queries.

    Usage::

        extractor = MySqlIncrementalExtractor(connection=conn)
        for record in extractor.extract(query_contract):
            process(record)
    """

    def __init__(self, connection: Any) -> None:
        self._conn = connection

    def extract(self, query_contract: QueryContract) -> Iterator[ExtractionRecord]:
        """
        Execute the query from QueryContract and yield ExtractionRecord per row.

        Parameterized values in query_contract.query_parameters are bound
        by pymysql — never interpolated by this code.

        Sets ExtractionRecord.source_timestamp from the watermark_field
        column value when present.

        Args:
            query_contract: The query to execute (built by MySqlRdsConnector).

        Yields:
            ExtractionRecord for each row returned by the query.

        Raises:
            MySqlIncrementalExtractorError: on cursor execution failure.
        """
        try:
            with self._conn.cursor() as cursor:
                cursor.execute(
                    query_contract.query_text,
                    query_contract.query_parameters or {},
                )
                columns = [col[0] for col in cursor.description]

                while True:
                    rows = cursor.fetchmany(_FETCH_BATCH_SIZE)
                    if not rows:
                        break
                    for row in rows:
                        payload = dict(zip(columns, row, strict=True))
                        record = ExtractionRecord(payload=payload)
                        if (
                            query_contract.watermark_field
                            and query_contract.watermark_field in payload
                            and payload[query_contract.watermark_field] is not None
                        ):
                            record.source_timestamp = str(payload[query_contract.watermark_field])
                        yield record

        except MySqlIncrementalExtractorError:
            raise
        except Exception as exc:
            raise MySqlIncrementalExtractorError(
                f"MySQL query execution failed: {type(exc).__name__}"
            ) from exc

    @staticmethod
    def build_query(
        field_contract: FieldContract,
        table_name: str,
        load_type: LoadType,
        watermark_field: str | None,
        watermark_lower: str | None,
        watermark_upper: str | None,
    ) -> QueryContract:
        """
        Build a parameterized SQL SELECT query from a FieldContract.

        Field names and table_name are validated before insertion into
        the query text.  Watermark bounds are stored as query_parameters
        (%(name)s placeholders) — never interpolated.

        Args:
            field_contract: Discovered columns.
            table_name: MySQL table to query (must match SQL identifier pattern).
            load_type: FULL or INCREMENTAL.
            watermark_field: Column name used for incremental window filtering.
            watermark_lower: ISO-8601 lower bound (inclusive).
            watermark_upper: ISO-8601 upper bound (exclusive).

        Returns:
            QueryContract with parameterized query_text and query_parameters.

        Raises:
            MySqlIncrementalExtractorError: on validation failure.
        """
        if not _IDENTIFIER_PATTERN.match(table_name):
            raise MySqlIncrementalExtractorError(
                f"table_name {table_name!r} does not match SQL identifier pattern."
            )

        if load_type == LoadType.INCREMENTAL and not watermark_field:
            raise MySqlIncrementalExtractorError(
                "watermark_field is required for INCREMENTAL load type."
            )

        field_names: list[str] = []
        for descriptor in field_contract.fields:
            if not _IDENTIFIER_PATTERN.match(descriptor.name):
                raise MySqlIncrementalExtractorError(
                    f"Field name {descriptor.name!r} does not match identifier pattern."
                )
            field_names.append(f"`{descriptor.name}`")

        if not field_names:
            raise MySqlIncrementalExtractorError(
                "FieldContract contains no queryable fields — cannot build SQL query."
            )

        select_clause = ", ".join(field_names)
        # field names and table_name are both validated against _IDENTIFIER_PATTERN
        # before this point — no user-controlled input can reach this f-string.
        query_text = f"SELECT {select_clause} FROM `{table_name}`"  # noqa: S608
        query_parameters: dict[str, Any] = {}
        effective_watermark_field: str | None = None

        if load_type == LoadType.INCREMENTAL and watermark_field:
            if not _IDENTIFIER_PATTERN.match(watermark_field):
                raise MySqlIncrementalExtractorError(
                    f"watermark_field {watermark_field!r} does not match identifier pattern."
                )
            if watermark_lower and not _ISO8601_UTC_PATTERN.match(watermark_lower):
                raise MySqlIncrementalExtractorError(
                    f"watermark_lower {watermark_lower!r} is not a valid ISO-8601 value."
                )
            if watermark_upper and not _ISO8601_UTC_PATTERN.match(watermark_upper):
                raise MySqlIncrementalExtractorError(
                    f"watermark_upper {watermark_upper!r} is not a valid ISO-8601 value."
                )
            query_text = (
                f"{query_text}"
                f" WHERE `{watermark_field}` >= %(lower_bound)s"
                f" AND `{watermark_field}` < %(upper_bound)s"
                f" ORDER BY `{watermark_field}` ASC"
            )
            query_parameters["lower_bound"] = watermark_lower
            query_parameters["upper_bound"] = watermark_upper
            effective_watermark_field = watermark_field

        return QueryContract(
            source_id=field_contract.source_id,
            entity_id=field_contract.entity_id,
            query_text=query_text,
            query_parameters=query_parameters,
            load_type=load_type,
            watermark_lower=watermark_lower,
            watermark_upper=watermark_upper,
            watermark_field=effective_watermark_field,
            estimated_record_count=None,
        )
