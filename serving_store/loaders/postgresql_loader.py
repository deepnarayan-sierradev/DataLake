"""
PostgreSQL serving store loader.

Loads analytics datasets into a PostgreSQL serving database for direct BI tool
access (Power BI, Tableau).

Security (OWASP A01):
  - One PostgreSQL *schema* per tenant, inside a fixed connection database
    (`edl_serving` by default, or a tenant-supplied database for BYO-DB) —
    the isolation boundary a tenant's BI tool credential is scoped to, since
    BI tools connect directly and bypass any application-level tenant filter.
  - A read-only PostgreSQL role, GRANTed SELECT on only that tenant's schema
    (including future tables, via ALTER DEFAULT PRIVILEGES), is provisioned
    per tenant and stored separately from the writer credential.

Security (OWASP A03, A05, A07):
  - All SQL is parameterized; identifiers (schema/table/column/username) are
    validated against safe-identifier regexes before being interpolated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

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
    "PostgreSqlLoader",
    "ServingStoreError",
    "ServingStoreLoadResult",
    "TransientServingError",
]

_UPSERT_CHUNK_SIZE: Final[int] = 2_000


@serving_store_registry.register("postgresql")
class PostgreSqlLoader(ServingStoreLoaderInterface):
    """PostgreSQL engine adapter — container = schema."""

    max_identifier_length = 63  # Postgres identifier length limit
    default_port = 5432
    default_connection_database = "edl_serving"

    def _ensure_connection_database(
        self, credentials: dict[str, str], connection_database: str
    ) -> None:
        """Bootstrap connection_database from the always-present `postgres` admin DB."""
        import psycopg

        if not SAFE_CONTAINER_PATTERN.match(connection_database):
            raise ServingStoreError(f"Unsafe connection database rejected: {connection_database!r}")
        admin = psycopg.connect(
            host=credentials["host"],
            port=int(credentials.get("port", self.default_port)),
            user=credentials["username"],
            password=credentials["password"],
            dbname="postgres",
            sslmode="require",
            connect_timeout=10,
            autocommit=True,  # CREATE DATABASE cannot run inside a transaction block
        )
        try:
            with admin.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (connection_database,))
                if cur.fetchone() is None:
                    cur.execute(f'CREATE DATABASE "{connection_database}"')
        finally:
            admin.close()

    def _connect(self, credentials: dict[str, str], connection_database: str) -> Any:
        """Open a psycopg connection with TLS enforced (OWASP A02)."""
        import psycopg
        import psycopg.rows

        return psycopg.connect(
            host=credentials["host"],
            port=int(credentials.get("port", self.default_port)),
            user=credentials["username"],
            password=credentials["password"],
            dbname=connection_database,
            sslmode="require",  # TLS always enforced — never negotiated away
            connect_timeout=10,
            row_factory=psycopg.rows.dict_row,
        )

    def _ensure_tenant_container(self, connection: Any, container_name: str) -> None:
        with connection.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{container_name}"')
        connection.commit()

    def _select_container(self, connection: Any, container_name: str) -> None:
        """Set search_path so unqualified table names resolve to the tenant schema."""
        with connection.cursor() as cur:
            cur.execute(f'SET search_path TO "{container_name}"')

    def _provision_reader_credential(
        self, connection: Any, tenant_code: str, container_name: str, writer_creds: dict[str, str]
    ) -> None:
        """Create/refresh a per-tenant read-only role, scoped to container_name only."""
        secret_name = f"edl/serving-store/{tenant_code}/postgresql/reader-credentials"
        username = reader_username(tenant_code, self.max_identifier_length)
        connection_database = connection.info.dbname
        password = self._get_or_create_reader_password(
            secret_name, username, container_name, writer_creds, connection_database
        )
        with connection.cursor() as cur:
            # Postgres has no CREATE ROLE IF NOT EXISTS — check first (a parameter
            # placeholder can't be substituted inside a dollar-quoted DO block body,
            # so that's not a viable shortcut here).
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (username,))
            if cur.fetchone() is None:
                cur.execute(f'CREATE ROLE "{username}" LOGIN PASSWORD %s', (password,))
            cur.execute(f'GRANT USAGE ON SCHEMA "{container_name}" TO "{username}"')
            cur.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{container_name}" TO "{username}"')
            # Without this, a table created by a *future* load (a new entity type for
            # this tenant) stays invisible to the reader until someone re-runs the GRANT.
            cur.execute(
                f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{container_name}" '
                f'GRANT SELECT ON TABLES TO "{username}"'
            )
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

        col_defs = ", ".join(f'"{col}" {_infer_postgres_type(sample.get(col))}' for col in columns)
        pk_def = ", ".join(f'"{k}"' for k in primary_keys)
        ddl = (
            f'CREATE TABLE IF NOT EXISTS "{table_name}" '
            f'({col_defs}, "_row_hash" CHAR(64), "_synced_at" TIMESTAMP, '
            f"PRIMARY KEY ({pk_def}))"
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
        pk_cols = ", ".join(f'"{k}"' for k in primary_keys)
        row_placeholder = "(" + ", ".join(["%s"] * len(primary_keys)) + ")"
        placeholders = ", ".join([row_placeholder] * len(pk_tuples))
        # Identifiers validated by load_batches()/_ensure_table() above; values bound as
        # params. Postgres supports row-value IN natively — unlike SQL Server (see
        # sqlserver_loader.py's VALUES-join workaround for the non-portable case).
        sql = (
            f'SELECT {pk_cols}, "_row_hash" FROM "{table_name}" '  # noqa: S608
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
        col_list = ", ".join(f'"{c}"' for c in all_columns)
        placeholders = ", ".join(["%s"] * len(all_columns))
        update_clause = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in all_columns)
        pk_cols = ", ".join(f'"{k}"' for k in primary_keys)
        # Identifiers validated by load_batches()/_ensure_table() above; values bound as params.
        sql = (
            f'INSERT INTO "{table_name}" ({col_list}) VALUES ({placeholders}) '  # noqa: S608
            f"ON CONFLICT ({pk_cols}) DO UPDATE SET {update_clause}"
        )
        now = datetime.now(UTC).isoformat()
        rows = [(*(record.get(c) for c in columns), row_hash, now) for record, row_hash in changed]
        total = 0
        with connection.cursor() as cur:
            for start in range(0, len(rows), _UPSERT_CHUNK_SIZE):
                chunk = rows[start : start + _UPSERT_CHUNK_SIZE]
                cur.executemany(sql, chunk)
                total += len(chunk)
        return total


def _infer_postgres_type(value: Any) -> str:
    """Infer a PostgreSQL column type from a sample Python value."""
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        return "BIGINT"
    if isinstance(value, float):
        return "DOUBLE PRECISION"
    if isinstance(value, (list, dict)):
        return "JSONB"
    return "TEXT"
