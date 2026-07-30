"""
SQL Server / Azure SQL serving store loader.

One adapter serves both `sqlserver` (RDS-provisioned, license-included) and
`azure_sql` (always BYO-DB — Azure SQL cannot be provisioned on AWS) — both
speak the same T-SQL dialect; only the credential's origin differs.

Security (OWASP A01):
  - One schema per tenant, inside a fixed connection database (`datalake_serving`
    by default, or a tenant-supplied database for BYO-DB) — the isolation
    boundary a tenant's BI tool credential is scoped to.
  - A read-only login, GRANTed SELECT on only that tenant's schema
    (`GRANT SELECT ON SCHEMA::`, deliberately not `db_datareader`, which would
    expose every other tenant's schema too), is provisioned per tenant.

Security (OWASP A03, A05, A07):
  - All SQL is parameterized; identifiers (schema/table/column/login) are
    validated against safe-identifier regexes before being interpolated.

T-SQL portability note: unlike MySQL/Postgres, T-SQL supports neither
row-value `IN` lists nor `REPLACE INTO`/`ON CONFLICT` — `_read_existing_hashes`
uses a `VALUES`-derived table join instead, and `_bulk_upsert` uses `MERGE`
with a smaller chunk size (one statement's `VALUES` list per batch, not a
driver-level `executemany` loop).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from contracts.resource_naming import secret_path
from serving_store.interfaces.loader_interface import (
    RESERVED_COLUMNS,
    SAFE_COLUMN_PATTERN,
    SAFE_CONTAINER_PATTERN,
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
    "SqlServerLoader",
    "TransientServingError",
]

_UPSERT_CHUNK_SIZE: Final[int] = 500


@serving_store_registry.register("sqlserver")
@serving_store_registry.register("azure_sql")
class SqlServerLoader(ServingStoreLoaderInterface):
    """SQL Server / Azure SQL engine adapter — container = schema."""

    max_identifier_length = 128  # SQL Server identifier length limit
    default_port = 1433
    default_connection_database = "datalake_serving"

    def _ensure_connection_database(
        self, credentials: dict[str, str], connection_database: str
    ) -> None:
        """Bootstrap connection_database from `master`, platform-provisioned RDS only.

        Azure SQL is always BYO-DB — its database already exists and `master`-level
        CREATE DATABASE is unavailable — so it (and direct unit instantiation) is skipped.
        """
        if self.engine_id != "sqlserver":
            return
        import pymssql

        if not SAFE_CONTAINER_PATTERN.match(connection_database):
            raise ServingStoreError(f"Unsafe connection database rejected: {connection_database!r}")
        admin = pymssql.connect(
            server=credentials["host"],
            port=str(credentials.get("port", self.default_port)),
            user=credentials["username"],
            password=credentials["password"],
            database="master",
            login_timeout=10,
            as_dict=True,
            autocommit=True,  # CREATE DATABASE cannot run inside a transaction block
        )
        try:
            with admin.cursor() as cur:
                cur.execute("SELECT 1 FROM sys.databases WHERE name = %s", (connection_database,))
                if cur.fetchone() is None:
                    cur.execute(f"CREATE DATABASE [{connection_database}]")
        finally:
            admin.close()

    def _connect(self, credentials: dict[str, str], connection_database: str) -> Any:
        """Open a pymssql connection. Azure SQL/RDS SQL Server both mandate TLS on the
        endpoint itself; FreeTDS negotiates encryption automatically against them."""
        import pymssql

        return pymssql.connect(
            server=credentials["host"],
            port=str(credentials.get("port", self.default_port)),
            user=credentials["username"],
            password=credentials["password"],
            database=connection_database,
            login_timeout=10,
            as_dict=True,
        )

    def _ensure_tenant_container(self, connection: Any, container_name: str) -> None:
        with connection.cursor() as cur:
            cur.execute("SELECT 1 FROM sys.schemas WHERE name = %s", (container_name,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE SCHEMA [{container_name}]")
        connection.commit()

    def _select_container(self, connection: Any, container_name: str) -> None:
        """No-op — T-SQL has no per-session default-schema equivalent to `USE`/
        `search_path`; every statement below schema-qualifies explicitly instead."""

    def _provision_reader_credential(
        self, connection: Any, tenant_code: str, container_name: str, writer_creds: dict[str, str]
    ) -> None:
        """Create/refresh a per-tenant read-only login, scoped to container_name only."""
        secret_name = secret_path("serving-store", tenant_code, "sqlserver", "reader-credentials")
        username = reader_username(tenant_code, self.max_identifier_length)
        connection_database = self._current_database(connection)
        password = self._get_or_create_reader_password(
            secret_name, username, container_name, writer_creds, connection_database
        )
        with connection.cursor() as cur:
            cur.execute("SELECT 1 FROM sys.sql_logins WHERE name = %s", (username,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE LOGIN [{username}] WITH PASSWORD = %s", (password,))
            cur.execute("SELECT 1 FROM sys.database_principals WHERE name = %s", (username,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE USER [{username}] FOR LOGIN [{username}]")
            cur.execute(f"GRANT SELECT ON SCHEMA::[{container_name}] TO [{username}]")
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
        """CREATE TABLE (T-SQL has no IF NOT EXISTS clause here — check, then create)."""
        for col in columns:
            if col in RESERVED_COLUMNS:
                raise ServingStoreError(f"Column name {col!r} is reserved for internal use")
            if not SAFE_COLUMN_PATTERN.match(col):
                raise ServingStoreError(f"Unsafe column name rejected: {col!r}")

        with connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM sys.tables t JOIN sys.schemas s ON t.schema_id = s.schema_id "
                "WHERE s.name = %s AND t.name = %s",
                (container_name, table_name),
            )
            if cur.fetchone() is not None:
                return
            col_defs = ", ".join(
                f"[{col}] {_infer_sqlserver_type(sample.get(col))} NULL" for col in columns
            )
            pk_def = ", ".join(f"[{k}]" for k in primary_keys)
            ddl = (
                f"CREATE TABLE [{container_name}].[{table_name}] "
                f"({col_defs}, [_row_hash] CHAR(64) NULL, [_synced_at] DATETIME2 NULL, "
                f"PRIMARY KEY ({pk_def}))"
            )
            cur.execute(ddl)
        connection.commit()

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
        pk_cols = ", ".join(f"[{k}]" for k in primary_keys)
        value_cols = ", ".join(f"pk{i}" for i in range(len(primary_keys)))
        join_clause = " AND ".join(f"t.[{k}] = v.pk{i}" for i, k in enumerate(primary_keys))
        row_placeholder = "(" + ", ".join(["%s"] * len(primary_keys)) + ")"
        values_clause = ", ".join([row_placeholder] * len(pk_tuples))
        sql = (
            f"SELECT t.{pk_cols}, t.[_row_hash] "  # noqa: S608  # nosec B608 — identifiers allowlisted; values bound
            f"FROM [{container_name}].[{table_name}] t "
            f"JOIN (VALUES {values_clause}) AS v({value_cols}) ON {join_clause}"
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
        non_pk_columns = [c for c in all_columns if c not in primary_keys]
        source_cols = ", ".join(f"src{i}" for i in range(len(all_columns)))
        insert_cols = ", ".join(f"[{c}]" for c in all_columns)
        insert_values = ", ".join(f"source.src{i}" for i in range(len(all_columns)))
        update_clause = ", ".join(
            f"target.[{c}] = source.src{all_columns.index(c)}" for c in non_pk_columns
        )
        on_clause = " AND ".join(
            f"target.[{k}] = source.src{all_columns.index(k)}" for k in primary_keys
        )
        row_placeholder = "(" + ", ".join(["%s"] * len(all_columns)) + ")"
        now = datetime.now(UTC).isoformat()
        rows = [(*(record.get(c) for c in columns), row_hash, now) for record, row_hash in changed]

        total = 0
        with connection.cursor() as cur:
            for start in range(0, len(rows), _UPSERT_CHUNK_SIZE):
                chunk = rows[start : start + _UPSERT_CHUNK_SIZE]
                values_clause = ", ".join([row_placeholder] * len(chunk))
                sql = (
                    f"MERGE INTO [{container_name}].[{table_name}] AS target "  # noqa: S608  # nosec B608 — identifiers allowlisted; values bound
                    f"USING (VALUES {values_clause}) AS source({source_cols}) "
                    f"ON {on_clause} "
                    f"WHEN MATCHED THEN UPDATE SET {update_clause} "
                    f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_values});"
                )
                params = [value for row in chunk for value in row]
                cur.execute(sql, params)
                total += len(chunk)
        connection.commit()
        return total

    @staticmethod
    def _current_database(connection: Any) -> str:
        with connection.cursor() as cur:
            cur.execute("SELECT DB_NAME() AS db_name")
            row = cur.fetchone()
            return str(row["db_name"])


def _infer_sqlserver_type(value: Any) -> str:
    """Infer a SQL Server column type from a sample Python value."""
    if isinstance(value, bool):
        return "BIT"
    if isinstance(value, int):
        return "BIGINT"
    if isinstance(value, float):
        return "FLOAT"
    if isinstance(value, (list, dict)):
        return "NVARCHAR(MAX)"
    return "NVARCHAR(MAX)"
