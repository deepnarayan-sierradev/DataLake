"""
DLQ Processor Lambda — consumes extraction failure DLQ messages.

Reads messages from the {env}-edl-extraction-failure-dlq, validates them,
writes an audit record to the run audit log DynamoDB table, emits an SNS
notification, and optionally replays the failed run through Step Functions.

Architecture (§4.4):
  - SQS Event Source Mapping, batch_size=1 for clear per-message audit trail.
  - auto_replay=false by default — operator reviews and replays manually.
  - SNS notification includes run_id, source_id, entity_id, and failure_reason
    so on-call engineers have context without navigating CloudWatch.

Security (OWASP A03, A05, A09):
  - All message body fields validated with Pydantic before use.
  - DynamoDB table name and SNS topic ARN from Lambda env vars — never from message.
  - Audit log write includes only metadata — no record field values (OWASP A09).
  - SNS message sanitised — no PII, no credentials.

Required Lambda environment variables:
  AWS_REGION             — injected by Lambda runtime
  PLATFORM_ENVIRONMENT   — deployment environment
  RUN_AUDIT_LOG_TABLE    — name of the DynamoDB run audit log table
  ALERT_SNS_TOPIC_ARN    — ARN of the platform alerts SNS topic
  STATE_MACHINE_ARN      — ARN of the extraction pipeline state machine (for replay)
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, field_validator

from contracts.identifier_policy import STABLE_ID_PATTERN
from observability.lambda_utils import require_env, check_lambda_timeout
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# Module-level singleton boto3 clients (warm invocation cache)
_dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
_sns = boto3.client("sns", region_name=os.environ.get("AWS_REGION", "us-east-1"))
_sfn = boto3.client("stepfunctions", region_name=os.environ.get("AWS_REGION", "us-east-1"))

_STAGE_DLQ_RECEIVED: Final[str] = "dlq_received"


# ---------------------------------------------------------------------------
# Pydantic model for DLQ message validation (OWASP A03)
# ---------------------------------------------------------------------------


class DLQMessage(BaseModel):
    """Validated shape of an extraction failure DLQ message body."""

    model_config = {"extra": "allow"}  # allow extra fields — DLQ preserves original payload

    run_id: str = Field(..., min_length=2, max_length=100)
    source_id: str = Field(..., min_length=2, max_length=64)
    entity_id: str = Field(..., min_length=2, max_length=64)
    environment: str = Field(..., pattern=r"^(dev|staging|prod)$")
    failure_reason: str = Field(default="unknown")
    failure_stage: str = Field(default="unknown")
    connector_params: dict[str, str] = Field(default_factory=dict)
    is_replay: bool = Field(default=False)

    @field_validator("source_id", "entity_id")
    @classmethod
    def _validate_stable_id(cls, v: str) -> str:
        if not STABLE_ID_PATTERN.match(v):
            raise ValueError(f"{v!r} does not conform to the stable identifier format.")
        return v


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


def lambda_handler(event: dict[str, Any], context: Any) -> None:
    """
    AWS Lambda entry point — processes one DLQ message per invocation.

    SQS ESM is configured with batch_size=1.
    """
    check_lambda_timeout(context, min_remaining_ms=30_000)

    audit_table_name = require_env("RUN_AUDIT_LOG_TABLE")
    alert_sns_topic_arn = require_env("ALERT_SNS_TOPIC_ARN")
    state_machine_arn = os.environ.get("STATE_MACHINE_ARN", "")
    environment = require_env("PLATFORM_ENVIRONMENT")
    auto_replay = os.environ.get("AUTO_REPLAY", "false").lower() == "true"

    records = event.get("Records", [])
    if not records:
        _logger.warning("dlq_processor_no_records")
        return

    for record in records:
        _process_dlq_record(
            record=record,
            audit_table_name=audit_table_name,
            alert_sns_topic_arn=alert_sns_topic_arn,
            state_machine_arn=state_machine_arn,
            environment=environment,
            auto_replay=auto_replay,
        )


def _process_dlq_record(
    record: dict[str, Any],
    audit_table_name: str,
    alert_sns_topic_arn: str,
    state_machine_arn: str,
    environment: str,
    auto_replay: bool,
) -> None:
    """Process a single DLQ message: validate → audit → notify → optional replay."""
    message_id: str = record.get("messageId", "unknown")
    body_str: str = record.get("body", "{}")

    # --- Parse and validate ---
    try:
        body_dict = json.loads(body_str)
    except json.JSONDecodeError as exc:
        _logger.error(
            "dlq_processor_invalid_json",
            message_id=message_id,
            error=str(exc),
        )
        raise ValueError(f"DLQ message {message_id!r} has invalid JSON body") from exc

    try:
        msg = DLQMessage.model_validate(body_dict)
    except Exception as exc:
        _logger.error(
            "dlq_processor_validation_failed",
            message_id=message_id,
            error=str(exc),
        )
        raise ValueError(f"DLQ message {message_id!r} failed validation: {exc}") from exc

    received_at = datetime.now(UTC).isoformat()

    _logger.info(
        "dlq_message_received",
        run_id=msg.run_id,
        source_id=msg.source_id,
        entity_id=msg.entity_id,
        environment=environment,
        failure_reason=msg.failure_reason,
        failure_stage=msg.failure_stage,
    )

    # --- Write audit record ---
    _write_audit_record(
        audit_table_name=audit_table_name,
        msg=msg,
        received_at=received_at,
    )

    # --- SNS notification ---
    _send_sns_notification(
        topic_arn=alert_sns_topic_arn,
        msg=msg,
        environment=environment,
    )

    # --- Optional auto replay ---
    if auto_replay and state_machine_arn:
        _replay_failed_run(
            state_machine_arn=state_machine_arn,
            msg=msg,
        )


def _write_audit_record(
    audit_table_name: str,
    msg: DLQMessage,
    received_at: str,
) -> None:
    """Write a DLQ receipt audit record to the run audit log DynamoDB table."""
    table = _dynamodb.Table(audit_table_name)
    try:
        table.put_item(
            Item={
                "run_id": msg.run_id,
                "stage": _STAGE_DLQ_RECEIVED,
                "source_id": msg.source_id,
                "entity_id": msg.entity_id,
                "environment": msg.environment,
                "failure_reason": msg.failure_reason,
                "failure_stage": msg.failure_stage,
                "received_at": received_at,
                "source_entity_key": f"{msg.source_id}#{msg.entity_id}",
                "started_at": received_at,  # Required for GSI sort key
            }
        )
        _logger.info(
            "dlq_audit_record_written",
            run_id=msg.run_id,
            source_id=msg.source_id,
            entity_id=msg.entity_id,
        )
    except ClientError as exc:
        # Audit write failure must not prevent SNS notification or replay.
        _logger.warning(
            "dlq_audit_record_write_failed",
            run_id=msg.run_id,
            error=str(exc),
        )


def _send_sns_notification(
    topic_arn: str,
    msg: DLQMessage,
    environment: str,
) -> None:
    """Send an SNS alert with DLQ message metadata (no PII, no record values)."""
    subject = f"[{environment.upper()}] Pipeline DLQ: {msg.source_id}/{msg.entity_id}"
    body = json.dumps(
        {
            "alert_type": "pipeline_dlq_message",
            "environment": environment,
            "run_id": msg.run_id,
            "source_id": msg.source_id,
            "entity_id": msg.entity_id,
            "failure_reason": msg.failure_reason,
            "failure_stage": msg.failure_stage,
        },
        indent=2,
    )
    try:
        _sns.publish(
            TopicArn=topic_arn,
            Subject=subject[:100],  # SNS subject max = 100 chars
            Message=body,
        )
        _logger.info(
            "dlq_sns_notification_sent",
            run_id=msg.run_id,
            source_id=msg.source_id,
        )
    except ClientError as exc:
        _logger.warning(
            "dlq_sns_notification_failed",
            run_id=msg.run_id,
            error=str(exc),
        )


def _replay_failed_run(state_machine_arn: str, msg: DLQMessage) -> None:
    """Re-submit the failed run through Step Functions (auto_replay=true only)."""
    import re
    _safe = re.compile(r"[^a-zA-Z0-9\-_]")
    exec_name = _safe.sub("-", f"replay-{msg.run_id}")[:80]

    sfn_input = json.dumps(
        {
            "source_id": msg.source_id,
            "entity_id": msg.entity_id,
            "environment": msg.environment,
            "connector_params": msg.connector_params,
            "is_replay": True,
            "replay_of_run_id": msg.run_id,
        },
        separators=(",", ":"),
    )
    try:
        _sfn.start_execution(
            stateMachineArn=state_machine_arn,
            name=exec_name,
            input=sfn_input,
        )
        _logger.info(
            "dlq_auto_replay_started",
            run_id=msg.run_id,
            source_id=msg.source_id,
            execution_name=exec_name,
        )
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        if error_code == "ExecutionAlreadyExists":
            _logger.info(
                "dlq_auto_replay_already_exists",
                run_id=msg.run_id,
                execution_name=exec_name,
            )
            return
        _logger.error(
            "dlq_auto_replay_failed",
            run_id=msg.run_id,
            error_code=error_code,
        )
        raise
