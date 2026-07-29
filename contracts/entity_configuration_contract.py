"""
Entity extraction configuration contract for the Enterprise Data Lake platform.

All source entity behaviour is driven by this configuration — no code changes
are required to onboard new entities or adjust extraction parameters.

Configuration records are versioned and stored in the Configuration Repository
(DynamoDB or S3-backed). The runtime loads and validates them at pipeline start.

Enforcement:
  - Schema validated by Pydantic before the connector runtime is invoked.
  - Unknown fields are rejected (model_config extra='forbid').
  - Conflicting field combinations raise validation errors (e.g. INCLUDE_ONLY
    with empty include_fields).
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, Field, field_validator, model_validator

from contracts.identifier_policy import (
    PROHIBITED_IDENTIFIERS as _PROHIBITED_IDENTIFIERS,
)
from contracts.identifier_policy import (
    STABLE_ID_PATTERN as _STABLE_ID_PATTERN,
)
from contracts.identifier_policy import TENANT_CODE_PATTERN as _TENANT_CODE_PATTERN

# Field name pattern: letters, digits, underscore only — no dots.
# pk_field and soft_delete_field must be flat canonical field names because
# record.get(field) performs top-level dict lookup only.  Dotted paths like
# 'auditInfo.id' would silently return None and break merge logic.
# Sourced from server-side entity config only — never from event input (OWASP A03).
_FIELD_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,127}$")


class LoadType(StrEnum):
    """Extraction load strategy."""

    FULL = "full"
    INCREMENTAL = "incremental"


class FieldMode(StrEnum):
    """Controls which fields are included in the extraction query."""

    ALL = "all"  # All queryable fields discovered from metadata
    STANDARD = "standard"  # Standard (non-custom) fields only
    CUSTOM = "custom"  # Custom fields only
    INCLUDE_ONLY = "includeOnly"  # Exactly the fields listed in include_fields


class OutputFormat(StrEnum):
    """Raw output file format written to S3."""

    PARQUET = "parquet"
    JSON_LINES = "jsonl"


class EntityExtractionConfig(BaseModel):
    """
    Versioned configuration record for a single source entity extraction.

    This is the single source of truth for connector runtime behaviour.
    Changing extraction behaviour requires updating this record — not code.

    Field naming: all keys use explicit names (no abbreviations or ambiguous
    labels). This matches the naming standard across the platform.
    """

    model_config = {"frozen": True, "extra": "forbid"}

    # ── Identity ──────────────────────────────────────────────────────────────
    source_id: str = Field(
        ...,
        description="Stable source system identifier (e.g. 'salesforce', 'netsuite').",
    )
    entity_id: str = Field(
        ...,
        description="Stable entity identifier (e.g. 'salesforce-account', 'netsuite-customer').",
    )
    config_version: str = Field(
        ...,
        description="Semantic version of this configuration record (e.g. '1.0.0').",
    )

    # ── Connection identity (DL-SCOPE-04) ─────────────────────────────────────
    connection_id: str | None = Field(
        default=None,
        description=(
            "Identity of the connector instance this entity is extracted through. "
            "None means the source's default connection (connection_id == source_id), "
            "which keeps every key byte-identical to the pre-DL-12 form."
        ),
    )

    # ── Configuration schema compatibility (DL-CFG-14) ────────────────────────
    config_schema_version: int = Field(
        default=1,
        ge=1,
        description="Schema generation of this config artefact; runtime declares its range.",
    )

    # ── Multi-tenancy (§1.1) ──────────────────────────────────────────────────
    tenant_code: str = Field(
        default="demo",
        description=(
            "Tenant identifier slug (e.g. 'demo', 'acme-corp'). "
            "Used as the root S3 path prefix for data isolation: "
            "{tenant_code}/raw/{source_id}/{entity_id}/... "
            "Validated against TENANT_CODE_PATTERN at load time."
        ),
    )

    # ── Extraction behaviour ──────────────────────────────────────────────────
    load_type: LoadType = Field(
        default=LoadType.INCREMENTAL,
        description="Full or incremental extraction strategy.",
    )
    watermark_field: str | None = Field(
        default=None,
        description="Source field used as the incremental watermark (e.g. 'SystemModstamp').",
    )
    extraction_window_days: int = Field(
        default=1,
        ge=1,
        le=365,
        description="Lookback window in days for incremental extraction.",
    )
    watermark_overlap_hours: int = Field(
        default=2,
        ge=0,
        le=48,
        description=(
            "Additional overlap hours subtracted from the lower watermark bound "
            "to capture late-arriving source updates."
        ),
    )

    # ── Field selection ───────────────────────────────────────────────────────
    field_mode: FieldMode = Field(
        default=FieldMode.ALL,
        description="Controls which fields are included in the extraction query.",
    )
    include_fields: list[str] = Field(
        default_factory=list,
        description="Explicit field list when field_mode is INCLUDE_ONLY.",
    )
    exclude_fields: list[str] = Field(
        default_factory=list,
        description="Fields to exclude from extraction regardless of field_mode.",
    )

    # ── Storage ───────────────────────────────────────────────────────────────
    target_raw_s3_prefix: str = Field(
        ...,
        description=(
            "S3 prefix for raw output (e.g. 's3://raw/salesforce/account/'). "
            "Run-specific partition appended by the runtime."
        ),
    )
    schema_snapshot_s3_prefix: str = Field(
        ...,
        description="S3 prefix for schema snapshots (e.g. 's3://schema-snapshots/salesforce/account/').",
    )
    output_format: OutputFormat = Field(
        default=OutputFormat.PARQUET,
        description="Output file format for raw layer writes.",
    )

    # ── Connector ─────────────────────────────────────────────────────────────
    connector_params: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Source-specific parameters passed to the extraction connector. "
            "MySQL: {'table_name': 'Contracts'}. "
            "Salesforce: {'object_name': 'Account'}."
        ),
    )

    # ── Schedule ──────────────────────────────────────────────────────────────
    schedule_cron: str | None = Field(
        default=None,
        description=(
            "EventBridge Scheduler cron expression for automatic extraction. "
            "None means the entity is triggered manually only. "
            "Example: 'cron(0 2 * * ? *)' = daily at 02:00 UTC."
        ),
    )
    schedule_enabled: bool = Field(
        default=True,
        description=(
            "Whether the EventBridge schedule should be active. Ignored when schedule_cron is None."
        ),
    )
    schedule_timezone: str = Field(
        default="UTC",
        description=(
            "IANA timezone for the cron schedule (e.g. 'America/New_York'). Defaults to UTC."
        ),
    )

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    active: bool = Field(
        default=True,
        description="Whether this entity is active for scheduled extraction.",
    )

    # ── Incremental merge ─────────────────────────────────────────────────────
    primary_key_field: str | None = Field(
        default=None,
        description=(
            "Canonical field name used as the primary key for curated-layer MERGE. "
            "Required for incremental entities to maintain a full current-state "
            "snapshot in the curated layer (SCD Type 1). "
            "Example: 'Id' for Salesforce entities, 'id' for MySQL tables. "
            "None means append-only behaviour (no merge) — correct for full-load entities."
        ),
    )
    soft_delete_field: str | None = Field(
        default=None,
        description=(
            "Canonical field name whose truthy value marks a record as soft-deleted. "
            "When set, records where this field is truthy are removed from the merged "
            "curated snapshot rather than upserted. "
            "Example: 'is_delete' for MySQL tables that use a deletion flag. "
            "None means deletions are not tracked via a flag (hard-delete or N/A)."
        ),
    )

    # ── Lambda execution tuning (§3.5, §3.7) ─────────────────────────────────
    max_records_per_lambda_run: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Hard cap on records extracted per Lambda invocation. When the extraction "
            "stage reaches this count with sufficient time remaining, it commits a "
            "partial watermark and raises LambdaTimeoutWarning so Step Functions "
            "can re-trigger from the checkpoint. "
            "None means no cap — the full extraction runs in a single invocation. "
            "Recommended for entities expected to exceed 500K records."
        ),
    )
    lambda_memory_mb: int | None = Field(
        default=None,
        ge=1024,
        le=10240,
        description=(
            "Lambda memory override in MB for this entity. When set, the Step Functions "
            "Parameters block passes this value so the appropriate Lambda alias is invoked. "
            "Use 2048 for entities requiring DuckDB in-process merge, 4096+ for very large "
            "entities. None uses the Lambda function default (1024 MB for extraction, "
            "2048 MB for transformation)."
        ),
    )

    # ── Sync, pagination and rate limiting (DL-CONN-11, 13, 15) ───────────────
    sync_strategy: str = Field(
        default="watermark_polling",
        description=(
            "Registered SyncStrategy name: watermark_polling | webhook_ingest | log_based_cdc."
        ),
    )
    rate_limit_policy: str | None = Field(
        default=None,
        description="Registered RateLimitPolicy name; None uses the source's declared default.",
    )
    pagination_strategy: str | None = Field(
        default=None,
        description="Registered PaginationStrategy name; None uses the adapter's declared default.",
    )
    writeback_enabled: bool = Field(
        default=False,
        description=(
            "Opt-in bi-directional write path (DL-CONN-02). Deliberately a separate flag from "
            "`active` so enabling reads can never enable source mutation."
        ),
    )

    # ── Quality, brand and retention (DL-DQ-05, DL-DQ-09, DL-PORT-03) ─────────
    quality_policy_id: str | None = Field(
        default=None,
        description="Attached quality policy; required before production promotion (DL-DQ-05).",
    )
    quality_gate_blocks_on_error: bool = Field(
        default=True,
        description="ERROR-severity violations block the analytics publish for this entity.",
    )
    brand_code: str | None = Field(
        default=None,
        max_length=64,
        description="First-class brand dimension, distinct from tenant_code (DL-DQ-09).",
    )
    retention_days: int | None = Field(
        default=None,
        ge=1,
        description="Raw-layer retention for this entity; validated against reprocessing windows.",
    )
    minimum_reprocessing_window_days: int | None = Field(
        default=None,
        ge=1,
        description="Declared reprocess-eligible window; retention must be at least this long.",
    )

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("connection_id", mode="before")
    @classmethod
    def validate_connection_id(cls, value: str | None) -> str | None:
        """connection_id shares the stable-id charset with source_id."""
        if value is None:
            return None
        if not _STABLE_ID_PATTERN.match(value):
            raise ValueError(
                f"connection_id '{value}' does not conform to the stable ID format "
                "(lowercase letters, digits, hyphens; 2-64 chars; must start with a letter)."
            )
        return value

    @field_validator("source_id", "entity_id", mode="before")
    @classmethod
    def enforce_stable_identifier_format(cls, value: str) -> str:
        """
        Source and entity IDs must match the stable identifier format.
        """
        if not _STABLE_ID_PATTERN.match(value):
            raise ValueError(
                f"Identifier '{value}' does not conform to the stable ID format. "
                "Use lowercase letters, digits, and hyphens only (2-64 chars, "
                "must start with a letter). "
                "Examples: 'salesforce', 'salesforce-account', 'mysql-rds'."
            )
        if value in _PROHIBITED_IDENTIFIERS:
            raise ValueError(
                f"Identifier '{value}' is a prohibited generic name. "
                "Use a specific, domain-meaningful identifier instead."
            )
        return value

    @field_validator("tenant_code", mode="before")
    @classmethod
    def validate_tenant_code(cls, value: str) -> str:
        """Tenant code must match TENANT_CODE_PATTERN (§1.1)."""
        if not _TENANT_CODE_PATTERN.match(value):
            raise ValueError(
                f"tenant_code '{value}' does not conform to the tenant code format. "
                "Use lowercase letters, digits, and hyphens only (2-48 chars, "
                "must start with a letter). Examples: 'demo', 'acme-corp'."
            )
        return value

    @field_validator("target_raw_s3_prefix", "schema_snapshot_s3_prefix", mode="before")
    @classmethod
    def validate_s3_prefix(cls, value: str) -> str:
        """S3 path fields must start with the s3:// scheme to prevent misconfiguration."""
        if not value.startswith("s3://"):
            raise ValueError(
                f"S3 prefix '{value}' must start with 's3://'. Example: 's3://my-bucket/prefix/'."
            )
        return value

    @model_validator(mode="after")
    def validate_configuration_consistency(self) -> EntityExtractionConfig:
        if self.load_type == LoadType.INCREMENTAL and not self.watermark_field:
            raise ValueError(
                f"Entity '{self.entity_id}': watermark_field is required when "
                "load_type is 'incremental'. Provide the source field name "
                "(e.g. 'SystemModstamp', 'LastModifiedDate')."
            )
        if self.field_mode == FieldMode.INCLUDE_ONLY and not self.include_fields:
            raise ValueError(
                f"Entity '{self.entity_id}': include_fields must not be empty when "
                "field_mode is 'includeOnly'."
            )
        overlap = set(self.include_fields) & set(self.exclude_fields)
        if overlap:
            raise ValueError(
                f"Entity '{self.entity_id}': fields appear in both include_fields "
                f"and exclude_fields: {sorted(overlap)}. Remove the conflict."
            )
        if self.soft_delete_field is not None and self.primary_key_field is None:
            raise ValueError(
                f"Entity '{self.entity_id}': soft_delete_field requires primary_key_field "
                "to be set. A primary key is needed to identify and remove deleted records "
                "from the merged curated state."
            )
        # DL-CFG-12: a retention policy shorter than a declared reprocessing window is a
        # configuration error caught at publish, not discovered when the data is gone.
        if (
            self.retention_days is not None
            and self.minimum_reprocessing_window_days is not None
            and self.retention_days < self.minimum_reprocessing_window_days
        ):
            raise ValueError(
                f"Entity '{self.entity_id}': retention_days={self.retention_days} is shorter "
                f"than minimum_reprocessing_window_days="
                f"{self.minimum_reprocessing_window_days}. Reprocessing would find the input "
                "data already expired."
            )
        return self

    @property
    def effective_connection_id(self) -> str:
        """Identity component for keys: the explicit connection, else the source's default."""
        return self.connection_id or self.source_id

    @field_validator("primary_key_field", "soft_delete_field", mode="before")
    @classmethod
    def validate_field_name(cls, value: str | None) -> str | None:
        """
        Validate that primary_key_field and soft_delete_field are safe field names.

        Allows letters, digits, underscores, and dots (for nested paths).
        Rejects empty strings, path traversal characters, and excessively long names.
        Values originate from server-side entity config only (OWASP A03).
        """
        if value is None:
            return None
        if not _FIELD_NAME_PATTERN.match(value):
            raise ValueError(
                f"Field name '{value}' is invalid. Must start with a letter or underscore "
                "and contain only letters, digits, or underscores (max 128 chars). "
                "Must be a flat canonical field name — dotted paths are not supported "
                "(record.get() performs top-level lookup only). "
                "Examples: 'Id', 'SystemModstamp', 'is_delete'."
            )
        return value
