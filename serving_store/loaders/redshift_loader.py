"""
Amazon Redshift serving store loader.

Loads analytics datasets into a Redshift Serverless serving warehouse for direct
BI tool access (Power BI, Tableau). Unlike the RDS engine adapters, Redshift is a
columnar MPP warehouse: row-by-row INSERT is an anti-pattern, so this adapter
loads set-based via `COPY` from the analytics Parquet in S3 (supports_s3_bulk_load),
then MERGEs a per-tenant staging table into the target.

Security (OWASP A01):
  - One Redshift *schema* per tenant, inside a fixed connection database
    (`edl_serving` by default) — the isolation boundary a tenant's BI tool
    credential is scoped to, since BI tools connect directly and bypass any
    application-level tenant filter.
  - A read-only Redshift user, GRANTed SELECT on only that tenant's schema
    (including future tables via ALTER DEFAULT PRIVILEGES), is provisioned per
    tenant and stored separately from the writer connection.

Security (OWASP A02, A07):
  - The writer authenticates via IAM (redshift-serverless:GetCredentials) — no
    writer password is ever stored or rotated; TLS is enforced on connect.
  - The reader login is created with an md5 password verifier (Redshift's own
    protocol), so the raw token is never interpolated into DDL.

Security (OWASP A03, A05):
  - All identifiers (schema/table/column/username) are validated against
    safe-identifier regexes before being interpolated; values bound as params.
"""

from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime
from typing import Any, Final

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
import redshift_connector

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
    "RedshiftLoader",
    "ServingStoreError",
    "ServingStoreLoadResult",
    "TransientServingError",
]

# Column concat delimiter and null sentinel for the in-SQL row hash — control
# characters that cannot occur in a business value, so distinct rows never collide.
_HASH_DELIMITER: Final[str] = "CHR(1)"
_HASH_NULL_SENTINEL: Final[str] = "CHR(2)"


