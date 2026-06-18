"""
Observability contract for the Enterprise Data Lake platform.

All platform services MUST emit structured log events conforming to
StructuredLogEvent. Sensitive fields (credentials, tokens, PII) are
never permitted in log output — enforced at the model validation layer.

Security requirements enforced here:
  - Credential and token patterns are detected and rejected at validation time.
  - Source and entity identifiers are validated against the stable ID format.
  - The scrub_sensitive_values() helper provides a last-resort safety net for
    exception messages before they enter the log pipeline.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, Field, field_validator

from contracts.identifier_policy import (
    PROHIBITED_IDENTIFIERS as _PROHIBITED_IDENTIFIERS,
)
from contracts.identifier_policy import (
    SEQUENTIAL_INTEGER_PATTERN as _SEQUENTIAL_INTEGER_PATTERN,
)
from contracts.identifier_policy import (
    STABLE_ID_PATTERN as _STABLE_ID_PATTERN,
)

# ---------------------------------------------------------------------------
# Sensitive pattern registry — add patterns here; never suppress this check
# ---------------------------------------------------------------------------
_SENSITIVE_PATTERNS: Final[list[re.Pattern[str]]] = [
    re.compile(r"(?i)(password|passwd|pwd)\s*[=:]\s*\S+"),
    re.compile(r"(?i)(token|access_token|refresh_token|bearer)\s*[=:]\s*\S+"),
    re.compile(r"(?i)(secret|api_key|apikey|private_key|client_secret)\s*[=:]\s*\S+"),
    re.compile(r"(?i)(ssn|social.?security)\s*[=:]\s*[\d\-]+"),
    re.compile(r"AKIA[A-Z0-9]{16}"),  # AWS access key ID pattern
    re.compile(
        r"(?i)authorization:\s*.+",  # HTTP Authorization header (any scheme + credentials)
    ),
]


# ---------------------------------------------------------------------------
# Canonical enumerations
# ---------------------------------------------------------------------------


class PipelineStage(StrEnum):
    """Canonical pipeline stage identifiers used in all log and audit events."""

    CONFIGURATION_LOAD = "configuration_load"
    CREDENTIAL_RETRIEVAL = "credential_retrieval"
    METADATA_DISCOVERY = "metadata_discovery"
    QUERY_BUILD = "query_build"
    EXTRACTION = "extraction"
    RAW_WRITE = "raw_write"
    SCHEMA_SNAPSHOT = "schema_snapshot"
    SCHEMA_DRIFT_EVALUATION = "schema_drift_evaluation"
    WATERMARK_UPDATE = "watermark_update"
    TRANSFORMATION = "transformation"
    CURATED_PUBLISH = "curated_publish"
    ENTITY_RESOLUTION = "entity_resolution"
    GOLDEN_RECORD_PUBLISH = "golden_record_publish"
    ANALYTICS_PUBLISH = "analytics_publish"
    TARGET_DB_LOAD = "target_db_load"
    # Phase 5 additions — orchestration lifecycle stages
    REPLAY_INITIATION = "replay_initiation"
    DLQ_ENQUEUE = "dlq_enqueue"
    RUN_COMPLETION = "run_completion"


class RunStatus(StrEnum):
    """Canonical run status values used across all pipeline stages."""

    STARTED = "started"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"
    RETRYING = "retrying"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# Structured log event model
# ---------------------------------------------------------------------------


class StructuredLogEvent(BaseModel):
    """
    Canonical structured log event schema.

    Every platform service must emit log events that conform to this model.
    Validation rejects any event carrying sensitive content in string fields.

    Required dimensions for CloudWatch Insights queries:
        run_id, source_id, entity_id, stage, status, duration_ms, retry_count
    """

    model_config = {"frozen": True}

    run_id: str = Field(
        ...,
        description="Immutable run identifier (UUID + timestamp). Never a sequential integer.",
    )
    source_id: str = Field(
        ...,
        description="Stable source system identifier, e.g. 'salesforce', 'netsuite', 'mysql-rds'.",
    )
    entity_id: str = Field(
        ...,
        description="Stable entity identifier, e.g. 'salesforce-account', 'netsuite-customer'.",
    )
    stage: PipelineStage = Field(..., description="Current pipeline stage at time of emission.")
    status: RunStatus = Field(..., description="Stage execution status at time of emission.")
    duration_ms: int = Field(
        ...,
        ge=0,
        description="Stage execution duration in milliseconds. 0 for START events.",
    )
    retry_count: int = Field(
        ...,
        ge=0,
        description="Number of retry attempts for this stage invocation.",
    )
    message: str = Field(
        default="",
        max_length=2048,
        description="Human-readable context message. Must not contain credentials or PII.",
    )
    record_count: int | None = Field(
        default=None,
        ge=0,
        description="Number of records processed in this stage, when applicable.",
    )
    error_classification: str | None = Field(
        default=None,
        description="Error classification string when status is FAILED (e.g. transient_timeout).",
    )
    schema_version: str | None = Field(
        default=None,
        description="Schema snapshot version in effect for this run.",
    )
    environment: str | None = Field(
        default=None,
        description="Deployment environment (dev, staging, prod).",
    )
    emitted_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="UTC timestamp when this log event was emitted. ISO 8601.",
    )

    # ── Validators ────────────────────────────────────────────────────────────

    @field_validator("run_id", mode="before")
    @classmethod
    def reject_sequential_integer_run_id(cls, value: str) -> str:
        """
        Reject run_ids that are bare sequential integers.

        Sequential integers enable enumeration attacks against run audit logs
        and break idempotency replay guarantees. run_id must include at minimum
        a timestamp or random component.
        Valid: 'run-2026-06-11-salesforce-001', 'run-a3f9c1d2-8b4e-...'
        Invalid: '1', '42', '999999'
        """
        if _SEQUENTIAL_INTEGER_PATTERN.match(value):
            raise ValueError(
                f"run_id '{value}' is a sequential integer, which is not permitted. "
                "Use a run_id that includes a timestamp or UUID component to prevent "
                "enumeration and ensure idempotency. "
                "Example: 'run-2026-06-11-salesforce-account-001'."
            )
        return value

    @field_validator("message", "error_classification", mode="before")
    @classmethod
    def reject_sensitive_content(cls, value: str | None) -> str | None:
        """
        Reject any field value that matches a known sensitive pattern.
        This is a hard reject — callers must scrub messages before emission.
        Use scrub_sensitive_values() on exception messages before building the event.
        """
        if value is None:
            return value
        for pattern in _SENSITIVE_PATTERNS:
            if pattern.search(value):
                raise ValueError(
                    "Log event contains a sensitive pattern and cannot be emitted. "
                    "Remove credentials, tokens, and PII before calling StructuredLogEvent. "
                    "Use scrub_sensitive_values() on exception messages."
                )
        return value

    @field_validator("source_id", "entity_id", mode="before")
    @classmethod
    def enforce_stable_identifier_format(cls, value: str) -> str:
        """
        Source and entity IDs must conform to the stable identifier format.
        Valid examples: 'salesforce', 'netsuite', 'salesforce-account', 'mysql-rds-order'.
        Invalid: 'Salesforce', 'salesforce_account', 'phase1', 'helper'.
        """
        if not _STABLE_ID_PATTERN.match(value):
            raise ValueError(
                f"Identifier '{value}' does not conform to the stable ID format. "
                "Use lowercase letters, digits, and hyphens only (2-64 chars). "
                "Example valid IDs: 'salesforce', 'salesforce-account', 'mysql-rds'."
            )
        if value in _PROHIBITED_IDENTIFIERS:
            raise ValueError(
                f"Identifier '{value}' is a prohibited generic name. "
                "Use a specific, domain-meaningful identifier instead."
            )
        return value


# ---------------------------------------------------------------------------
# Utility — sensitive value scrubber
# ---------------------------------------------------------------------------


def scrub_sensitive_values(text: str) -> str:
    """
    Scrub known sensitive patterns from a string before it enters the log pipeline.

    Use this as a safety net on exception messages, error strings, and any
    third-party output that may contain credentials or PII before building
    a StructuredLogEvent or passing the string to the logger.

    Example:
        safe_message = scrub_sensitive_values(str(exc))
        logger.error("extraction_failed", message=safe_message)
    """
    scrubbed = text
    for pattern in _SENSITIVE_PATTERNS:
        scrubbed = pattern.sub("[REDACTED]", scrubbed)
    return scrubbed
