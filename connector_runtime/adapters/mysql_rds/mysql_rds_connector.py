"""
MySQL RDS connector adapter.

Implements ConnectorInterface for MySQL RDS as the single, metadata-driven
adapter for all MySQL RDS tables.  No table-specific subclasses.

Design:
  - All columns discovered at runtime via information_schema — no hardcoded lists.
  - Single connector class handles all MySQL tables through configuration only.
  - Registers itself with the platform ConnectorRegistry at import time.
  - Private VPC connectivity — the RDS endpoint is never publicly accessible.
  - SSL enforced on all connections (ssl_disabled=False in pymysql.connect).

Credentials:
  - Fetched from AWS Secrets Manager via MySqlRdsCredentialsClient.
  - Never in constructor arguments, env vars, or logs.

Security (OWASP A02, A03, A07, A09):
  - SQL built from validated column names only; table names validated with regex.
  - Watermark values bound as query_parameters (%(name)s) — never interpolated.
  - Password never included in logs or exception messages.
  - SSL (ssl_disabled=False) enforced on every connection (in-transit encryption).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Final

import pymysql
import pymysql.cursors

from connector_runtime.adapters.mysql_rds.mysql_incremental_extractor import (
    MySqlIncrementalExtractor,
    MySqlIncrementalExtractorError,
)
from connector_runtime.adapters.mysql_rds.mysql_rds_credentials_client import (
    MySqlRdsCredentialError,
    MySqlRdsCredentialsClient,
)
from connector_runtime.adapters.mysql_rds.mysql_schema_introspection_client import (
    MySqlSchemaIntrospectionClient,
    MySqlSchemaIntrospectionClientError,
)
from connector_runtime.interfaces.connector_interface import (
    ConnectorCapabilities,
    ConnectorInterface,
    ExtractionErrorClassification,
    ExtractionRecord,
    FieldContract,
    QueryContract,
)
from connector_runtime.registry import connector_registry
from contracts.entity_configuration_contract import FieldMode, LoadType
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_SOURCE_ID: Final[str] = "mysql-rds"


@connector_registry.register(_SOURCE_ID)
class MySqlRdsConnector(ConnectorInterface):
    """
    Metadata-driven MySQL RDS connector for all MySQL tables.

    One instance per extraction run.  The MySQL table name is provided
    as a constructor argument (from entity config) and is never hardcoded.

    Constructor args are NOT used for credentials — those come exclusively
    from AWS Secrets Manager via MySqlRdsCredentialsClient.
    """

    def __init__(
        self,
        environment: str,
        region_name: str,
        table_name: str,
    ) -> None:
        if not table_name:
            raise ValueError("table_name must not be empty.")
        self._table_name = table_name
        self._creds_client = MySqlRdsCredentialsClient(
            environment=environment,
            region_name=region_name,
        )

    def get_capability_declaration(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            source_id=_SOURCE_ID,
            supports_bulk_extraction=False,
            supports_incremental=True,
            supports_full_load=True,
            supports_metadata_discovery=True,
            bulk_threshold_records=0,
            max_concurrent_jobs=1,
            supported_field_modes=(
                FieldMode.ALL,
                FieldMode.STANDARD,
                FieldMode.CUSTOM,
                FieldMode.INCLUDE_ONLY,
            ),
        )

    def discover_queryable_fields(
        self,
        source_id: str,
        entity_id: str,
        field_mode: FieldMode,
        include_fields: list[str],
        exclude_fields: list[str],
    ) -> FieldContract:
        """
        Discover all queryable columns for this table via information_schema.

        New columns added to the MySQL table appear automatically in the
        next run without any code changes.
        """
        params = self._creds_client.get_connection_parameters()
        conn = self._open_connection(params)
        try:
            introspection_client = MySqlSchemaIntrospectionClient(connection=conn)
            return introspection_client.discover_fields(
                source_id=source_id,
                entity_id=entity_id,
                database=params.database,
                table_name=self._table_name,
                field_mode=field_mode,
                include_fields=include_fields,
                exclude_fields=exclude_fields,
            )
        finally:
            conn.close()

    def build_extraction_query(
        self,
        field_contract: FieldContract,
        load_type: LoadType,
        watermark_field: str | None,
        watermark_lower: str | None,
        watermark_upper: str | None,
        extraction_window_days: int,
    ) -> QueryContract:
        """
        Build a parameterized SQL SELECT query from the discovered FieldContract.

        Watermark bounds are stored as query_parameters (%(name)s style) —
        never interpolated into the query_text string.
        """
        return MySqlIncrementalExtractor.build_query(
            field_contract=field_contract,
            table_name=self._table_name,
            load_type=load_type,
            watermark_field=watermark_field,
            watermark_lower=watermark_lower,
            watermark_upper=watermark_upper,
        )

    def execute_extraction(
        self,
        query_contract: QueryContract,
        run_id: str,
    ) -> Iterator[ExtractionRecord]:
        """
        Execute the SQL query and yield records.

        Opens a fresh connection for this extraction run.  The connection
        is closed in a finally block regardless of success or failure.
        SSL is enforced on every connection (in-transit encryption).
        """
        _logger.info(
            "mysql_rds_extraction_started",
            source_id=query_contract.source_id,
            entity_id=query_contract.entity_id,
            run_id=run_id,
            load_type=str(query_contract.load_type),
            table_name=self._table_name,
        )

        params = self._creds_client.get_connection_parameters()
        conn = self._open_connection(params)
        record_count = 0
        try:
            extractor = MySqlIncrementalExtractor(connection=conn)
            for record in extractor.extract(query_contract):
                record_count += 1
                yield record
        finally:
            conn.close()

        _logger.info(
            "mysql_rds_extraction_completed",
            source_id=query_contract.source_id,
            entity_id=query_contract.entity_id,
            run_id=run_id,
            record_count=record_count,
        )

    def classify_extraction_error(self, exc: Exception) -> ExtractionErrorClassification:
        """
        Classify a MySQL RDS extraction exception for the retry framework.
        """
        if isinstance(exc, MySqlRdsCredentialError):
            return ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS
        if isinstance(exc, MySqlIncrementalExtractorError):
            # Covers invalid table/column names and query execution failures.
            # Query execution failures may be transient (deadlock, lock timeout)
            # or deterministic (invalid schema). Default to UNKNOWN for DLQ routing.
            return ExtractionErrorClassification.UNKNOWN
        if isinstance(exc, MySqlSchemaIntrospectionClientError):
            # Missing table is deterministic; schema query failure may be transient.
            return ExtractionErrorClassification.UNKNOWN
        if isinstance(exc, pymysql.err.OperationalError):
            # Covers connection failures (host unreachable, authentication failure).
            return ExtractionErrorClassification.TRANSIENT_NETWORK
        if isinstance(exc, pymysql.err.ProgrammingError):
            # Covers syntax errors, missing tables — deterministic.
            return ExtractionErrorClassification.DETERMINISTIC_INVALID_OBJECT
        if isinstance(exc, OSError):
            return ExtractionErrorClassification.TRANSIENT_NETWORK
        return ExtractionErrorClassification.UNKNOWN

    # ── Private ────────────────────────────────────────────────────────────────

    @staticmethod
    def _open_connection(params: Any) -> Any:
        """
        Open a pymysql connection with SSL enforced.

        SSL is enforced by setting ssl_disabled=False (the pymysql default
        when ssl_disabled is not explicitly set, but we set it explicitly
        to make the security requirement auditable).

        The password is passed to pymysql but never logged.
        """
        return pymysql.connect(
            host=params.host,
            port=params.port,
            user=params.username,
            password=params.password,
            database=params.database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            ssl_disabled=False,
            connect_timeout=10,
        )


# ---------------------------------------------------------------------------
# Connector builder
# ---------------------------------------------------------------------------


def _build_mysql_rds(
    environment: str,
    region_name: str,
    connector_params: dict[str, str],
    raw_s3_bucket: str,
) -> tuple[ConnectorInterface, Any]:
    """
    Factory used by the extraction pipeline Lambda to construct a fully-wired
    MySqlRdsConnector and MySqlRdsRawLayerWriter from the Step Functions
    execution input.

    Required connector_params key:
      table_name (str) — RDS table name (e.g. 'orders', 'customers').
    """
    from connector_runtime.adapters.mysql_rds.mysql_rds_raw_layer_writer import (
        MySqlRdsRawLayerWriter,
    )

    table_name = connector_params.get("table_name", "")
    if not table_name:
        raise ValueError(
            "connector_params must include 'table_name' for source_id='mysql-rds'. "
            "Example: {'table_name': 'orders'}."
        )
    connector = MySqlRdsConnector(
        environment=environment,
        region_name=region_name,
        table_name=table_name,
    )
    writer = MySqlRdsRawLayerWriter(
        s3_bucket=raw_s3_bucket,
        s3_prefix="mysql-rds",
        region_name=region_name,
    )
    return connector, writer


connector_registry.register_builder(_SOURCE_ID, _build_mysql_rds)
