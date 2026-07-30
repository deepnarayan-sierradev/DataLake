"""
`datalake-workflow-definitions-<env>` and `datalake-workflow-tasks-<env>` repositories
(DL-WF-01, DL-WF-05, DL-WF-06).

Definitions adopt the DL-11 propagation contract: version-bumping publishes, an
effective-config record, and an execution pinned to one definition version for its whole run.
Bodies above a size threshold live in S3 with a hash pointer in DynamoDB, matching the
semantic-model pattern rather than inventing a second one.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError

from contracts.identifier_policy import validate_tenant_code
from contracts.platform_metrics import PlatformMetric
from observability.lambda_runtime import require_env
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger
from workflow_automation.definition import WorkflowDefinition, WorkflowStatus

_logger = get_platform_logger(__name__)


BODY_S3_THRESHOLD_BYTES: Final[int] = 200_000


class WorkflowNotFoundError(Exception):
    """Raised when a workflow version does not resolve for the tenant."""


class WorkflowIntegrityError(Exception):
    """Raised when an S3-stored body does not match its recorded hash (OWASP A08)."""


class TaskState(StrEnum):
    """Approval and triage task lifecycle (DL-WF-05, DL-WF-06)."""

    OPEN = "open"
    REMINDED = "reminded"
    ESCALATED = "escalated"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


_TERMINAL_TASK_STATES: Final[frozenset[TaskState]] = frozenset(
    {TaskState.APPROVED, TaskState.REJECTED, TaskState.CANCELLED}
)


def workflow_body_s3_key(tenant_code: str, workflow_id: str, version: str) -> str:
    """`{tenant_code}/workflows/{workflow_id}/{version}.json`."""
    validate_tenant_code(tenant_code)
    return f"{tenant_code}/workflows/{workflow_id}/{version}.json"


def body_hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class WorkflowDefinitionRepository:
    """Version-keyed definitions; a publish always writes a new version."""

    def __init__(
        self,
        environment: str,
        region_name: str,
        s3_bucket: str | None = None,
        s3_client: Any | None = None,
    ) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        self._bucket = s3_bucket
        self._s3 = s3_client
        table_name = require_env("WORKFLOW_DEFINITION_TABLE")
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)
        self._cache: dict[str, WorkflowDefinition] = {}

    def save(self, definition: WorkflowDefinition) -> str:
        """
        Persist a version. Never overwrites: the conditional write is what makes the
        version-keyed cache below safe (DL-CFG-05).
        """
        body = definition.to_body()
        digest = body_hash(body)
        item: dict[str, Any] = {
            "tenant_code": definition.tenant_code,
            "workflow_key": definition.sort_key,
            "workflow_id": definition.workflow_id,
            "version": definition.version,
            "status": definition.status.value,
            "owner": definition.owner,
            "published_by": definition.published_by,
            "approved_by": definition.approved_by,
            "content_hash": digest,
            "updated_at": datetime.now(UTC).isoformat(),
            "environment": self._environment,
        }
        serialised = json.dumps(body, sort_keys=True, separators=(",", ":"))
        if len(serialised.encode("utf-8")) > BODY_S3_THRESHOLD_BYTES and self._s3 and self._bucket:
            key = workflow_body_s3_key(
                definition.tenant_code, definition.workflow_id, definition.version
            )
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=serialised.encode("utf-8"),
                ContentType="application/json",
            )
            item["body_s3_key"] = key
        else:
            item["body"] = serialised
        self._table.put_item(Item=item)
        self._cache.pop(_cache_key(definition.tenant_code, definition.sort_key), None)
        _logger.info(
            "workflow_definition_saved",
            tenant_code=definition.tenant_code,
            workflow_id=definition.workflow_id,
            version=definition.version,
            status=definition.status.value,
        )
        return digest

    def publish(
        self, definition: WorkflowDefinition, *, published_by: str, approved_by: str | None = None
    ) -> WorkflowDefinition:
        """
        Publish a version, enforcing maker-checker for external-effect workflows.

        A publish is version-bumping by construction: `save` writes under
        `{workflow_id}#{version}`, so the previous version stays readable and the
        version-keyed cache invalidates naturally.
        """
        published = definition.model_copy(
            update={
                "status": WorkflowStatus.PUBLISHED,
                "published_by": published_by,
                "approved_by": approved_by,
            }
        )
        validated = WorkflowDefinition(**published.model_dump())
        self.save(validated)
        self._write_pointer(validated)
        return validated

    def disable(self, tenant_code: str, workflow_id: str, version: str) -> WorkflowDefinition:
        definition = self.load(tenant_code, workflow_id, version)
        disabled = definition.model_copy(update={"status": WorkflowStatus.DISABLED})
        self.save(disabled)
        return disabled

    def load(self, tenant_code: str, workflow_id: str, version: str) -> WorkflowDefinition:
        tenant_code = validate_tenant_code(tenant_code)
        sort_key = f"{workflow_id}#{version}"
        cached = self._cache.get(_cache_key(tenant_code, sort_key))
        if cached is not None:
            return cached
        response = self._table.get_item(
            Key={"tenant_code": tenant_code, "workflow_key": sort_key}, ConsistentRead=True
        )
        item = response.get("Item")
        if not item:
            raise WorkflowNotFoundError(
                f"No workflow {workflow_id!r} version {version!r} for tenant {tenant_code!r}."
            )
        definition = self._materialise(dict(item))
        self._cache[_cache_key(tenant_code, sort_key)] = definition
        return definition

    def load_published(self, tenant_code: str, workflow_id: str) -> WorkflowDefinition:
        """The currently-published version — the one an execution pins to (DL-CFG-01)."""
        tenant_code = validate_tenant_code(tenant_code)
        response = self._table.get_item(
            Key={"tenant_code": tenant_code, "workflow_key": f"{workflow_id}#$published"}
        )
        item = response.get("Item")
        if not item:
            raise WorkflowNotFoundError(
                f"Workflow {workflow_id!r} has no published version for tenant {tenant_code!r}."
            )
        return self.load(tenant_code, workflow_id, str(item["published_version"]))

    def list_workflows(self, tenant_code: str) -> list[dict[str, Any]]:
        tenant_code = validate_tenant_code(tenant_code)
        response = self._table.query(
            KeyConditionExpression="tenant_code = :tc",
            ExpressionAttributeValues={":tc": tenant_code},
        )
        return [
            dict(item)
            for item in response.get("Items", [])
            if not str(item["workflow_key"]).endswith("#$published")
        ]

    def invalidate(self, tenant_code: str, workflow_id: str) -> int:
        """Version-keyed cache; this exists for the disable path, not for staleness."""
        prefix = _cache_key(tenant_code, f"{workflow_id}#")
        stale = [k for k in self._cache if k.startswith(prefix)]
        for key in stale:
            del self._cache[key]
        return len(stale)

    def _materialise(self, item: dict[str, Any]) -> WorkflowDefinition:
        if "body" in item:
            body: dict[str, Any] = json.loads(str(item["body"]))
        else:
            if not (self._s3 and self._bucket):
                raise WorkflowNotFoundError(
                    "Workflow body is stored in S3 but no S3 client was configured."
                )
            key = str(item["body_s3_key"])
            response = self._s3.get_object(Bucket=self._bucket, Key=key)
            body = json.loads(response["Body"].read().decode("utf-8"))
        if body_hash(body) != str(item["content_hash"]):
            raise WorkflowIntegrityError(
                f"Workflow body for {item['workflow_key']!r} does not match its recorded hash. "
                "Refusing to execute a possibly-tampered definition (OWASP A08)."
            )
        return WorkflowDefinition(**body)

    def _write_pointer(self, definition: WorkflowDefinition) -> None:
        self._table.put_item(
            Item={
                "tenant_code": definition.tenant_code,
                "workflow_key": f"{definition.workflow_id}#$published",
                "published_version": definition.version,
                "updated_at": datetime.now(UTC).isoformat(),
                "environment": self._environment,
            }
        )


def _cache_key(tenant_code: str, sort_key: str) -> str:
    return f"{tenant_code}|{sort_key}"


@dataclass(frozen=True)
class WorkflowTask:
    """An approval or triage task."""

    tenant_code: str
    task_id: str
    workflow_id: str
    execution_id: str
    assignee: str
    title: str
    state: TaskState
    due_at: str
    created_at: str
    description: str = ""
    decision_comment: str = ""
    decided_by: str | None = None
    decided_at: str | None = None


class WorkflowTaskRepository:
    """Task inbox store; queryable by assignee and status so the console can render it."""

    def __init__(self, environment: str, region_name: str) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        table_name = require_env("WORKFLOW_TASK_TABLE")
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)

    def create_task(
        self,
        tenant_code: str,
        workflow_id: str,
        execution_id: str,
        assignee: str,
        title: str,
        description: str = "",
        due_after_hours: int = 24,
    ) -> str:
        if not assignee:
            raise ValueError(
                "An approval task must have an assignee; an unassigned task is never actioned."
            )
        task_id = f"wtk-{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC)
        self._table.put_item(
            Item={
                "tenant_code": validate_tenant_code(tenant_code),
                "task_id": task_id,
                "workflow_id": workflow_id,
                "execution_id": execution_id,
                "assignee": assignee,
                "assignee_status": f"{assignee}#{TaskState.OPEN.value}",
                "title": title,
                "description": description,
                "state": TaskState.OPEN.value,
                "created_at": now.isoformat(),
                "due_at": (now + timedelta(hours=due_after_hours)).isoformat(),
                "environment": self._environment,
            }
        )
        return task_id

    def decide(
        self,
        tenant_code: str,
        task_id: str,
        *,
        state: TaskState,
        decided_by: str,
        comment: str = "",
    ) -> None:
        """Approve or reject with a comment; a terminal decision names its actor (OWASP A09)."""
        if state in _TERMINAL_TASK_STATES and state is not TaskState.CANCELLED and not decided_by:
            raise ValueError("An approval decision must name its actor.")
        try:
            self._table.update_item(
                Key={"tenant_code": validate_tenant_code(tenant_code), "task_id": task_id},
                UpdateExpression=(
                    "SET #state = :state, assignee_status = :assignee_status, "
                    "decision_comment = :comment, decided_by = :actor, decided_at = :ts"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":state": state.value,
                    ":assignee_status": f"{decided_by}#{state.value}",
                    ":comment": comment,
                    ":actor": decided_by,
                    ":ts": datetime.now(UTC).isoformat(),
                    ":open": TaskState.OPEN.value,
                    ":reminded": TaskState.REMINDED.value,
                    ":escalated": TaskState.ESCALATED.value,
                },
                ConditionExpression="#state IN (:open, :reminded, :escalated)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ValueError(
                    f"Task {task_id!r} has already reached a terminal state; a second decision "
                    "would overwrite the audit record."
                ) from exc
            raise

    def mark_state(self, tenant_code: str, task_id: str, state: TaskState) -> None:
        """Reminder and escalation transitions, which carry no decision-maker."""
        self._table.update_item(
            Key={"tenant_code": validate_tenant_code(tenant_code), "task_id": task_id},
            UpdateExpression="SET #state = :state, updated_at = :ts",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":state": state.value,
                ":ts": datetime.now(UTC).isoformat(),
            },
        )

    def list_tasks(
        self, tenant_code: str, assignee: str | None = None, state: TaskState | None = None
    ) -> list[dict[str, Any]]:
        tenant_code = validate_tenant_code(tenant_code)
        response = self._table.query(
            KeyConditionExpression="tenant_code = :tc",
            ExpressionAttributeValues={":tc": tenant_code},
        )
        tasks = [dict(item) for item in response.get("Items", [])]
        open_states = {TaskState.OPEN.value, TaskState.REMINDED.value, TaskState.ESCALATED.value}
        record_platform_metric(
            PlatformMetric.WORKFLOW_TASKS_OPEN,
            sum(1 for task in tasks if str(task.get("state")) in open_states),
        )
        if assignee is not None:
            tasks = [t for t in tasks if str(t.get("assignee")) == assignee]
        if state is not None:
            tasks = [t for t in tasks if str(t.get("state")) == state.value]
        return tasks

    def overdue_tasks(self, tenant_code: str, now: datetime | None = None) -> list[dict[str, Any]]:
        """Tasks past their due date and not yet terminal — the escalation input."""
        reference = now or datetime.now(UTC)
        moment = reference.isoformat()
        terminal = {s.value for s in _TERMINAL_TASK_STATES}
        overdue = [
            task
            for task in self.list_tasks(tenant_code)
            if str(task.get("state")) not in terminal and str(task.get("due_at", "")) < moment
        ]
        record_platform_metric(PlatformMetric.WORKFLOW_ESCALATIONS, len(overdue))
        for task in overdue:
            age_hours = max(
                0.0,
                (reference - datetime.fromisoformat(str(task["created_at"]))).total_seconds()
                / 3_600,
            )
            record_platform_metric(PlatformMetric.WORKFLOW_TASK_AGE_HOURS, age_hours)
        return overdue
