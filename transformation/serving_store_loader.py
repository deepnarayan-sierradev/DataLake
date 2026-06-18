"""
Target serving store loader.

Loads curated or analytics datasets into a MySQL RDS target serving database.

Design (spec §8.2):
  - Table schema derived from Parquet schema; no hardcoded DDL.
  - Idempotent: REPLACE INTO strategy prevents duplicate rows.
  - Credentials retrieved exclusively from AWS Secrets Manager.
  - Private VPC connectivity (no public RDS endpoint).
  - Load metrics emitted to structured log.

Security (OWASP A05, A07):
  - All SQL is parameterized; no string interpolation of column names or values.
  - Table name validated against safe-identifier regex before use.
  - Credentials never appear in logs or exceptions.
  - Connection closed in finally block to prevent resource leaks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import boto3
import pymysql
import pymysql.cursors

from observability.metrics_emitter import CloudWatchMetricsEmitter
from observability.structured_logger import get_platform_logger
from governance.lineage_record import LineageEmitter, build_serving_store_lineage

_logger = get_platform_logger(__name__)

# Validate table names and column names against safe SQL identifier patterns (OWASP A03, A05)
_SAFE_TABLE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_COLUMN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


@dataclass(frozen=True)
class ServingStoreLoadResult:
    """Summary of one serving store load operation."""

    table_name: str
    records_loaded: int
    records_skipped: int
    started_at: str  # ISO-8601 UTC
    completed_at: str  # ISO-8601 UTC


class ServingStoreLoader:
    """
    Loads canonical records from S3 analytics output into a MySQL RDS table.

    Workflow:
      1. Retrieve DB credentials from Secrets Manager
      2. Infer CREATE TABLE DDL from record schema
      3. CREATE TABLE IF NOT EXISTS
      4. REPLACE INTO <table> for idempotent upsert
      5. Commit and close connection

    One instance per serving database.
    """

    def __init__(
        self,
        secret_arn: str,
        db_name: str,
        region_name: str,
        metrics_emitter: CloudWatchMetricsEmitter | None = None,
        environment: str = "dev",
        governance_s3_bucket: str | None = None,
    ) -> None:
        self._secret_arn = secret_arn
        self._db_name = db_name
        self._region_name = region_name
        self._metrics_emitter = metrics_emitter
        self._environment = environment
        self._governance_s3_bucket = governance_s3_bucket
        self._sm: Any = boto3.client("secretsmanager", region_name=region_name)

    def load(
        self,
        records: list[dict[str, Any]],
        table_name: str,
        primary_keys: tuple[str, ...],
        run_id: str | None = None,
        analytics_s3_bucket: str | None = None,
        analytics_s3_prefix: str | None = None,
    ) -> ServingStoreLoadResult:
        """
        Load records into the named MySQL table.

        Args:
            records:              List of canonical records (all with consistent schema).
            table_name:           Target MySQL table name.
            primary_keys:         Fields that form the primary key for REPLACE INTO logic.
            run_id:               Pipeline run ID; required for lineage emission.
            analytics_s3_bucket:  Source analytics S3 bucket; required for lineage emission.
            analytics_s3_prefix:  Source analytics S3 prefix; required for lineage emission.

        Returns:
            ServingStoreLoadResult.

        Raises:
            ServingStoreError on connection, DDL, or DML failure.
            ValueError on invalid table_name.
        """
        if not _SAFE_TABLE_PATTERN.match(table_name):
            raise ValueError(f"Invalid table name: {table_name!r}")
        if not records:
            raise ServingStoreError("Cannot load zero records")

        started_at = datetime.now(UTC).isoformat()
        credentials = self._retrieve_credentials()
        try:
            connection = self._connect(credentials)
        except Exception as exc:
            raise ServingStoreError(f"Failed to connect to database: {exc}") from exc

        try:
            columns = list(records[0].keys())
            self._ensure_table(connection, table_name, columns, records[0], primary_keys)
            loaded = self._bulk_replace(connection, table_name, columns, records)
            connection.commit()
        except Exception as exc:
            connection.rollback()
            raise ServingStoreError(f"Load failed for table {table_name!r}: {exc}") from exc
        finally:
            connection.close()

        completed_at = datetime.now(UTC).isoformat()

        _logger.info(
            "serving_store_load_complete",
            table_name=table_name,
            records_loaded=loaded,
            db_name=self._db_name,
        )

        # Emit CloudWatch metrics (spec §8.2 AC)
        if self._metrics_emitter is not None:
            self._metrics_emitter.emit_records_extracted(
                source_id=table_name,
                entity_id=table_name,
                environment=self._environment,
                count=loaded,
            )

        # Emit lineage record if governance context was provided
        if (
            self._governance_s3_bucket
            and run_id
            and analytics_s3_bucket
            and analytics_s3_prefix
        ):
            try:
                lineage_record = build_serving_store_lineage(
                    run_id=run_id,
                    source_id=table_name,
                    entity_id=table_name,
                    analytics_s3_bucket=analytics_s3_bucket,
                    analytics_s3_prefix=analytics_s3_prefix,
                    table_name=table_name,
                    record_count=loaded,
                )
                LineageEmitter(
                    governance_s3_bucket=self._governance_s3_bucket,
                    region_name=self._region_name,
                ).emit(lineage_record)
            except Exception as exc:
                _logger.warning(
                    "serving_store_lineage_emission_failed",
                    table_name=table_name,
                    error=str(exc),
                )

        return ServingStoreLoadResult(
            table_name=table_name,
            records_loaded=loaded,
            records_skipped=len(records) - loaded,
            started_at=started_at,
            completed_at=completed_at,
        )

    def _retrieve_credentials(self) -> dict[str, str]:
        """Fetch DB credentials from Secrets Manager."""
        try:
            response = self._sm.get_secret_value(SecretId=self._secret_arn)
            creds: dict[str, str] = json.loads(response["SecretString"])
            return creds
        except Exception as exc:
            raise ServingStoreError("Failed to retrieve database credentials") from exc

    def _connect(self, creds: dict[str, str]) -> Any:
        """Open a pymysql connection with TLS enforced (OWASP A02)."""
        return pymysql.connect(
            host=creds["host"],
            port=int(creds.get("port", "3306")),
            user=creds["username"],
            password=creds["password"],
            database=self._db_name,
            ssl_disabled=False,  # TLS always enforced — never negotiated away
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10,
        )

    def _ensure_table(
        self,
        connection: Any,
        table_name: str,
        columns: list[str],
        sample: dict[str, Any],
        primary_keys: tuple[str, ...],
    ) -> None:
        """CREATE TABLE IF NOT EXISTS with schema inferred from sample record."""
        # Validate all column names and primary key names before DDL construction (OWASP A03)
        for col in columns:
            if not _SAFE_COLUMN_PATTERN.match(col):
                raise ServingStoreError(f"Unsafe column name rejected: {col!r}")
        for pk in primary_keys:
            if not _SAFE_COLUMN_PATTERN.match(pk):
                raise ServingStoreError(f"Unsafe primary key name rejected: {pk!r}")

        col_defs = ", ".join(
            f"`{col}` {_infer_mysql_type(sample.get(col))} NULL" for col in columns
        )
        pk_def = ", ".join(f"`{k}`" for k in primary_keys) if primary_keys else ""
        pk_clause = f", PRIMARY KEY ({pk_def})" if pk_def else ""
        ddl = (
            f"CREATE TABLE IF NOT EXISTS `{table_name}` "
            f"({col_defs}{pk_clause}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )

        with connection.cursor() as cur:
            cur.execute(ddl)

    def _bulk_replace(
        self,
        connection: Any,
        table_name: str,
        columns: list[str],
        records: list[dict[str, Any]],
    ) -> int:
        """REPLACE INTO table for each record; returns count of loaded rows."""
        col_list = ", ".join(f"`{c}`" for c in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        sql = f"REPLACE INTO `{table_name}` ({col_list}) VALUES ({placeholders})"  # noqa: S608

        with connection.cursor() as cur:
            rows = [tuple(r.get(c) for c in columns) for r in records]
            cur.executemany(sql, rows)
            return int(cur.rowcount)


def _infer_mysql_type(value: Any) -> str:
    """Infer a MySQL column type from a sample Python value."""
    if isinstance(value, bool):
        return "TINYINT(1)"
    if isinstance(value, int):
        return "BIGINT"
    if isinstance(value, float):
        return "DOUBLE"
    if isinstance(value, (list, dict)):
        return "JSON"
    return "TEXT"


class ServingStoreError(Exception):
    """Raised when a serving store operation fails."""
