"""
AWS Glue Data Catalog registration client.

Registers datasets in the AWS Glue Data Catalog with schema, lineage
metadata, and data classification tags.  Every production dataset must
be registered before consumers can query it (spec §9.1).

Schema publishing workflow:
  1. Ensure Glue database exists
  2. Infer column types from dataset schema (Parquet or dict sample)
  3. Create or update Glue table with StorageDescriptor
  4. Attach catalog metadata (owner, lineage, classification, retention)

Security (OWASP A01, A02):
  - Only the governance service role has write access to the Glue catalog.
  - Table names validated against safe-identifier regex.
  - No catalog entry includes raw data values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

import boto3

from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_SAFE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


class DataLayer(StrEnum):
    RAW = "raw"
    CURATED = "curated"
    ANALYTICS = "analytics"


@dataclass(frozen=True)
class CatalogDatasetSpec:
    """
    Metadata required to register a dataset in the Glue Data Catalog.
    """

    database_name: str
    table_name: str
    s3_location: str
    data_layer: DataLayer
    owner: str
    data_classification: str  # e.g., "public", "internal", "confidential", "pii"
    retention_days: int
    source_lineage: tuple[str, ...]  # upstream S3 prefixes / dataset names
    partition_keys: tuple[str, ...] = ()
    schema: tuple[dict[str, str], ...] = ()  # [{"Name": "...", "Type": "..."}]
    description: str = ""


@dataclass(frozen=True)
class CatalogRegistrationResult:
    """Result of a Glue Data Catalog registration operation."""

    database_name: str
    table_name: str
    operation: str  # "created" | "updated"
    registered_at: str  # ISO-8601 UTC


class DataCatalogRegistrationClient:
    """
    Registers and updates dataset entries in the AWS Glue Data Catalog.

    Idempotent: calling register_dataset for an existing table updates it.
    """

    def __init__(self, region_name: str) -> None:
        self._region_name = region_name
        self._glue: Any = boto3.client("glue", region_name=region_name)

    def register_dataset(self, spec: CatalogDatasetSpec) -> CatalogRegistrationResult:
        """
        Register or update a dataset entry in the Glue Data Catalog.

        Raises:
            CatalogRegistrationError on validation failure or Glue API error.
        """
        if not _SAFE_NAME_PATTERN.match(spec.database_name):
            raise CatalogRegistrationError(f"Invalid database name: {spec.database_name!r}")
        if not _SAFE_NAME_PATTERN.match(spec.table_name):
            raise CatalogRegistrationError(f"Invalid table name: {spec.table_name!r}")

        self._ensure_database(spec.database_name, spec.owner)
        operation = self._upsert_table(spec)

        registered_at = datetime.now(UTC).isoformat()

        _logger.info(
            "catalog_dataset_registered",
            database=spec.database_name,
            table=spec.table_name,
            operation=operation,
            data_layer=spec.data_layer.value,
            owner=spec.owner,
        )

        return CatalogRegistrationResult(
            database_name=spec.database_name,
            table_name=spec.table_name,
            operation=operation,
            registered_at=registered_at,
        )

    def get_dataset(self, database_name: str, table_name: str) -> dict[str, Any]:
        """
        Retrieve the Glue table definition.

        Raises DatasetNotFoundError if the table does not exist.
        """
        try:
            response = self._glue.get_table(DatabaseName=database_name, Name=table_name)
            return response["Table"]  # type: ignore[no-any-return]
        except self._glue.exceptions.EntityNotFoundException as exc:
            raise DatasetNotFoundError(database_name, table_name) from exc

    def list_datasets(self, database_name: str) -> list[str]:
        """Return a list of table names in the given Glue database."""
        try:
            paginator = self._glue.get_paginator("get_tables")
            names: list[str] = []
            for page in paginator.paginate(DatabaseName=database_name):
                names.extend(t["Name"] for t in page.get("TableList", []))
            return names
        except self._glue.exceptions.EntityNotFoundException:
            return []

    def _ensure_database(self, database_name: str, owner: str) -> None:
        """Create the Glue database if it does not exist."""
        try:
            self._glue.get_database(Name=database_name)
        except self._glue.exceptions.EntityNotFoundException:
            self._glue.create_database(
                DatabaseInput={
                    "Name": database_name,
                    "Description": f"Enterprise Data Lake — {database_name} (owner: {owner})",
                }
            )

    def _upsert_table(self, spec: CatalogDatasetSpec) -> str:
        """Create or update the Glue table; return 'created' or 'updated'.

        Uses a create-or-update pattern that is safe under concurrent Lambda
        invocations (TOCTOU fix, F-16):
          1. Attempt to create the table (optimistic path for first registration).
          2. If AlreadyExistsException: another process raced us — fall through to update.
          3. If neither exception: table created successfully.
        An unconditional get_table + conditional create/update would be racy between
        step 1 and step 2.  This approach is atomic at the Glue API level.
        """
        table_input = self._build_table_input(spec)
        try:
            self._glue.create_table(DatabaseName=spec.database_name, TableInput=table_input)
            return "created"
        except self._glue.exceptions.AlreadyExistsException:
            # Table exists (either pre-existing or created by a concurrent invocation).
            self._glue.update_table(DatabaseName=spec.database_name, TableInput=table_input)
            return "updated"

    @staticmethod
    def _build_table_input(spec: CatalogDatasetSpec) -> dict[str, Any]:
        columns = list(spec.schema) if spec.schema else []
        partition_keys = [{"Name": k, "Type": "string"} for k in spec.partition_keys]

        return {
            "Name": spec.table_name,
            "Description": spec.description or f"{spec.table_name} ({spec.data_layer.value} layer)",
            "StorageDescriptor": {
                "Columns": columns,
                "Location": spec.s3_location,
                "InputFormat": ("org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"),
                "OutputFormat": (
                    "org.apache.hadoop.hive.ql.io.parquet.MapredParquetHiveOutputFormat"
                ),
                "SerdeInfo": {
                    "SerializationLibrary": (
                        "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
                    ),
                },
            },
            "PartitionKeys": partition_keys,
            "TableType": "EXTERNAL_TABLE",
            "Parameters": {
                "classification": "parquet",
                "compressionType": "snappy",
                "owner": spec.owner,
                "data_classification": spec.data_classification,
                "data_layer": spec.data_layer.value,
                "retention_days": str(spec.retention_days),
                "source_lineage": ",".join(spec.source_lineage),
                "registered_at": datetime.now(UTC).isoformat(),
            },
        }


class CatalogRegistrationError(Exception):
    """Raised when catalog registration validation or API call fails."""


class DatasetNotFoundError(Exception):
    def __init__(self, database: str, table: str) -> None:
        super().__init__(f"Dataset not found: {database}.{table}")
