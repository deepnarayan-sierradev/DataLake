"""
The registered action handlers (DL-WF-04).

Each handler is small on purpose: the engine owns retry, idempotency, and circuit breaking, so
a handler only performs its effect. Outbound destinations come from a per-tenant allowlist and
are signed with a per-destination secret — no user-supplied URL is called (OWASP A05, A10).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlparse

import boto3

from observability.structured_logger import get_platform_logger
from workflow_automation.action_registry import (
    ActionContext,
    WorkflowActionHandler,
    action_registry,
)
from workflow_automation.definition import ActionKind

_logger = get_platform_logger(__name__)

_DESTINATION_TABLE_NAME: Final[str] = "EdlWorkflowDestination"


class DestinationNotAllowedError(Exception):
    """Raised when a workflow names a webhook destination outside the tenant allowlist."""


@dataclass(frozen=True)
class OutboundDestination:
    """An allowlisted webhook destination and the secret its payloads are signed with."""

    destination_id: str
    url: str
    secret_arn: str

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError(f"destination {self.destination_id!r}: url must be absolute https.")

    @property
    def hostname(self) -> str:
        return urlparse(self.url).netloc.lower()


class DestinationAllowlist:
    """Per-tenant registry of permitted outbound destinations."""

    def __init__(self, region_name: str, table_name: str | None = None) -> None:
        resolved = (
            table_name or os.environ.get("WORKFLOW_DESTINATION_TABLE") or _DESTINATION_TABLE_NAME
        )
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(resolved)

    def register(self, tenant_code: str, destination: OutboundDestination) -> None:
        self._table.put_item(
            Item={
                "tenant_code": tenant_code,
                "destination_id": destination.destination_id,
                "url": destination.url,
                "secret_arn": destination.secret_arn,
            }
        )

    def resolve(self, tenant_code: str, destination_id: str) -> OutboundDestination:
        response = self._table.get_item(
            Key={"tenant_code": tenant_code, "destination_id": destination_id}
        )
        item = response.get("Item")
        if not item:
            raise DestinationNotAllowedError(
                f"Destination {destination_id!r} is not on tenant {tenant_code!r}'s allowlist. "
                "Outbound destinations are an affirmative allowlist, never caller-supplied."
            )
        return OutboundDestination(
            destination_id=destination_id,
            url=str(item["url"]),
            secret_arn=str(item["secret_arn"]),
        )


def sign_outbound_payload(secret: str, body: str) -> str:
    """HMAC-SHA256 signature so a destination can verify the payload came from us."""
    return hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


class SendNotificationAction(WorkflowActionHandler):
    """Publishes to SNS; the topic is resolved from configuration, never from the payload."""

    kind = ActionKind.SEND_NOTIFICATION

    def __init__(self, region_name: str, sns_client: Any | None = None) -> None:
        self._sns = sns_client or boto3.client("sns", region_name=region_name)

    def describe(self, parameters: dict[str, str]) -> str:
        return (
            f"send notification to topic {parameters.get('topic_arn', '<unset>')} "
            f"with subject {parameters.get('subject', '<unset>')!r}"
        )

    def destination(self, parameters: dict[str, str]) -> str:
        return f"sns:{parameters.get('topic_arn', 'unset')}"

    def execute(self, parameters: dict[str, str], context: ActionContext) -> dict[str, Any]:
        topic_arn = parameters.get("topic_arn", "")
        if not topic_arn.startswith("arn:aws:sns:"):
            raise ValueError("send_notification requires a valid SNS topic ARN.")
        message = {
            "tenant_code": context.tenant_code,
            "workflow_id": context.workflow_id,
            "execution_id": context.execution_id,
            "correlation_id": context.correlation_id,
            "subject": parameters.get("subject", "Workflow notification"),
            "body": parameters.get("body", ""),
            "condition_values": context.condition_values,
        }
        response = self._sns.publish(
            TopicArn=topic_arn,
            Subject=parameters.get("subject", "Workflow notification")[:100],
            Message=json.dumps(message, separators=(",", ":")),
        )
        return {"message_id": response.get("MessageId", "")}


class WriteExceptionAction(WorkflowActionHandler):
    """Writes a quality exception; the store is shared with DL-02 (no parallel history)."""

    kind = ActionKind.WRITE_EXCEPTION

    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def describe(self, parameters: dict[str, str]) -> str:
        return f"write exception rule={parameters.get('rule_id', '<unset>')}"

    def execute(self, parameters: dict[str, str], context: ActionContext) -> dict[str, Any]:
        from data_quality.exception_repository import (
            ExceptionKind,
            ExceptionSeverity,
            QualityException,
        )

        exception = QualityException(
            tenant_code=context.tenant_code,
            run_id=context.execution_id,
            rule_id=parameters.get("rule_id", "workflow"),
            entity_id=parameters.get("entity_id", "workflow"),
            kind=ExceptionKind.QUALITY_VIOLATION,
            severity=ExceptionSeverity(parameters.get("severity", "warn")),
            message=parameters.get("message", "Raised by a workflow condition."),
            correlation_id=context.correlation_id,
        )
        return {"exception_key": self._repository.record(exception)}


class CreateApprovalTaskAction(WorkflowActionHandler):
    """Creates a human approval task; the engine owns escalation, not this handler."""

    kind = ActionKind.CREATE_APPROVAL_TASK

    def __init__(self, task_repository: Any) -> None:
        self._tasks = task_repository

    def describe(self, parameters: dict[str, str]) -> str:
        return (
            f"create approval task for {parameters.get('assignee', '<unset>')}: "
            f"{parameters.get('title', '<untitled>')}"
        )

    def execute(self, parameters: dict[str, str], context: ActionContext) -> dict[str, Any]:
        task_id = self._tasks.create_task(
            tenant_code=context.tenant_code,
            workflow_id=context.workflow_id,
            execution_id=context.execution_id,
            assignee=parameters.get("assignee", ""),
            title=parameters.get("title", "Approval required"),
            description=parameters.get("description", ""),
            due_after_hours=int(parameters.get("due_after_hours", "24")),
        )
        return {"task_id": task_id}


class InvokePipelineRunAction(WorkflowActionHandler):
    """Starts a Step Functions execution; the state machine ARN comes from configuration."""

    kind = ActionKind.INVOKE_PIPELINE_RUN

    def __init__(
        self, region_name: str, state_machine_arn: str, sfn_client: Any | None = None
    ) -> None:
        if not state_machine_arn.startswith("arn:aws:states:"):
            raise ValueError("state_machine_arn must be a Step Functions ARN.")
        self._arn = state_machine_arn
        self._sfn = sfn_client or boto3.client("stepfunctions", region_name=region_name)

    def describe(self, parameters: dict[str, str]) -> str:
        return (
            f"invoke pipeline run for source={parameters.get('source_id', '<unset>')} "
            f"entity={parameters.get('entity_id', '<unset>')}"
        )

    def execute(self, parameters: dict[str, str], context: ActionContext) -> dict[str, Any]:
        payload = {
            "tenant_code": context.tenant_code,
            "source_id": parameters.get("source_id", ""),
            "entity_id": parameters.get("entity_id", ""),
            "connector_params": {},
            "environment": context.environment,
            "is_replay": False,
        }
        response = self._sfn.start_execution(
            stateMachineArn=self._arn,
            # The execution name is the idempotency key, so a retry cannot start two runs.
            name=f"wf-{context.execution_id[:60]}",
            input=json.dumps(payload, separators=(",", ":")),
        )
        return {"execution_arn": response.get("executionArn", "")}


class RunSavedQueryAction(WorkflowActionHandler):
    """Runs a saved semantic query; the only read path a workflow has."""

    kind = ActionKind.RUN_SAVED_QUERY

    def __init__(self, saved_query_runner: Any) -> None:
        self._runner = saved_query_runner

    def describe(self, parameters: dict[str, str]) -> str:
        return f"run saved query {parameters.get('saved_query_id', '<unset>')}"

    def execute(self, parameters: dict[str, str], context: ActionContext) -> dict[str, Any]:
        rows = self._runner.run(
            tenant_code=context.tenant_code,
            query_id=parameters.get("saved_query_id", ""),
        )
        return {"row_count": len(rows)}


class CallOutboundWebhookAction(WorkflowActionHandler):
    """Calls an allowlisted destination with a signed payload."""

    kind = ActionKind.CALL_OUTBOUND_WEBHOOK

    def __init__(
        self,
        allowlist: DestinationAllowlist,
        secret_reader: Any,
        http_session: Any,
    ) -> None:
        self._allowlist = allowlist
        self._secrets = secret_reader
        self._session = http_session

    def describe(self, parameters: dict[str, str]) -> str:
        return f"call outbound webhook destination {parameters.get('destination_id', '<unset>')}"

    def destination(self, parameters: dict[str, str]) -> str:
        return f"webhook:{parameters.get('destination_id', 'unset')}"

    def execute(self, parameters: dict[str, str], context: ActionContext) -> dict[str, Any]:
        destination = self._allowlist.resolve(
            context.tenant_code, parameters.get("destination_id", "")
        )
        body = json.dumps(
            {
                "tenant_code": context.tenant_code,
                "workflow_id": context.workflow_id,
                "execution_id": context.execution_id,
                "correlation_id": context.correlation_id,
                "payload": parameters.get("body", ""),
            },
            separators=(",", ":"),
        )
        secret = self._secrets.get_secret(destination.secret_arn)
        response = self._session.post(
            destination.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Edl-Signature": sign_outbound_payload(secret, body),
            },
            timeout=15,
        )
        status = int(getattr(response, "status_code", 0))
        if status >= 400:
            raise RuntimeError(f"Destination returned {status}.")
        return {"status_code": status}


class GenerateReportAction(WorkflowActionHandler):
    """
    Delegates report generation and distribution to the enterprise-platform (EP-06).

    Enqueued rather than rendered here: report rendering and delivery live on the other side
    of the contract, and duplicating a templating path would be exactly the redundancy the
    reuse clause forbids.
    """

    kind = ActionKind.GENERATE_REPORT

    def __init__(self, region_name: str, queue_url: str, sqs_client: Any | None = None) -> None:
        self._queue_url = queue_url
        self._sqs = sqs_client or boto3.client("sqs", region_name=region_name)

    def describe(self, parameters: dict[str, str]) -> str:
        return (
            f"request report {parameters.get('report_id', '<unset>')} for distribution to "
            f"{parameters.get('recipients', '<unset>')}"
        )

    def destination(self, parameters: dict[str, str]) -> str:
        return "report-distribution"

    def execute(self, parameters: dict[str, str], context: ActionContext) -> dict[str, Any]:
        self._sqs.send_message(
            QueueUrl=self._queue_url,
            MessageBody=json.dumps(
                {
                    "tenant_code": context.tenant_code,
                    "report_id": parameters.get("report_id", ""),
                    "recipients": parameters.get("recipients", ""),
                    "workflow_id": context.workflow_id,
                    "execution_id": context.execution_id,
                    "correlation_id": context.correlation_id,
                },
                separators=(",", ":"),
            ),
        )
        return {"queued": True}


class InvokeConnectorWritebackAction(WorkflowActionHandler):
    """Invokes the write-back stage; separate deployable, so its failures stay isolated."""

    kind = ActionKind.INVOKE_CONNECTOR_WRITEBACK

    def __init__(
        self, region_name: str, function_name: str, lambda_client: Any | None = None
    ) -> None:
        self._function_name = function_name
        self._lambda = lambda_client or boto3.client("lambda", region_name=region_name)

    def describe(self, parameters: dict[str, str]) -> str:
        return (
            f"invoke write-back for source={parameters.get('source_id', '<unset>')} "
            f"entity={parameters.get('entity_id', '<unset>')}"
        )

    def destination(self, parameters: dict[str, str]) -> str:
        return f"writeback:{parameters.get('source_id', 'unset')}"

    def execute(self, parameters: dict[str, str], context: ActionContext) -> dict[str, Any]:
        payload = {
            "tenant_code": context.tenant_code,
            "source_id": parameters.get("source_id", ""),
            "entity_id": parameters.get("entity_id", ""),
            "connection_id": parameters.get("connection_id") or None,
            "run_id": context.execution_id,
            "records": json.loads(parameters.get("records", "[]")),
        }
        response = self._lambda.invoke(
            FunctionName=self._function_name,
            InvocationType="Event",
            Payload=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        )
        return {"status_code": int(response.get("StatusCode", 0))}


def register_default_actions(handlers: list[WorkflowActionHandler]) -> None:
    """Register a wired handler set; idempotent so a warm container can re-import safely."""
    already = set(action_registry.registered_kinds())
    for handler in handlers:
        if handler.kind.value not in already:
            action_registry.register(handler)
