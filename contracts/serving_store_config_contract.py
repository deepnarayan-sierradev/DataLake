"""
Serving store load configuration contract for the Enterprise Data Lake platform.

Which tenant/entity pairs get loaded into a serving store, targeting which
database engine, is driven entirely by this configuration — no code changes
are required to onboard a new tenant or entity.

Configuration records are stored in the Serving Store Configuration Repository
(DynamoDB or S3-backed, mirroring ConfigurationRepositoryClient).
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, Field, field_validator

from contracts.identifier_policy import STABLE_ID_PATTERN as _STABLE_ID_PATTERN
from contracts.identifier_policy import TENANT_CODE_PATTERN as _TENANT_CODE_PATTERN

# Matches connector_runtime/adapters/mysql_rds/mysql_rds_params.py's table_name
# pattern — same boundary-validation convention, config-contract layer.
_SQL_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class ServingStoreEngine(StrEnum):
    """Target relational database engine for a serving store load."""

    MYSQL_RDS = "mysql_rds"
    POSTGRESQL = "postgresql"
    SQLSERVER = "sqlserver"
    AZURE_SQL = "azure_sql"  # always BYO-DB — cannot be provisioned on AWS


class ServingStoreLoadConfig(BaseModel):
    """Versioned configuration record for one tenant/entity serving store load."""

    model_config = {"frozen": True, "extra": "forbid"}

    tenant_code: str = Field(..., description="Tenant identifier slug (e.g. 'acme-corp').")
    entity_id: str = Field(
        ..., description="Stable entity identifier (e.g. 'salesforce-account')."
    )
    target_engine: ServingStoreEngine = Field(
        default=ServingStoreEngine.MYSQL_RDS, description="Target serving store engine."
    )
    table_name: str = Field(
        ..., description="Unscoped table name; the loader applies tenant scoping."
    )
    primary_keys: tuple[str, ...] = Field(
        ..., min_length=1, description="Column(s) forming the upsert/primary key."
    )
    secret_arn: str = Field(..., description="Secrets Manager ARN for the writer credential.")
    region_name: str = Field(..., description="AWS region of the target database.")
    connection_database: str | None = Field(
        default=None,
        description=(
            "Fixed top-level database to connect to before creating/using the tenant "
            "schema (PostgreSQL/SQL Server/Azure SQL only — ignored for MySQL, where "
            "tenant_code is itself the database). None uses the engine adapter's default."
        ),
    )
    enabled: bool = Field(default=True, description="Whether this entity loads on each run.")

    @field_validator("tenant_code", mode="before")
    @classmethod
    def validate_tenant_code(cls, value: str) -> str:
        if not _TENANT_CODE_PATTERN.match(value):
            raise ValueError(f"tenant_code {value!r} does not conform to the tenant code format.")
        return value

    @field_validator("entity_id", mode="before")
    @classmethod
    def validate_entity_id(cls, value: str) -> str:
        if not _STABLE_ID_PATTERN.match(value):
            raise ValueError(f"entity_id {value!r} does not conform to the stable ID format.")
        return value

    @field_validator("table_name", mode="before")
    @classmethod
    def validate_table_name(cls, value: str) -> str:
        if not _SQL_IDENTIFIER_PATTERN.match(value):
            raise ValueError(f"table_name {value!r} is not a safe SQL identifier.")
        return value

    @field_validator("primary_keys", mode="before")
    @classmethod
    def validate_primary_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for column in value:
            if not _SQL_IDENTIFIER_PATTERN.match(column):
                raise ValueError(f"primary_keys entry {column!r} is not a safe SQL identifier.")
        return value

    @field_validator("connection_database", mode="before")
    @classmethod
    def validate_connection_database(cls, value: str | None) -> str | None:
        if value is not None and not _SQL_IDENTIFIER_PATTERN.match(value):
            raise ValueError(f"connection_database {value!r} is not a safe SQL identifier.")
        return value
