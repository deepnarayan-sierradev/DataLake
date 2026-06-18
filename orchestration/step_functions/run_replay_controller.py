"""
Run replay controller for the Enterprise Data Lake platform.

Consumes failed-run entries from the SQS Dead-Letter Queue and re-triggers
an extraction run via AWS Step Functions.

Design:
  - Replay is idempotent: each replay produces a new run_id and does not
    modify the watermark of the original failed run.
  - DLQ entries are validated before triggering a new execution.
  - The original run_id is passed to the new execution for audit lineage.
  - No connector-specific logic lives here: the state machine input carries
    connector_params that the pipeline Lambda resolves.

Replay contract:
  - A replay execution includes is_replay=True and replay_of_run_id=<original>.
  - The watermark is NOT advanced if the original run already advanced it.
  - The new run emits REPLAY_INITIATION before starting the pipeline stages.

Security (OWASP A01, A07, A09):
  - DLQ entries are validated against a strict schema before use.
  - SQS receipt handle is only deleted after Step Functions execution is started.
  - Execution names include the run_id to prevent duplicate replay submissions.
  - Sensitive values are never in DLQ messages (enforced by RunCoordinator).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError

from contracts.identifier_policy import (
    RUN_ID_PATTERN as _RUN_ID_PATTERN,
)
from contracts.identifier_policy import (
    STABLE_ID_PATTERN as _STABLE_ID_PATTERN,
)
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# DLQ entry must have all of these keys.
_REQUIRED_DLQ_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "run_id",
        "source_id",
        "entity_id",
        "environment",
        "failed_stage",
        "error_code",
        "error_message",
        "enqueued_at",
    }
)

# Known deployment environments — matches Terraform variable validation.
_KNOWN_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"dev", "staging", "prod"})

# Step Functions execution names: alphanumeric + hyphens, max 80 chars.
_SFN_EXEC_NAME_MAX: Final[int] = 80


# ---------------------------------------------------------------------------
# Value objects and exceptions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DlqEntry:
    """
    Parsed and validated DLQ entry from the extraction failure queue.

    All fields are read-only after construction.  Sensitive values must
    never appear here (enforced upstream by RunCoordinator / scrub_sensitive_values).
    """

    run_id: str
    source_id: str
    entity_id: str
    environment: str
    failed_stage: str
    error_code: str
    error_message: str
    enqueued_at: str


class ReplayValidationError(Exception):
    """
    Raised when a DLQ entry is malformed or contains invalid field values.

    Replay is aborted when this is raised; the DLQ message is NOT deleted
    so it remains for manual inspection.
    """


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class RunReplayController:
    """
    Triggers replay executions for failed extraction runs.

    Usage pattern::

        controller = RunReplayController(
            state_machine_arn="arn:aws:states:...",
            region_name="us-east-1",
        )

        # From an SQS poller:
        entry = controller.parse_dlq_entry(message["Body"])
        execution_arn = controller.start_replay_execution(entry, connector_params)
    """

    def __init__(
        self,
        state_machine_arn: str,
        region_name: str,
    ) -> None:
        if not state_machine_arn:
            raise ValueError("state_machine_arn must not be empty.")
        self._state_machine_arn = state_machine_arn
        self._sfn = boto3.client("stepfunctions", region_name=region_name)

    # ── Public API ──────────────────────────────────────────────────────────

    def parse_dlq_entry(self, message_body: str) -> DlqEntry:
        """
        Parse and validate a raw SQS message body into a DlqEntry.

        Parameters
        ----------
        message_body : str
            JSON-encoded SQS message body from the extraction DLQ.

        Returns
        -------
        DlqEntry
            Validated, immutable entry ready for replay.

        Raises
        ------
        ReplayValidationError
            When the message is not valid JSON, is missing required fields,
            or contains invalid identifier values.
        """
        try:
            raw: dict[str, Any] = json.loads(message_body)
        except json.JSONDecodeError as exc:
            raise ReplayValidationError(f"DLQ message body is not valid JSON: {exc}") from exc

        if not isinstance(raw, dict):
            raise ReplayValidationError("DLQ message body must be a JSON object.")

        missing = _REQUIRED_DLQ_FIELDS - raw.keys()
        if missing:
            raise ReplayValidationError(f"DLQ entry is missing required fields: {sorted(missing)}")

        run_id = str(raw["run_id"])
        source_id = str(raw["source_id"])
        entity_id = str(raw["entity_id"])
        environment = str(raw["environment"])

        # Validate run_id before it is used in a Step Functions execution name
        # (OWASP A03 — injection prevention in resource identifiers).
        if not _RUN_ID_PATTERN.match(run_id):
            raise ReplayValidationError(
                f"run_id {run_id!r} in DLQ entry contains characters not permitted "
                "in Step Functions execution names.  Manual review required."
            )
        if not _STABLE_ID_PATTERN.match(source_id):
            raise ReplayValidationError(
                f"source_id {source_id!r} in DLQ entry does not conform to the "
                "stable identifier format.  Manual review required."
            )
        if not _STABLE_ID_PATTERN.match(entity_id):
            raise ReplayValidationError(
                f"entity_id {entity_id!r} in DLQ entry does not conform to the "
                "stable identifier format.  Manual review required."
            )
        if environment not in _KNOWN_ENVIRONMENTS:
            raise ReplayValidationError(
                f"environment {environment!r} in DLQ entry is not a known deployment "
                f"environment.  Expected one of {sorted(_KNOWN_ENVIRONMENTS)}.  "
                "Manual review required."
            )

        return DlqEntry(
            run_id=run_id,
            source_id=source_id,
            entity_id=entity_id,
            environment=environment,
            failed_stage=str(raw["failed_stage"]),
            error_code=str(raw["error_code"]),
            error_message=str(raw["error_message"]),
            enqueued_at=str(raw["enqueued_at"]),
        )

    def start_replay_execution(
        self,
        entry: DlqEntry,
        connector_params: dict[str, str],
    ) -> str:
        """
        Start a new Step Functions execution to replay the failed run.

        The new execution receives:
          - is_replay=True (so the pipeline emits REPLAY_INITIATION)
          - replay_of_run_id=<original run_id> (for audit lineage)
          - All original source/entity/connector parameters

        Parameters
        ----------
        entry : DlqEntry
            Validated DLQ entry from parse_dlq_entry().
        connector_params : dict[str, str]
            Source-specific connection parameters (e.g. record_type, table_name).
            These are not stored in the DLQ message to avoid coupling the DLQ
            message format to connector internals.

        Returns
        -------
        str
            The ARN of the newly-started Step Functions execution.

        Raises
        ------
        ClientError
            When the Step Functions StartExecution API call fails.
        """
        # Build a unique, deterministic execution name from the original run_id.
        # The "replay-" prefix makes replay executions easy to identify in the
        # Step Functions console.  Truncate to fit the 80-char limit.
        raw_name = f"replay-{entry.run_id}"
        execution_name = raw_name[:_SFN_EXEC_NAME_MAX]

        input_payload: dict[str, Any] = {
            "source_id": entry.source_id,
            "entity_id": entry.entity_id,
            "environment": entry.environment,
            "connector_params": connector_params,
            "is_replay": True,
            "replay_of_run_id": entry.run_id,
        }

        _logger.info(
            "replay_execution_starting",
            original_run_id=entry.run_id,
            source_id=entry.source_id,
            entity_id=entry.entity_id,
            failed_stage=entry.failed_stage,
            execution_name=execution_name,
        )

        try:
            response = self._sfn.start_execution(
                stateMachineArn=self._state_machine_arn,
                name=execution_name,
                input=json.dumps(input_payload, separators=(",", ":")),
            )
        except ClientError as exc:
            error_code = exc.response["Error"]["Code"]
            if error_code == "ExecutionAlreadyExists":
                # Idempotent: a replay execution for this run_id was already submitted
                # (e.g. SQS duplicate delivery or a concurrent DLQ consumer).
                # Construct the deterministic execution ARN and return it so the caller
                # can safely delete the SQS message without re-triggering the pipeline.
                # ARN pattern: arn:aws:states:{region}:{account}:execution:{sm_name}:{exec_name}
                arn_parts = self._state_machine_arn.split(":")
                idempotent_arn = (
                    ":".join(arn_parts[:-2]) + ":execution:" + arn_parts[-1] + ":" + execution_name
                )
                _logger.warning(
                    "replay_execution_already_exists",
                    original_run_id=entry.run_id,
                    source_id=entry.source_id,
                    entity_id=entry.entity_id,
                    execution_name=execution_name,
                    execution_arn=idempotent_arn,
                )
                return idempotent_arn
            _logger.error(
                "replay_execution_start_failed",
                original_run_id=entry.run_id,
                source_id=entry.source_id,
                entity_id=entry.entity_id,
                error_code=error_code,
            )
            raise

        execution_arn: str = response["executionArn"]
        _logger.info(
            "replay_execution_started",
            original_run_id=entry.run_id,
            source_id=entry.source_id,
            entity_id=entry.entity_id,
            execution_arn=execution_arn,
        )
        return execution_arn
