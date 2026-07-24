"""
Serving store loader interface — the pluggable multi-engine seam.

Concrete/shared here (identical across every engine, proven by the original MySQL-only
implementation): credential retrieval, reader-credential secret get-or-create, batch/session
orchestration, and row-hash computation. Abstract: only genuine SQL-dialect differences per
engine (container DDL, upsert syntax, hash-diff query shape, GRANT syntax).

Mirrors connector_runtime/interfaces/connector_interface.py's ABC pattern on the write side.
"""

from __future__ import annotations

import abc
import hashlib
import json
import re
import secrets as _secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import boto3

from contracts.identifier_policy import validate_tenant_code
from governance.lineage_record import LineageEmitter, build_serving_store_lineage
from observability.metrics_emitter import CloudWatchMetricsEmitter
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# Validate database/schema/table/column identifiers before any DDL/DML (OWASP A03, A05).
SAFE_CONTAINER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SAFE_COLUMN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
RESERVED_COLUMNS: Final[frozenset[str]] = frozenset({"_row_hash", "_synced_at"})


@dataclass(frozen=True)
class ServingStoreLoadResult:
    """Summary of one serving store load operation."""

    database_name: str  # tenant-scoped container: a database (MySQL) or schema (else)
    table_name: str
    records_loaded: int
    records_skipped: int
    started_at: str  # ISO-8601 UTC
    completed_at: str  # ISO-8601 UTC


class ServingStoreError(Exception):
    """Raised when a serving store operation fails."""


class TransientServingError(ServingStoreError):
    """Raised for a failure Step Functions should retry (e.g. an approaching Lambda timeout)."""


