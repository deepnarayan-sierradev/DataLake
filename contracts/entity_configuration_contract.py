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

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

from contracts.identifier_policy import (
    PROHIBITED_IDENTIFIERS as _PROHIBITED_IDENTIFIERS,
)
from contracts.identifier_policy import (
    STABLE_ID_PATTERN as _STABLE_ID_PATTERN,
)


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

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    active: bool = Field(
        default=True,
        description="Whether this entity is active for scheduled extraction.",
    )

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("source_id", "entity_id", mode="before")
    @classmethod
    def enforce_stable_identifier_format(cls, value: str) -> str:
        """
        Source and entity IDs must match the stable identifier format.
        Same pattern enforced by StructuredLogEvent — single source of truth
        for identifier constraints across the platform.
        Valid: 'salesforce', 'salesforce-account', 'mysql-rds', 'netsuite-customer'
        Invalid: 'Salesforce', 'salesforce_account', 'phase1', 'helper'
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
        return self
