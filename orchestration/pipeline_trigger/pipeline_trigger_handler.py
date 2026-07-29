"""
Pipeline Trigger Lambda — SQS FIFO to Step Functions burst buffer.

Consumes messages from the EdlPipelineTrigger.fifo FIFO queue
(populated by EventBridge Scheduler) and starts one Step Functions execution
per message.

Architecture:
  EventBridge Scheduler (N simultaneous fires)
      │
      ▼ (writes to SQS FIFO queue — absorbs burst instantly)
  SQS FIFO Queue: EdlPipelineTrigger.fifo
      │
      ▼ (ESM batch_size=1, reserved_concurrency=50 caps execution rate)
  This Lambda (pipeline_trigger_handler)
      │
      ▼ (starts one Step Functions execution per message, idempotent)
  Step Functions State Machine → extraction → transformation → ...

Idempotency:
  Each Step Functions execution is started with a deterministic `name`
  parameter: `{tenant_code}--{source_id}--{entity_id}--{schedule_tick_iso}`.
  Step Functions rejects duplicate execution names with ExecutionAlreadyExists,
  which this handler treats as a successful no-op.  This guarantees exactly-once
  semantics even if SQS re-delivers a message after a visibility timeout.
  The 80-char SFN name limit is enforced by truncating the tenant/source/entity
  prefix, never the trailing schedule_tick_iso — the tick is what disambiguates
  two ticks for the same tenant/source/entity, and tenant_code is what
  disambiguates two tenants sharing a source/entity (ARCH-1); truncating from
  the tail (as a naive slice would) can silently drop the tick and collide two
  unrelated executions as ExecutionAlreadyExists.

Security (OWASP A03, A05):
  - All message body fields validated with Pydantic before use in any AWS call.
  - State machine ARN comes from Lambda environment variable — never from message.
  - No credentials or PII flow through this handler.
  - SQS message body must not contain credentials.

Required Lambda environment variables:
  AWS_REGION              — injected by Lambda runtime
  PLATFORM_ENVIRONMENT    — deployment environment (dev/staging/prod)
  STATE_MACHINE_ARN       — ARN of the Step Functions extraction pipeline state machine
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from typing import Any, Final

import boto3
import structlog
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, field_validator

from config_propagation.capability import ConfigCapability
from config_propagation.pinned_versions import PinnedConfigVersions
from config_propagation.pinning_service import ConfigPinningService
from contracts.identifier_policy import STABLE_ID_PATTERN, TENANT_CODE_PATTERN
from entity_resolution.resolution_config.resolution_config_registry import (
    ResolutionConfigRegistry,
)
from observability.lambda_runtime import check_lambda_timeout, require_env
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# Compiled once — used for execution name sanitisation.
_EXEC_NAME_SAFE: Final[re.Pattern[str]] = re.compile(r"[^a-zA-Z0-9\-_]")
# Step Functions execution name max length is 80 characters.
_EXEC_NAME_MAX_LEN: Final[int] = 80
# Reuse boto3 client across warm invocations (module-level singleton).
_sfn_client = boto3.client("stepfunctions", region_name=os.environ.get("AWS_REGION", "us-east-1"))


# ---------------------------------------------------------------------------
# Pydantic model for SQS message body validation (OWASP A03)
# ---------------------------------------------------------------------------


class TriggerMessage(BaseModel):
    """Validated shape of an SQS trigger message body."""

    model_config = {"extra": "forbid"}

    source_id: str = Field(..., min_length=2, max_length=64)
    entity_id: str = Field(..., min_length=2, max_length=64)
    environment: str = Field(..., pattern=r"^(dev|staging|prod)$")
    connector_params: dict[str, str] = Field(default_factory=dict)
    is_replay: bool = Field(default=False)
    # No default (ARCH-17, pre-go-live fix): a message that omits tenant_code
    # must fail Pydantic validation, not silently run under the "demo" tenant.
    # A fail-open default here would let a malformed or truncated message
    # start a real Step Functions execution against the wrong tenant's data
    # (OWASP A01 — broken access control via an implicit, attacker-reachable
    # default identity).
    tenant_code: str = Field(..., min_length=2, max_length=48)
    schedule_tick_iso: str = Field(
        default="",
        description="ISO-8601 UTC timestamp of the schedule tick; used in execution name.",
    )

    @field_validator("source_id", "entity_id")
    @classmethod
    def _validate_stable_id(cls, v: str) -> str:
        if not STABLE_ID_PATTERN.match(v):
            raise ValueError(f"{v!r} does not conform to the stable identifier format.")
        return v

    @field_validator("tenant_code")
    @classmethod
    def _validate_tenant_code(cls, v: str) -> str:
        if not TENANT_CODE_PATTERN.match(v):
            raise ValueError(f"tenant_code {v!r} does not conform to the tenant code format.")
        return v


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


def lambda_handler(event: dict[str, Any], context: Any) -> None:
    """
    AWS Lambda entry point — processes one SQS message per invocation.

    SQS ESM is configured with batch_size=1, so `Records` always has exactly
    one element.  Each record starts one Step Functions execution.

    Raises on failure (SQS will retry the message after VisibilityTimeout).
    """
    check_lambda_timeout(context, min_remaining_ms=30_000)
    # Bound and cleared here so every line this invocation emits carries the request id, and so
    # nothing leaks into the next invocation on a warm container.
    structlog.contextvars.bind_contextvars(aws_request_id=getattr(context, "aws_request_id", ""))
    try:
        _dispatch_records(event)
    finally:
        structlog.contextvars.clear_contextvars()


def _dispatch_records(event: dict[str, Any]) -> None:
    """Validate the ESM batch contract and start one execution per record."""
    state_machine_arn = require_env("STATE_MACHINE_ARN")

    records: list[dict[str, Any]] = event.get("Records", [])
    if not records:
        _logger.warning("pipeline_trigger_no_records_in_event")
        return

    # Enforce the batch_size=1 contract — if the ESM is misconfigured to send
    # multiple records, fail loudly rather than silently start multiple executions.
    # This is a defensive guard against operational misconfiguration (OWASP A05).
    if len(records) != 1:
        raise ValueError(
            f"pipeline_trigger: SQS ESM batch_size must be 1; "
            f"received {len(records)} records. Check the Event Source Mapping configuration."
        )

    for record in records:
        _process_record(record, state_machine_arn)


def _build_execution_name(tenant_code: str, source_id: str, entity_id: str, tick: str) -> str:
    """
    Build a Step Functions execution name that fits the 80-char limit without
    ever truncating away the trailing tick or the leading tenant_code (ARCH-1).

    A naive `f"{tenant_code}--{source_id}--{entity_id}--{tick}"[:80]` slice can
    drop the tick entirely when source_id/entity_id are long (each up to 64
    chars, well past the limit on its own) — two unrelated ticks would then
    collide on the same truncated name and the second's start_execution call
    would silently no-op as ExecutionAlreadyExists. Truncating the
    tenant/source/entity prefix instead, and always keeping the full tick
    suffix, preserves both per-tick and per-tenant uniqueness.
    """
    safe_tick = _EXEC_NAME_SAFE.sub("-", tick)
    suffix = f"--{safe_tick}"
    prefix_budget = max(_EXEC_NAME_MAX_LEN - len(suffix), 0)
    raw_prefix = f"{tenant_code}--{source_id}--{entity_id}"
    safe_prefix = _EXEC_NAME_SAFE.sub("-", raw_prefix)[:prefix_budget]
    return f"{safe_prefix}{suffix}"[:_EXEC_NAME_MAX_LEN]


def _process_record(record: dict[str, Any], state_machine_arn: str) -> None:
    """Process a single SQS record — validate, build execution name, start SFN."""
    message_id: str = record.get("messageId", "unknown")
    body_str: str = record.get("body", "{}")

    # --- Parse and validate message body (OWASP A03) ---
    try:
        body_dict = json.loads(body_str)
    except json.JSONDecodeError as exc:
        _logger.error(
            "pipeline_trigger_invalid_json",
            message_id=message_id,
            error=str(exc),
        )
        raise ValueError(f"SQS message {message_id!r} has invalid JSON body") from exc

    try:
        msg = TriggerMessage.model_validate(body_dict)
        structlog.contextvars.bind_contextvars(
            tenant_code=msg.tenant_code, source_id=msg.source_id, entity_id=msg.entity_id
        )
    except Exception as exc:
        _logger.error(
            "pipeline_trigger_validation_failed",
            message_id=message_id,
            error=str(exc),
        )
        raise ValueError(f"SQS message {message_id!r} failed validation: {exc}") from exc

    # --- Build deterministic, idempotent execution name ---
    tick = msg.schedule_tick_iso or datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    exec_name = _build_execution_name(msg.tenant_code, msg.source_id, msg.entity_id, tick)

    # --- Pin the configuration set once, at the run boundary (DL-CFG-01) ---
    # Every `latest` pointer is resolved here and carried in the payload, so a publish landing
    # mid-run cannot change behaviour under a stage that already started. Without this the run
    # reads whatever `latest` means at the moment each stage happens to look.
    pinned = _pin_configuration(msg)

    # --- Build Step Functions input payload ---
    sfn_input = json.dumps(
        {
            "source_id": msg.source_id,
            "entity_id": msg.entity_id,
            "environment": msg.environment,
            "connector_params": msg.connector_params,
            "is_replay": msg.is_replay,
            "tenant_code": msg.tenant_code,
            "pinned_config_versions": pinned.to_payload(),
            # L14: seeds the checkpoint-resume loop's bound. `States.MathAdd($.resume_attempts, 1)`
            # in the state machine needs the field to exist on the first pass.
            "resume_attempts": 0,
        },
        separators=(",", ":"),
    )

    # --- Start Step Functions execution (idempotent via execution name) ---
    try:
        _sfn_client.start_execution(
            stateMachineArn=state_machine_arn,
            name=exec_name,
            input=sfn_input,
        )
        _logger.info(
            "pipeline_trigger_execution_started",
            source_id=msg.source_id,
            entity_id=msg.entity_id,
            environment=msg.environment,
            tenant_code=msg.tenant_code,
            execution_name=exec_name,
        )
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        if error_code == "ExecutionAlreadyExists":
            # Idempotent re-delivery — safe to treat as success.
            _logger.info(
                "pipeline_trigger_execution_already_exists",
                source_id=msg.source_id,
                entity_id=msg.entity_id,
                execution_name=exec_name,
            )
            return
        _logger.error(
            "pipeline_trigger_sfn_start_failed",
            source_id=msg.source_id,
            entity_id=msg.entity_id,
            execution_name=exec_name,
            error_code=error_code,
        )
        raise


def _pin_configuration(msg: Any) -> PinnedConfigVersions:
    """
    Resolve every `latest` pointer this run will consume (DL-CFG-01).

    Resolvers are deliberately forgiving: a capability this run does not consume contributes
    nothing rather than failing the pin, because blocking a run on an unrelated capability's
    registry being unavailable would trade a consistency guarantee for an availability loss.
    """
    # The Lambda runtime always sets AWS_REGION, so its absence means we are not in a Lambda.
    # Read it rather than require it: failing the trigger on a missing pin would trade a
    # consistency guarantee for an availability loss, and an unpinned run is still correct —
    # just not protected against a mid-run publish (DL-CFG-01).
    region_name = os.environ.get("AWS_REGION", "")
    curated_bucket = os.environ.get("CURATED_S3_BUCKET", "")
    if not region_name:
        _logger.warning("config_pin_skipped_no_region", entity_id=msg.entity_id)
        return PinnedConfigVersions(pinned_at=datetime.now(UTC).isoformat())

    def _resolution_version(tenant_code: str, entity_key: str) -> str | None:
        if not curated_bucket:
            return None
        try:
            registry = ResolutionConfigRegistry(s3_bucket=curated_bucket, region_name=region_name)
            return registry.resolved_version(tenant_code, entity_key)
        except Exception as exc:
            _logger.info(
                "config_pin_resolver_unavailable",
                capability=ConfigCapability.ENTITY_RESOLUTION.value,
                tenant_code=tenant_code,
                entity_key=entity_key,
                error=str(exc),
            )
            return None

    service = ConfigPinningService({ConfigCapability.ENTITY_RESOLUTION: _resolution_version})
    return service.pin(msg.tenant_code, msg.entity_id)
