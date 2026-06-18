"""
EventBridge Scheduler client for the Enterprise Data Lake platform.

Manages per-entity extraction schedules in an EventBridge Scheduler schedule
group.  Each entity has exactly one schedule; create_or_update_schedule()
is idempotent — it creates a new schedule or updates an existing one.

Schedule naming: {source_id}--{entity_id}
  - Double hyphen separates source and entity to avoid ambiguity with
    single-hyphen stable identifiers (e.g. "netsuite" / "netsuite-customer").

Schedule target: the Step Functions state machine that runs the extraction
pipeline.  The schedule passes the source_id, entity_id, and connector_params
as the Step Functions input payload.

Security (OWASP A01, A05):
  - Schedule names are constructed from validated stable identifiers only
    (OWASP A03 — no user-controlled input in resource names).
  - The IAM execution role ARN is passed as a constructor argument; the client
    never constructs or guesses ARNs.
  - Connector params are embedded in the schedule input; they must not contain
    credentials (credentials come from Secrets Manager at runtime).
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError

from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# Stable identifier format — same constraint used platform-wide.
_STABLE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9\-]{1,63}$")

# Schedule name: {source_id}--{entity_id}
_SCHEDULE_NAME_SEP: Final[str] = "--"

# EventBridge Scheduler flexible time window (OFF = exact schedule time).
_FLEXIBLE_WINDOW_OFF: Final[dict[str, str]] = {"Mode": "OFF"}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ScheduleNotFoundError(Exception):
    """Raised when get_schedule() is called for a non-existent schedule."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ExtractionScheduleClient:
    """
    Manages EventBridge Scheduler schedules for entity extraction runs.

    One instance per application or Lambda invocation; the boto3 scheduler
    client is thread-safe for read/write operations.

    Constructor args:
      schedule_group_name  Name of the EventBridge Scheduler schedule group.
      target_arn           Step Functions state machine ARN to invoke.
      execution_role_arn   IAM role ARN that EventBridge assumes to start SFN
                           executions.  Must have sfn:StartExecution permission.
      region_name          AWS region.
    """

    def __init__(
        self,
        schedule_group_name: str,
        target_arn: str,
        execution_role_arn: str,
        region_name: str,
    ) -> None:
        if not schedule_group_name:
            raise ValueError("schedule_group_name must not be empty.")
        if not target_arn:
            raise ValueError("target_arn must not be empty.")
        if not execution_role_arn:
            raise ValueError("execution_role_arn must not be empty.")
        self._group_name = schedule_group_name
        self._target_arn = target_arn
        self._execution_role_arn = execution_role_arn
        self._scheduler = boto3.client("scheduler", region_name=region_name)

    # ── Public API ──────────────────────────────────────────────────────────

    def create_or_update_schedule(
        self,
        source_id: str,
        entity_id: str,
        cron_expression: str,
        connector_params: dict[str, str],
        timezone: str = "UTC",
    ) -> str:
        """
        Create or update the extraction schedule for a source entity.

        Parameters
        ----------
        source_id : str
            Stable source identifier (e.g. 'salesforce').
        entity_id : str
            Stable entity identifier (e.g. 'salesforce-account').
        cron_expression : str
            EventBridge Scheduler cron expression, e.g. 'cron(0 2 * * ? *)'.
        connector_params : dict[str, str]
            Source-specific connection parameters passed to the pipeline as the
            Step Functions input payload.  Must NOT contain credentials.
        timezone : str
            IANA timezone name (default 'UTC').

        Returns
        -------
        str
            ARN of the created or updated schedule.

        Raises
        ------
        ValueError
            When source_id or entity_id do not conform to the stable ID format.
        ClientError
            When the EventBridge Scheduler API call fails.
        """
        _validate_stable_id("source_id", source_id)
        _validate_stable_id("entity_id", entity_id)

        schedule_name = _build_schedule_name(source_id, entity_id)
        sfn_input = json.dumps(
            {
                "source_id": source_id,
                "entity_id": entity_id,
                "connector_params": connector_params,
                "is_replay": False,
            },
            separators=(",", ":"),
        )
        target: dict[str, Any] = {
            "Arn": self._target_arn,
            "RoleArn": self._execution_role_arn,
            "Input": sfn_input,
        }
        kwargs: dict[str, Any] = {
            "GroupName": self._group_name,
            "Name": schedule_name,
            "ScheduleExpression": cron_expression,
            "ScheduleExpressionTimezone": timezone,
            "FlexibleTimeWindow": _FLEXIBLE_WINDOW_OFF,
            "Target": target,
            "State": "ENABLED",
        }

        # Try to update existing schedule first; if not found, create it.
        try:
            response = self._scheduler.update_schedule(**kwargs)
            schedule_arn: str = response["ScheduleArn"]
            _logger.info(
                "extraction_schedule_updated",
                source_id=source_id,
                entity_id=entity_id,
                schedule_name=schedule_name,
                schedule_arn=schedule_arn,
            )
            return schedule_arn
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
            # Schedule does not exist — create it.

        response = self._scheduler.create_schedule(**kwargs)
        schedule_arn = response["ScheduleArn"]
        _logger.info(
            "extraction_schedule_created",
            source_id=source_id,
            entity_id=entity_id,
            schedule_name=schedule_name,
            schedule_arn=schedule_arn,
        )
        return schedule_arn

    def delete_schedule(self, source_id: str, entity_id: str) -> None:
        """
        Delete the extraction schedule for a source entity.

        Parameters
        ----------
        source_id : str
            Stable source identifier.
        entity_id : str
            Stable entity identifier.

        Raises
        ------
        ScheduleNotFoundError
            When no schedule exists for the given source/entity.
        ClientError
            When the EventBridge Scheduler API call fails for other reasons.
        """
        _validate_stable_id("source_id", source_id)
        _validate_stable_id("entity_id", entity_id)

        schedule_name = _build_schedule_name(source_id, entity_id)
        try:
            self._scheduler.delete_schedule(
                GroupName=self._group_name,
                Name=schedule_name,
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                raise ScheduleNotFoundError(
                    f"No schedule found for source_id={source_id!r} "
                    f"entity_id={entity_id!r} in group {self._group_name!r}."
                ) from exc
            raise
        _logger.info(
            "extraction_schedule_deleted",
            source_id=source_id,
            entity_id=entity_id,
            schedule_name=schedule_name,
        )

    def get_schedule(self, source_id: str, entity_id: str) -> dict[str, Any] | None:
        """
        Retrieve the current schedule configuration for a source entity.

        Parameters
        ----------
        source_id : str
            Stable source identifier.
        entity_id : str
            Stable entity identifier.

        Returns
        -------
        dict[str, Any] | None
            Raw API response dict when the schedule exists; None otherwise.
        """
        _validate_stable_id("source_id", source_id)
        _validate_stable_id("entity_id", entity_id)

        schedule_name = _build_schedule_name(source_id, entity_id)
        try:
            response: dict[str, Any] = self._scheduler.get_schedule(
                GroupName=self._group_name,
                Name=schedule_name,
            )
            return response
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                return None
            raise

    @staticmethod
    def build_schedule_name(source_id: str, entity_id: str) -> str:
        """Return the deterministic schedule name for a source/entity pair."""
        return _build_schedule_name(source_id, entity_id)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _validate_stable_id(field_name: str, value: str) -> None:
    """Raise ValueError when value is not a valid stable identifier."""
    if not _STABLE_ID_PATTERN.match(value):
        raise ValueError(
            f"{field_name}={value!r} does not conform to the stable identifier format. "
            "Use lowercase letters, digits, and hyphens only (2-64 chars, "
            "must start with a letter)."
        )


def _build_schedule_name(source_id: str, entity_id: str) -> str:
    """Build the EventBridge Scheduler schedule name for a source/entity pair."""
    return f"{source_id}{_SCHEDULE_NAME_SEP}{entity_id}"