@serving_store_registry.register("redshift")
class RedshiftLoader(ServingStoreLoaderInterface):
    """Redshift Serverless engine adapter — container = schema, load via S3 COPY."""

    max_identifier_length = 127  # Redshift identifier length limit
    default_port = 5439
    default_connection_database = "edl_serving"
    supports_s3_bulk_load = True

    # ── Bulk S3 load path (production) ────────────────────────────────────────

    def load_from_s3(
        self,
        analytics_s3_bucket: str,
        analytics_s3_prefix: str,
        table_name: str,
        primary_keys: tuple[str, ...],
        tenant_code: str,
        run_id: str | None = None,
        connection_database: str | None = None,
    ) -> ServingStoreLoadResult:
        """COPY analytics Parquet from S3 into a staging table, then set-based MERGE."""
        container_name = self._validate_and_resolve_container(table_name, primary_keys, tenant_code)
        credentials = self._retrieve_credentials()
        copy_iam_role = credentials.get("copy_iam_role")
        if not copy_iam_role:
            raise ServingStoreError("Redshift connection secret missing 'copy_iam_role'.")

        col_types = self._infer_columns_from_parquet(analytics_s3_bucket, analytics_s3_prefix)

        started_at = datetime.now(UTC).isoformat()
        try:
            connection = self._connect(
                credentials, connection_database or self.default_connection_database
            )
        except Exception as exc:
            raise ServingStoreError(f"Failed to connect to Redshift: {exc}") from exc

        try:
            self._ensure_tenant_container(connection, container_name)
            self._select_container(connection, container_name)
            self._provision_reader_credential(connection, tenant_code, container_name, credentials)
            self._ensure_table_with_types(
                connection, container_name, table_name, col_types, primary_keys
            )
            loaded, skipped = self._copy_and_merge(
                connection,
                container_name,
                table_name,
                col_types,
                primary_keys,
                analytics_s3_bucket,
                analytics_s3_prefix,
                copy_iam_role,
            )
            connection.commit()
        except ServingStoreError:
            connection.rollback()
            raise
        except Exception as exc:
            connection.rollback()
            raise ServingStoreError(
                f"Redshift load failed for table {table_name!r}: {exc}"
            ) from exc
        finally:
            connection.close()

        return self._finalize_load(
            container_name=container_name,
            table_name=table_name,
            total_loaded=loaded,
            total_skipped=skipped,
            started_at=started_at,
            run_id=run_id,
            analytics_s3_bucket=analytics_s3_bucket,
            analytics_s3_prefix=analytics_s3_prefix,
        )

    def _copy_and_merge(
        self,
        connection: Any,
        container_name: str,
        table_name: str,
        col_types: dict[str, str],
        primary_keys: tuple[str, ...],
        analytics_s3_bucket: str,
        analytics_s3_prefix: str,
        copy_iam_role: str,
    ) -> tuple[int, int]:
        """COPY Parquet into a temp staging table, then MERGE only new/changed rows."""
        columns = list(col_types.keys())
        s3_uri = f"s3://{analytics_s3_bucket}/{analytics_s3_prefix.strip().rstrip('/')}/"
        col_defs = ", ".join(f'"{c}" {ctype}' for c, ctype in col_types.items())
        target = f'"{container_name}"."{table_name}"'
        hash_expr = _row_hash_expr("s", columns)
        target_hash_expr = _row_hash_expr("s", columns)  # source alias in the NOT EXISTS check
        on_clause = " AND ".join(f'target."{k}" = src."{k}"' for k in primary_keys)
        pk_match = " AND ".join(f't."{k}" = s."{k}"' for k in primary_keys)
        all_columns = [*columns, "_row_hash", "_synced_at"]
        update_set = ", ".join(f'"{c}" = src."{c}"' for c in all_columns if c not in primary_keys)
        insert_cols = ", ".join(f'"{c}"' for c in all_columns)
        insert_vals = ", ".join(f'src."{c}"' for c in all_columns)
        select_cols = ", ".join(f's."{c}"' for c in columns)

        with connection.cursor() as cur:
            # Staging holds only business columns, in Parquet order, for a positional COPY.
            cur.execute(f'CREATE TEMP TABLE "_stg_{table_name}" ({col_defs})')
            cur.execute(
                f'COPY "_stg_{table_name}" FROM %s IAM_ROLE %s FORMAT AS PARQUET',
                (s3_uri, copy_iam_role),
            )
            cur.execute(f'SELECT COUNT(*) FROM "_stg_{table_name}"')  # noqa: S608
            total = int(cur.fetchone()[0])

            # New-or-changed = staging rows with no target row sharing pk AND the same hash.
            changed_filter = (
                f'FROM "_stg_{table_name}" s WHERE NOT EXISTS ('  # noqa: S608
                f"SELECT 1 FROM {target} t WHERE {pk_match} "
                f'AND t."_row_hash" = SHA2({target_hash_expr}, 256))'
            )
            cur.execute(f"SELECT COUNT(*) {changed_filter}")
            loaded = int(cur.fetchone()[0])

            if loaded:
                source = (
                    f'SELECT {select_cols}, SHA2({hash_expr}, 256) AS "_row_hash", '
                    f'GETDATE() AS "_synced_at" {changed_filter}'
                )
                cur.execute(
                    f"MERGE INTO {target} AS target USING ({source}) AS src "  # noqa: S608
                    f"ON {on_clause} "
                    f"WHEN MATCHED THEN UPDATE SET {update_set} "
                    f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
                )
        return loaded, total - loaded

    def _infer_columns_from_parquet(self, bucket: str, prefix: str) -> dict[str, str]:
        """Read the Parquet arrow schema (metadata only) from the first S3 object."""
        clean = prefix.strip().rstrip("/") + "/"
        if ".." in clean or clean.startswith("/"):
            raise ValueError(f"Unsafe S3 prefix rejected: {clean!r}")
        s3 = boto3.client("s3", region_name=self._region_name)
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=clean):
            for obj in page.get("Contents", []):
                if not obj["Key"].endswith(".parquet"):
                    continue
                raw = s3.get_object(Bucket=bucket, Key=obj["Key"])
                parquet_file = pq.ParquetFile(io.BytesIO(raw["Body"].read()))  # type: ignore[no-untyped-call]
                return self._schema_to_column_types(parquet_file.schema_arrow)
        raise ServingStoreError(f"No Parquet objects found under s3://{bucket}/{clean}")

    @staticmethod
    def _schema_to_column_types(schema: pa.Schema) -> dict[str, str]:
        """Map an arrow schema to {column: Redshift type}, rejecting unsafe/reserved names."""
        col_types: dict[str, str] = {}
        for field in schema:
            col = field.name
            if col in RESERVED_COLUMNS:
                raise ServingStoreError(f"Column name {col!r} is reserved for internal use")
            if not SAFE_COLUMN_PATTERN.match(col):
                raise ServingStoreError(f"Unsafe column name rejected: {col!r}")
            col_types[col] = _arrow_to_redshift_type(field.type)
        return col_types

    # ── Shared DDL / connection (used by the S3 path) ─────────────────────────

    def _connect(self, credentials: dict[str, str], connection_database: str) -> Any:
        """Open a redshift_connector connection with IAM auth and TLS (OWASP A02, A07)."""
        return redshift_connector.connect(
            iam=True,
            is_serverless=True,
            host=credentials["host"],
            port=int(credentials.get("port", self.default_port)),
            database=connection_database,
            region=credentials.get("region", self._region_name),
            ssl=True,
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
        """Create/refresh a per-tenant read-only user, scoped to container_name only."""
        secret_name = f"edl/serving-store/{tenant_code}/redshift/reader-credentials"
        username = reader_username(tenant_code, self.max_identifier_length)
        connection_database = writer_creds.get("database", self.default_connection_database)
        password = self._get_or_create_reader_password(
            secret_name, username, container_name, writer_creds, connection_database
        )
        # md5 verifier (hex only, safe to inline) — Redshift never sees the raw token,
        # and this bypasses the default password-complexity rules token_urlsafe may fail.
        md5_pw = (
            "md5"
            + hashlib.md5((password + username).encode("utf-8"), usedforsecurity=False).hexdigest()
        )
        with connection.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_user WHERE usename = %s", (username,))
            if cur.fetchone() is None:
                cur.execute(f"CREATE USER \"{username}\" PASSWORD '{md5_pw}'")
            cur.execute(f'GRANT USAGE ON SCHEMA "{container_name}" TO "{username}"')
            cur.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{container_name}" TO "{username}"')
            cur.execute(
                f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{container_name}" '
                f'GRANT SELECT ON TABLES TO "{username}"'
            )
        connection.commit()

    def _ensure_table_with_types(
        self,
        connection: Any,
        container_name: str,
        table_name: str,
        col_types: dict[str, str],
        primary_keys: tuple[str, ...],
    ) -> None:
        """CREATE TABLE IF NOT EXISTS from an already-resolved {column: type} map."""
        col_defs = ", ".join(f'"{col}" {ctype}' for col, ctype in col_types.items())
        pk_def = ", ".join(f'"{k}"' for k in primary_keys)
        ddl = (
            f'CREATE TABLE IF NOT EXISTS "{container_name}"."{table_name}" '
            f'({col_defs}, "_row_hash" CHAR(64), "_synced_at" TIMESTAMP, '
            f"PRIMARY KEY ({pk_def}))"
        )
        with connection.cursor() as cur:
            cur.execute(ddl)

    def _ensure_table(
        self,
        connection: Any,
        container_name: str,
        table_name: str,
        columns: list[str],
        sample: dict[str, Any],
        primary_keys: tuple[str, ...],
    ) -> None:
        """Sample-based table creation (row-path parity); delegates to _ensure_table_with_types."""
        col_types: dict[str, str] = {}
        for col in columns:
            if col in RESERVED_COLUMNS:
                raise ServingStoreError(f"Column name {col!r} is reserved for internal use")
            if not SAFE_COLUMN_PATTERN.match(col):
                raise ServingStoreError(f"Unsafe column name rejected: {col!r}")
            col_types[col] = _infer_redshift_type(sample.get(col))
        self._ensure_table_with_types(
            connection, container_name, table_name, col_types, primary_keys
        )

    # ── Row-batch path: unsupported on Redshift (columnar MPP; use load_from_s3) ─

    def _read_existing_hashes(
        self,
        connection: Any,
        container_name: str,
        table_name: str,
        primary_keys: tuple[str, ...],
        pk_tuples: list[tuple[Any, ...]],
    ) -> dict[tuple[Any, ...], str]:
        raise ServingStoreError(
            "Redshift loads via load_from_s3 (S3 COPY); the row-batch path is not supported."
        )

    def _bulk_upsert(
        self,
        connection: Any,
        container_name: str,
        table_name: str,
        columns: list[str],
        primary_keys: tuple[str, ...],
        changed: list[tuple[dict[str, Any], str]],
    ) -> int:
        raise ServingStoreError(
            "Redshift loads via load_from_s3 (S3 COPY); the row-batch path is not supported."
        )


def _row_hash_expr(alias: str, columns: list[str]) -> str:
    """Build a deterministic, null-safe SHA2 input over an alias's business columns."""
    parts = [
        f'COALESCE(CAST({alias}."{col}" AS VARCHAR), {_HASH_NULL_SENTINEL})' for col in columns
    ]
    return f" || {_HASH_DELIMITER} || ".join(parts)


def _infer_redshift_type(value: Any) -> str:
    """Infer a Redshift column type from a sample Python value (never TEXT — truncates to 256)."""
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int):
        return "BIGINT"
    if isinstance(value, float):
        return "DOUBLE PRECISION"
    if isinstance(value, (list, dict)):
        return "SUPER"
    return "VARCHAR(65535)"


def _arrow_to_redshift_type(arrow_type: pa.DataType) -> str:
    """Map a pyarrow type to a Redshift column type (VARCHAR(65535) for strings, SUPER nested)."""
    if pa.types.is_boolean(arrow_type):
        return "BOOLEAN"
    if pa.types.is_integer(arrow_type):
        return "BIGINT"
    if pa.types.is_floating(arrow_type):
        return "DOUBLE PRECISION"
    if pa.types.is_decimal(arrow_type):
        return f"DECIMAL({arrow_type.precision},{arrow_type.scale})"
    if pa.types.is_timestamp(arrow_type):
        return "TIMESTAMP"
    if pa.types.is_date(arrow_type):
        return "DATE"
    if pa.types.is_nested(arrow_type):
        return "SUPER"
    return "VARCHAR(65535)"
