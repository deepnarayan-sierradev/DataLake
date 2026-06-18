"""
Pipeline stage boundary contract for the Enterprise Data Lake platform.

Every pipeline stage emits a PipelineStageContract on completion.
The orchestration layer consumes these contracts to determine next-stage
routing, handle failures, and persist the immutable audit trail.

This contract is the canonical record for:
  - Run lineage (what ran, when, with what schema version)
  - Watermark management (extraction window bounds)
  - Drift classification (non-breaking / potentially-breaking / breaking)
  - Failure routing (error_code drives retry vs fail-fast decisions)
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

# PipelineStage and RunStatus are the canonical enums; import from observability_contract
# to ensure a single definition — never duplicate them here.
from contracts.observability_contract import (
    PipelineStage,
    RunStatus,
    scrub_sensitive_values,
)


class DriftClassification(StrEnum):
    """Schema drift severity classification."""

    NO_DRIFT = "no_drift"
    NON_BREAKING = "non_breaking"  # e.g. new nullable field added
    POTENTIALLY_BREAKING = "potentially_breaking"  # e.g. precision/scale/length change
    BREAKING = "breaking"  # e.g. field removed, type changed


class PipelineStageContract(BaseModel):
    """
    Canonical contract emitted at every pipeline stage boundary.

    Used by Step Functions orchestration to:
      - Determine next-stage routing
      - Gate watermark advancement (success only)
      - Route breaking drift alerts
      - Populate the immutable run audit log in DynamoDB

    Immutability: once emitted by a stage, this record is never modified.
    Corrections require a new run with a new run_id.
    """

    model_config = {"frozen": True}

    # ── Identity ──────────────────────────────────────────────────────────────
    run_id: str = Field(..., description="Immutable run identifier.")
    source_id: str = Field(..., description="Stable source system identifier.")
    entity_id: str = Field(..., description="Stable entity identifier.")
    stage: PipelineStage = Field(..., description="Pipeline stage name.")
    status: RunStatus = Field(..., description="Stage status.")
    environment: str = Field(..., description="Deployment environment (dev, staging, prod).")

    # ── Extraction window ─────────────────────────────────────────────────────
    extraction_window_start: datetime | None = Field(
        default=None,
        description="Lower bound of extraction window (inclusive). UTC.",
    )
    extraction_window_end: datetime | None = Field(
        default=None,
        description="Upper bound of extraction window (exclusive). UTC.",
    )

    # ── Schema ────────────────────────────────────────────────────────────────
    schema_version: str | None = Field(
        default=None,
        description="Schema snapshot version applied during this run.",
    )
    drift_classification: DriftClassification | None = Field(
        default=None,
        description="Schema drift severity detected during this run.",
    )

    # ── Output locations ──────────────────────────────────────────────────────
    raw_s3_prefix: str | None = Field(
        default=None,
        description="S3 prefix where raw files were written for this run.",
    )
    schema_snapshot_s3_key: str | None = Field(
        default=None,
        description="S3 key of the schema snapshot written for this run.",
    )

    # ── Counts ────────────────────────────────────────────────────────────────
    record_count: int | None = Field(
        default=None,
        ge=0,
        description="Number of records extracted and written to raw layer.",
    )
    failed_record_count: int | None = Field(
        default=None,
        ge=0,
        description="Number of records that failed extraction or validation.",
    )

    # ── Error ─────────────────────────────────────────────────────────────────
    error_code: str | None = Field(
        default=None,
        description="Machine-readable error code for orchestration routing decisions.",
    )
    error_message: str | None = Field(
        default=None,
        description="Human-readable error summary. Must not contain credentials or PII.",
    )
    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("error_message", "error_code", mode="before")
    @classmethod
    def scrub_error_fields(cls, value: str | None) -> str | None:
        """
        Automatically scrub sensitive patterns from error fields.

        Unlike StructuredLogEvent (which hard-rejects), the stage contract
        scrubs silently — pipeline execution must not halt on a mis-formatted
        error string. Scrubbing ensures no credentials reach the audit log.
        """
        if value is None:
            return value
        return scrub_sensitive_values(value)

    @field_validator("environment", mode="before")
    @classmethod
    def validate_environment(cls, value: str) -> str:
        allowed = {"dev", "staging", "prod"}
        if value not in allowed:
            raise ValueError(f"environment must be one of {sorted(allowed)}, got '{value}'.")
        return value

    # ── Timing ────────────────────────────────────────────────────────────────
    completed_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="UTC timestamp when the stage completed (success or failure).",
    )
    duration_ms: int = Field(
        default=0,
        ge=0,
        description="Stage execution duration in milliseconds.",
    )
