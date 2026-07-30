"""
EventBridge Scheduler client for the Enterprise Data Lake platform.

Manages per-entity extraction schedules in an EventBridge Scheduler schedule
group.  Each entity has exactly one schedule; create_or_update_schedule()
is idempotent — it creates a new schedule or updates an existing one.

Schedule naming: {tenant_code}--{source_id}--{entity_id}
  - Double hyphen separates ALL THREE components (tenant, source, entity) —
    not just source/entity — to avoid ambiguity with single-hyphen stable
    identifiers (e.g. "netsuite" / "netsuite-customer") AND with hyphenated
    tenant codes (e.g. "acme-corp" / "globex-eu"). A single-hyphen join
    between tenant_code and source_id previously let two distinct tenants
    collide on the same literal schedule name — e.g. tenant="acme",
    source="corp-salesforce" produced the same name as tenant="acme-corp",
    source="salesforce" (ARCH-16, pre-go-live blocker). Since
    create_or_update_schedule() tries an update first, that collision meant
    Tenant B's onboarding would silently clobber Tenant A's cron,
    connector_params, and the tenant_code embedded in its own Step Functions
    input — a cross-tenant data/schedule leak, not just a cosmetic clash.
  - tenant_code prefix (ARCH-1) prevents two tenants onboarding the same
    source/entity from silently overwriting each other's live schedule.
  - EventBridge Scheduler schedule names are capped at 64 characters. Since
    tenant_code (up to 48 chars) and source_id/entity_id (up to 64 chars
    each) can individually exceed that budget once joined, names longer than
    64 chars are deterministically collapsed to a truncated-prefix +
    content-hash form (see `_build_schedule_name()` / `_MAX_SCHEDULE_NAME_LEN`)
    rather than naively sliced — a naive slice could make two distinct
    long-id tenants collide on the same truncated name.

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

import hashlib
import json
import re
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError

from contracts.identifier_policy import validate_tenant_code
from observability.structured_logger import get_platform_logger
from tenancy.connection_keys import resolve_connection_id

_logger = get_platform_logger(__name__)

_STABLE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9\-]{1,63}$")

_SCHEDULE_NAME_SEP: Final[str] = "--"

_MAX_SCHEDULE_NAME_LEN: Final[int] = 64

_SCHEDULE_NAME_HASH_LEN: Final[int] = 10

DEFAULT_FLEXIBLE_WINDOW_MINUTES: Final[int] = 5
_FLEXIBLE_WINDOW_OFF: Final[dict[str, str]] = {"Mode": "OFF"}


def _flexible_window(minutes: int) -> dict[str, Any]:
    """Jitter window for schedule fan-out (DL-OPS-11); 0 minutes disables it."""
    if minutes <= 0:
        return dict(_FLEXIBLE_WINDOW_OFF)
    return {"Mode": "FLEXIBLE", "MaximumWindowInMinutes": minutes}


class ScheduleNotFoundError(Exception):
    """Raised when get_schedule() is called for a non-existent schedule."""


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
      environment          Deployment environment (dev / uat / prod).  Passed
                           as the 'environment' field in the Step Functions input
                           payload — required by the extraction Lambda handler.
      region_name          AWS region.
    """

    def __init__(
        self,
        schedule_group_name: str,
        target_arn: str,
        execution_role_arn: str,
        environment: str,
        region_name: str,
        flexible_window_minutes: int = DEFAULT_FLEXIBLE_WINDOW_MINUTES,
    ) -> None:
        if not schedule_group_name:
            raise ValueError("schedule_group_name must not be empty.")
        if not target_arn:
            raise ValueError("target_arn must not be empty.")
        if not execution_role_arn:
            raise ValueError("execution_role_arn must not be empty.")
        if not environment:
            raise ValueError("environment must not be empty.")
        self._group_name = schedule_group_name
        self._target_arn = target_arn
        self._execution_role_arn = execution_role_arn
        self._environment = environment
        self._flexible_window_minutes = flexible_window_minutes
        self._scheduler = boto3.client("scheduler", region_name=region_name)

    def create_or_update_schedule(
        self,
        source_id: str,
        entity_id: str,
        cron_expression: str,
        connector_params: dict[str, str],
        timezone: str = "UTC",
        tenant_code: str = "demo",
        connection_id: str | None = None,
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
        tenant_code : str
            Tenant identity for this schedule (ARCH-1) — prefixes the schedule
            name so two tenants onboarding the same source/entity never share
            (and silently overwrite) one schedule.

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
        tenant_code = validate_tenant_code(tenant_code)

        schedule_name = _build_schedule_name(tenant_code, source_id, entity_id, connection_id)
        sfn_input = json.dumps(
            {
                "source_id": source_id,
                "connection_id": resolve_connection_id(source_id, connection_id),
                "entity_id": entity_id,
                "environment": self._environment,
                "connector_params": connector_params,
                "is_replay": False,
                "tenant_code": tenant_code,
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
            "FlexibleTimeWindow": _flexible_window(self._flexible_window_minutes),
            "Target": target,
            "State": "ENABLED",
        }

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

    def delete_schedule(
        self,
        source_id: str,
        entity_id: str,
        tenant_code: str = "demo",
        connection_id: str | None = None,
    ) -> None:
        """
        Delete the extraction schedule for a source entity.

        Parameters
        ----------
        source_id : str
            Stable source identifier.
        entity_id : str
            Stable entity identifier.
        tenant_code : str
            Tenant identity — must match the tenant the schedule was created
            under, or the wrong (non-existent) schedule name is targeted.

        Raises
        ------
        ScheduleNotFoundError
            When no schedule exists for the given source/entity.
        ClientError
            When the EventBridge Scheduler API call fails for other reasons.
        """
        _validate_stable_id("source_id", source_id)
        _validate_stable_id("entity_id", entity_id)
        tenant_code = validate_tenant_code(tenant_code)

        schedule_name = _build_schedule_name(tenant_code, source_id, entity_id, connection_id)
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

    def get_schedule(
        self,
        source_id: str,
        entity_id: str,
        tenant_code: str = "demo",
        connection_id: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Retrieve the current schedule configuration for a source entity.

        Parameters
        ----------
        source_id : str
            Stable source identifier.
        entity_id : str
            Stable entity identifier.
        tenant_code : str
            Tenant identity the schedule was created under.

        Returns
        -------
        dict[str, Any] | None
            Raw API response dict when the schedule exists; None otherwise.
        """
        _validate_stable_id("source_id", source_id)
        _validate_stable_id("entity_id", entity_id)
        tenant_code = validate_tenant_code(tenant_code)

        schedule_name = _build_schedule_name(tenant_code, source_id, entity_id, connection_id)
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
    def build_schedule_name(
        source_id: str,
        entity_id: str,
        tenant_code: str = "demo",
        connection_id: str | None = None,
    ) -> str:
        """Return the deterministic schedule name for a tenant/connection/entity tuple."""
        return _build_schedule_name(
            validate_tenant_code(tenant_code), source_id, entity_id, connection_id
        )


def _validate_stable_id(field_name: str, value: str) -> None:
    """Raise ValueError when value is not a valid stable identifier."""
    if not _STABLE_ID_PATTERN.match(value):
        raise ValueError(
            f"{field_name}={value!r} does not conform to the stable identifier format. "
            "Use lowercase letters, digits, and hyphens only (2-64 chars, "
            "must start with a letter)."
        )


def _build_schedule_name(
    tenant_code: str, source_id: str, entity_id: str, connection_id: str | None = None
) -> str:
    """
    Build the EventBridge Scheduler schedule name for a tenant/source/entity tuple.

    All three components are joined with the same double-hyphen separator
    (ARCH-16) — joining tenant_code and source_id with a single hyphen let two
    distinct tenants collide on one literal name (e.g. tenant="acme",
    source="corp-salesforce" vs. tenant="acme-corp", source="salesforce"),
    and create_or_update_schedule() is update-first, so the collision was a
    silent cross-tenant schedule clobber, not just a cosmetic name clash.

    EventBridge Scheduler caps schedule names at _MAX_SCHEDULE_NAME_LEN (64)
    characters. tenant_code (<=48 chars) and source_id/entity_id (<=64 chars
    each) can individually push the joined name past that cap, so names that
    would exceed it are deterministically collapsed to a truncated prefix of
    the full name plus a short content-hash suffix — never a naive slice,
    which could make two distinct long-id tuples collide on the same
    truncated name.
    """
    key_id = resolve_connection_id(source_id, connection_id)
    name = f"{tenant_code}{_SCHEDULE_NAME_SEP}{key_id}{_SCHEDULE_NAME_SEP}{entity_id}"
    if len(name) <= _MAX_SCHEDULE_NAME_LEN:
        return name

    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:_SCHEDULE_NAME_HASH_LEN]
    prefix_budget = _MAX_SCHEDULE_NAME_LEN - len(digest) - len(_SCHEDULE_NAME_SEP)
    return f"{name[:prefix_budget]}{_SCHEDULE_NAME_SEP}{digest}"