def compute_row_hash(record: dict[str, Any], columns: list[str]) -> str:
    """Deterministic content hash over a record's business columns."""
    canonical = json.dumps([record.get(c) for c in columns], sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def reader_username(tenant_code: str, max_length: int) -> str:
    """Deterministic, length-bounded DB username for a tenant's reader login."""
    suffix = hashlib.sha256(tenant_code.encode("utf-8")).hexdigest()[:8]
    base = tenant_code.replace("-", "_") + "_ro"
    max_base_len = max_length - len(suffix) - 1
    return f"{base[:max_base_len]}_{suffix}"


class ServingStoreLoaderInterface(abc.ABC):
    """
    Loads canonical records from S3 analytics output into a tenant-scoped relational
    database. One instance per serving database server/engine; `load()`/`load_batches()`
    resolve the tenant-scoped container (database or schema) per call.
    """

    #: Max identifier (username) length this engine's server allows.
    max_identifier_length: int = 63
    #: Default port when a writer credential omits one.
    default_port: int = 5432
    #: Default connection database for platform-provisioned Postgres/SQL Server instances
    #: (irrelevant for MySQL, where tenant_code is itself the connection database).
    default_connection_database: str = "edl_serving"
    #: Whether this engine loads directly from S3 (COPY) instead of Python row batches.
    supports_s3_bulk_load: bool = False
    #: The registry engine_id this loader was resolved as; set by the registry so an
    #: adapter shared across engines (SQL Server / Azure SQL) can tell them apart.
    engine_id: str = ""

    def __init__(
        self,
        secret_arn: str,
        region_name: str,
        metrics_emitter: CloudWatchMetricsEmitter | None = None,
        environment: str = "dev",
        governance_s3_bucket: str | None = None,
        db_host: str | None = None,
        db_port: int | None = None,
    ) -> None:
        self._secret_arn = secret_arn
        self._region_name = region_name
        self._metrics_emitter = metrics_emitter
        self._environment = environment
        self._governance_s3_bucket = governance_s3_bucket
        self._db_host = db_host
        self._db_port = db_port
        self._sm: Any = boto3.client("secretsmanager", region_name=region_name)

    # ── Public API (shared across every engine) ──────────────────────────────

    def load(
        self,
        records: list[dict[str, Any]],
        table_name: str,
        primary_keys: tuple[str, ...],
        tenant_code: str,
        run_id: str | None = None,
        analytics_s3_bucket: str | None = None,
        analytics_s3_prefix: str | None = None,
        connection_database: str | None = None,
    ) -> ServingStoreLoadResult:
        """Convenience wrapper: load a single already-materialised batch of records."""
        return self.load_batches(
            [records],
            table_name,
            primary_keys,
            tenant_code,
            run_id=run_id,
            analytics_s3_bucket=analytics_s3_bucket,
            analytics_s3_prefix=analytics_s3_prefix,
            connection_database=connection_database,
        )

    def load_batches(
        self,
        record_batches: Iterable[list[dict[str, Any]]],
        table_name: str,
        primary_keys: tuple[str, ...],
        tenant_code: str,
        run_id: str | None = None,
        analytics_s3_bucket: str | None = None,
        analytics_s3_prefix: str | None = None,
        connection_database: str | None = None,
    ) -> ServingStoreLoadResult:
        """
        Load one or more record batches (e.g. Parquet row groups).

        Only new-or-changed rows are upserted per batch — memory stays bounded regardless
        of total record count, since batches are consumed one at a time rather than
        materialised as a single list.

        Args:
            record_batches: Batches of records with a consistent schema.
            table_name:     Target table name, plain/unscoped (tenant scoping applies to
                             the containing database/schema, not the table name).
            primary_keys:   Fields forming the upsert key and the diff key.
            tenant_code:    Validated tenant code slug — becomes the physical container
                             (database or schema) name (OWASP A01).
            run_id:              Pipeline run ID; required for lineage emission.
            analytics_s3_bucket: Source analytics S3 bucket; required for lineage.
            analytics_s3_prefix: Source analytics S3 prefix; required for lineage.
            connection_database: Fixed top-level database to connect to before
                                  creating/using the tenant schema (Postgres/SQL Server
                                  only; ignored by engines where tenant_code is itself the
                                  database). None defaults to `default_connection_database`.

        Raises:
            ServingStoreError on connection, DDL, or DML failure.
            ValueError on invalid tenant_code, table_name, or primary_keys.
        """
        container_name = self._validate_and_resolve_container(table_name, primary_keys, tenant_code)

        started_at = datetime.now(UTC).isoformat()
        total_loaded, total_skipped = self._run_session(
            record_batches,
            table_name,
            primary_keys,
            tenant_code,
            container_name,
            connection_database or self.default_connection_database,
        )
        return self._finalize_load(
            container_name=container_name,
            table_name=table_name,
            total_loaded=total_loaded,
            total_skipped=total_skipped,
            started_at=started_at,
            run_id=run_id,
            analytics_s3_bucket=analytics_s3_bucket,
            analytics_s3_prefix=analytics_s3_prefix,
        )

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
        """Bulk-load analytics Parquet straight from S3 (only if supports_s3_bulk_load)."""
        raise ServingStoreError(
            f"{type(self).__name__} does not support S3 bulk load; use load_batches()."
        )

    def _finalize_load(
        self,
        container_name: str,
        table_name: str,
        total_loaded: int,
        total_skipped: int,
        started_at: str,
        run_id: str | None,
        analytics_s3_bucket: str | None,
        analytics_s3_prefix: str | None,
    ) -> ServingStoreLoadResult:
        """Shared post-load bookkeeping (log, metrics, lineage, result) for every load path."""
        completed_at = datetime.now(UTC).isoformat()

        _logger.info(
            "serving_store_load_complete",
            database_name=container_name,
            table_name=table_name,
            records_loaded=total_loaded,
            records_skipped=total_skipped,
        )

        if self._metrics_emitter is not None:
            self._metrics_emitter.emit_records_extracted(
                source_id=table_name,
                entity_id=table_name,
                environment=self._environment,
                count=total_loaded,
                stage="serving_store_load",
            )
            self._metrics_emitter.emit_records_skipped(
                source_id=table_name,
                entity_id=table_name,
                environment=self._environment,
                count=total_skipped,
                stage="serving_store_load",
            )

        if self._governance_s3_bucket and run_id and analytics_s3_bucket and analytics_s3_prefix:
            try:
                lineage_record = build_serving_store_lineage(
                    run_id=run_id,
                    source_id=table_name,
                    entity_id=table_name,
                    analytics_s3_bucket=analytics_s3_bucket,
                    analytics_s3_prefix=analytics_s3_prefix,
                    table_name=f"{container_name}.{table_name}",
                    record_count=total_loaded,
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
            database_name=container_name,
            table_name=table_name,
            records_loaded=total_loaded,
            records_skipped=total_skipped,
            started_at=started_at,
            completed_at=completed_at,
        )

    def _validate_and_resolve_container(
        self, table_name: str, primary_keys: tuple[str, ...], tenant_code: str
    ) -> str:
        """Validate identifiers (OWASP A03) and derive the tenant container name."""
        validate_tenant_code(tenant_code)
        container_name = tenant_code.replace("-", "_")
        if not SAFE_CONTAINER_PATTERN.match(container_name):
            raise ValueError(f"Invalid tenant container name: {container_name!r}")
        if not SAFE_CONTAINER_PATTERN.match(table_name):
            raise ValueError(f"Invalid table name: {table_name!r}")
        for pk in primary_keys:
            if not SAFE_COLUMN_PATTERN.match(pk):
                raise ServingStoreError(f"Unsafe primary key name rejected: {pk!r}")
        return container_name

    def _run_session(
        self,
        record_batches: Iterable[list[dict[str, Any]]],
        table_name: str,
        primary_keys: tuple[str, ...],
        tenant_code: str,
        container_name: str,
        connection_database: str,
    ) -> tuple[int, int]:
        """Open one connection, load every batch through it, and close it."""
        credentials = self._retrieve_credentials()
        try:
            self._ensure_connection_database(credentials, connection_database)
            connection = self._connect(credentials, connection_database)
        except ServingStoreError:
            raise
        except Exception as exc:
            raise ServingStoreError(f"Failed to connect to database: {exc}") from exc

        total_loaded = 0
        total_skipped = 0
        columns: list[str] | None = None
        try:
            self._ensure_tenant_container(connection, container_name)
            self._select_container(connection, container_name)
            self._provision_reader_credential(connection, tenant_code, container_name, credentials)

            any_batch = False
            for batch in record_batches:
                if not batch:
                    continue
                any_batch = True
                if columns is None:
                    columns = list(batch[0].keys())
                    self._ensure_table(
                        connection, container_name, table_name, columns, batch[0], primary_keys
                    )
                loaded, skipped = self._load_one_batch(
                    connection, container_name, table_name, columns, primary_keys, batch
                )
                connection.commit()
                total_loaded += loaded
                total_skipped += skipped
            if not any_batch:
                raise ServingStoreError("Cannot load zero records")
        except ServingStoreError:
            connection.rollback()
            raise
        except Exception as exc:
            connection.rollback()
            raise ServingStoreError(f"Load failed for table {table_name!r}: {exc}") from exc
        finally:
            connection.close()

        return total_loaded, total_skipped

    def _load_one_batch(
        self,
        connection: Any,
        container_name: str,
        table_name: str,
        columns: list[str],
        primary_keys: tuple[str, ...],
        batch: list[dict[str, Any]],
    ) -> tuple[int, int]:
        hashed = [(record, compute_row_hash(record, columns)) for record in batch]
        pk_tuples = [tuple(record[pk] for pk in primary_keys) for record, _ in hashed]
        existing_hashes = self._read_existing_hashes(
            connection, container_name, table_name, primary_keys, pk_tuples
        )
        changed = [
            (record, row_hash)
            for (record, row_hash), pk_tuple in zip(hashed, pk_tuples, strict=True)
            if existing_hashes.get(pk_tuple) != row_hash
        ]
        loaded = self._bulk_upsert(
            connection, container_name, table_name, columns, primary_keys, changed
        )
        return loaded, len(batch) - loaded

    # ── Credentials (shared — Secrets Manager access is identical per engine) ─

    def _retrieve_credentials(self) -> dict[str, str]:
        """Fetch writer DB credentials from Secrets Manager, then inject the endpoint.

        An AWS-managed RDS master secret carries only username/password — no host/port.
        The DB endpoint is infrastructure (not a rotating credential), so it is supplied
        via the config record (`db_host`/`db_port`) and injected here only when the secret
        does not already carry a host (e.g. the Redshift custom secret does).
        """
        try:
            response = self._sm.get_secret_value(SecretId=self._secret_arn)
            creds: dict[str, str] = json.loads(response["SecretString"])
        except Exception as exc:
            raise ServingStoreError("Failed to retrieve database credentials") from exc
        if "host" not in creds and self._db_host:
            creds["host"] = self._db_host
        if "port" not in creds and self._db_port is not None:
            creds["port"] = str(self._db_port)
        return creds

    def _get_or_create_reader_password(
        self,
        secret_name: str,
        username: str,
        container_name: str,
        writer_creds: dict[str, str],
        connection_database: str,
    ) -> str:
        try:
            response = self._sm.get_secret_value(SecretId=secret_name)
            existing: dict[str, str] = json.loads(response["SecretString"])
            return existing["password"]
        except self._sm.exceptions.ResourceNotFoundException:
            password = _secrets.token_urlsafe(24)
            payload = {
                "host": writer_creds["host"],
                "port": str(writer_creds.get("port", self.default_port)),
                "username": username,
                "password": password,
                "database": connection_database,
            }
            self._sm.create_secret(Name=secret_name, SecretString=json.dumps(payload))
            return password

    def _ensure_connection_database(  # noqa: B027 — intentional no-op default, not abstract
        self, credentials: dict[str, str], connection_database: str
    ) -> None:
        """Create the fixed connection database before _connect, if the engine needs it.

        No-op by default: MySQL connects with no default database and creates the tenant
        database itself; Redshift Serverless always has a default database. Postgres/SQL
        Server override this — a freshly provisioned RDS instance has no `edl_serving`
        database, so `CREATE SCHEMA` on connect would otherwise fail (OWASP A05).
        """

    # ── Abstract: genuine per-engine SQL-dialect differences only ────────────

    @abc.abstractmethod
    def _connect(self, credentials: dict[str, str], connection_database: str) -> Any:
        """Open a connection with TLS enforced (OWASP A02)."""

    @abc.abstractmethod
    def _ensure_tenant_container(self, connection: Any, container_name: str) -> None:
        """CREATE DATABASE (MySQL) or CREATE SCHEMA (Postgres/SQL Server) if not exists."""

    @abc.abstractmethod
    def _select_container(self, connection: Any, container_name: str) -> None:
        """Make container_name the implicit default for unqualified statements, if possible."""

    @abc.abstractmethod
    def _provision_reader_credential(
        self, connection: Any, tenant_code: str, container_name: str, writer_creds: dict[str, str]
    ) -> None:
        """Create/refresh a per-tenant read-only login, scoped to container_name only."""

    @abc.abstractmethod
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

    @abc.abstractmethod
    def _read_existing_hashes(
        self,
        connection: Any,
        container_name: str,
        table_name: str,
        primary_keys: tuple[str, ...],
        pk_tuples: list[tuple[Any, ...]],
    ) -> dict[tuple[Any, ...], str]:
        """Fetch {primary_key_tuple: _row_hash} for the given keys."""

    @abc.abstractmethod
    def _bulk_upsert(
        self,
        connection: Any,
        container_name: str,
        table_name: str,
        columns: list[str],
        primary_keys: tuple[str, ...],
        changed: list[tuple[dict[str, Any], str]],
    ) -> int:
        """Upsert only new-or-changed (record, row_hash) pairs; return rows written."""
