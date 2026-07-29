"""
MySQL RDS serving store loader.

Loads analytics datasets into a MySQL RDS serving database for direct BI tool
access (Power BI, Tableau).

Security (OWASP A01):
  - One MySQL *database* per tenant (`validate_tenant_code()` gates
    `CREATE DATABASE IF NOT EXISTS {tenant_code}`) — the isolation boundary a
    tenant's BI tool credential is scoped to, since BI tools connect directly
    and bypass any application-level tenant filter.
  - A read-only MySQL user, GRANTed SELECT on only that tenant's database, is
    provisioned per tenant and stored separately from the writer credential —
    that reader credential is what's handed to the tenant's BI connection.

Security (OWASP A03, A05, A07):
  - All SQL is parameterized; identifiers (database/table/column/username)
    are validated against safe-identifier regexes before being interpolated.
  - Credentials never appear in logs or exceptions.
  - Connection closed in finally block to prevent resource leaks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import pymysql
import pymysql.cursors

from serving_store.interfaces.loader_interface import (
    RESERVED_COLUMNS,
    SAFE_COLUMN_PATTERN,
    ServingStoreError,
    ServingStoreLoaderInterface,
    ServingStoreLoadResult,
    TransientServingError,
    reader_username,
)
from serving_store.registry import serving_store_registry

__all__ = [
    "ServingStoreError",
    "ServingStoreLoadResult",
    "ServingStoreLoader",
    "TransientServingError",
]

_UPSERT_CHUNK_SIZE: Final[int] = 2_000


@serving_store_registry.register("mysql_rds")
class ServingStoreLoader(ServingStoreLoaderInterface):
    """MySQL RDS engine adapter — container = database."""

    max_identifier_length = 32  # MySQL username length limit
    default_port = 3306

    def _connect(self, credentials: dict[str, str], connection_database: str) -> Any:
        """Open a pymysql connection (no database selected yet) with TLS enforced (OWASP A02)."""
        return pymysql.connect(
            host=credentials["host"],
            port=int(credentials.get("port", self.default_port)),
            user=credentials["username"],
            password=credentials["password"],
            ssl_disabled=False,  # TLS always enforced — never negotiated away
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10,
        )

    def _ensure_tenant_container(self, connection: Any, container_name: str) -> None:
        with connection.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{container_name}` CHARACTER SET utf8mb4")

    def _select_container(self, connection: Any, container_name: str) -> None:
        connection.select_db(container_name)

    def _provision_reader_credential(
        self, connection: Any, tenant_code: str, container_name: str, writer_creds: dict[str, str]
    ) -> None:
        """Create/refresh a per-tenant read-only MySQL user, scoped to container_name only."""
        secret_name = f"edl/serving-store/{tenant_code}/mysql_rds/reader-credentials"
        username = reader_username(tenant_code, self.max_identifier_length)
        password = self._get_or_create_reader_password(
            secret_name, username, container_name, writer_creds, container_name
        )
        with connection.cursor() as cur:
            cur.execute(f"CREATE USER IF NOT EXISTS '{username}'@'%' IDENTIFIED BY %s", (password,))
            cur.execute(f"GRANT SELECT ON `{container_name}`.* TO '{username}'@'%'")
        connection.commit()

    def _ensure_table(
        self,
        connection: Any,
        container_name: str,
        table_name: str,
        columns: list[str],
        sample: dict[str, Any],
        primary_keys: tuple[str, ...],
    ) -> None:
        """CREATE TABLE IF NOT EXISTS with schema inferred from sample record."""
        for col in columns:
            if col in RESERVED_COLUMNS:
                raise ServingStoreError(f"Column name {col!r} is reserved for internal use")
            if not SAFE_COLUMN_PATTERN.match(col):
                raise ServingStoreError(f"Unsafe column name rejected: {col!r}")

        col_defs = ", ".join(
            f"`{col}` {_infer_mysql_type(sample.get(col))} NULL" for col in columns
        )
        pk_def = ", ".join(f"`{k}`" for k in primary_keys)
        ddl = (
            f"CREATE TABLE IF NOT EXISTS `{table_name}` "
            f"({col_defs}, `_row_hash` CHAR(64) NULL, `_synced_at` DATETIME NULL, "
            f"PRIMARY KEY ({pk_def})) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
        with connection.cursor() as cur:
            cur.execute(ddl)

    def _read_existing_hashes(
        self,
        connection: Any,
        container_name: str,
        table_name: str,
        primary_keys: tuple[str, ...],
        pk_tuples: list[tuple[Any, ...]],
    ) -> dict[tuple[Any, ...], str]:
        if not pk_tuples:
            return {}
        pk_cols = ", ".join(f"`{k}`" for k in primary_keys)
        row_placeholder = "(" + ", ".join(["%s"] * len(primary_keys)) + ")"
        placeholders = ", ".join([row_placeholder] * len(pk_tuples))
        # Identifiers validated by load_batches()/_ensure_table() above; values bound as params.
        sql = (
            f"SELECT {pk_cols}, `_row_hash` FROM `{table_name}` "  # noqa: S608  # nosec B608 — identifiers allowlisted; values bound
            f"WHERE ({pk_cols}) IN ({placeholders})"
        )
        params = [value for pk_tuple in pk_tuples for value in pk_tuple]
        with connection.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return {tuple(row[pk] for pk in primary_keys): row["_row_hash"] for row in rows}

    def _bulk_upsert(
        self,
        connection: Any,
        container_name: str,
        table_name: str,
        columns: list[str],
        primary_keys: tuple[str, ...],
        changed: list[tuple[dict[str, Any], str]],
    ) -> int:
        if not changed:
            return 0
        all_columns = [*columns, "_row_hash", "_synced_at"]
        col_list = ", ".join(f"`{c}`" for c in all_columns)
        placeholders = ", ".join(["%s"] * len(all_columns))
        sql = f"REPLACE INTO `{table_name}` ({col_list}) VALUES ({placeholders})"  # noqa: S608
        now = datetime.now(UTC).isoformat()
        rows = [(*(record.get(c) for c in columns), row_hash, now) for record, row_hash in changed]
        total = 0
        with connection.cursor() as cur:
            for start in range(0, len(rows), _UPSERT_CHUNK_SIZE):
                chunk = rows[start : start + _UPSERT_CHUNK_SIZE]
                cur.executemany(sql, chunk)
                total += cur.rowcount
        return total


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
