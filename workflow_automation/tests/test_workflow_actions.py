"""
Tests for the registered workflow action handlers (DL-WF-04, DL-WF-10).

The security properties matter more than the happy paths here: outbound destinations come from
a per-tenant allowlist rather than the payload, payloads are signed, and the pipeline-run action
uses the execution id as its idempotency key so a retry cannot start two runs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import boto3
import pytest
from moto import mock_aws

from conftest import RESOURCE_NAME_ENVIRONMENT
from workflow_automation.action_registry import ActionContext, action_registry
from workflow_automation.actions import (
    CallOutboundWebhookAction,
    CreateApprovalTaskAction,
    DestinationAllowlist,
    DestinationNotAllowedError,
    GenerateReportAction,
    InvokeConnectorWritebackAction,
    InvokePipelineRunAction,
    OutboundDestination,
    RunSavedQueryAction,
    SendNotificationAction,
    WriteExceptionAction,
    register_default_actions,
    sign_outbound_payload,
)
from workflow_automation.definition import ActionKind

_REGION = "us-east-1"
_CONTEXT = ActionContext(
    tenant_code="demo",
    workflow_id="wf-1",
    execution_id="exec-1",
    correlation_id="run-1",
    environment="dev",
    condition_values={"open_exceptions": 12.0},
)


class _RecordingClient:
    """Captures the single AWS call each handler makes."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response or {}

    def _record(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._response

    publish = _record
    start_execution = _record
    send_message = _record
    invoke = _record


class _RecordingResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _RecordingSession:
    def __init__(self, status_code: int = 200) -> None:
        self.posts: list[dict[str, Any]] = []
        self._status_code = status_code

    def post(self, url: str, **kwargs: Any) -> _RecordingResponse:
        self.posts.append({"url": url, **kwargs})
        return _RecordingResponse(self._status_code)


class _StaticSecretReader:
    def __init__(self, secret: str = "shhh") -> None:
        self._secret = secret
        self.requested: list[str] = []

    def get_secret(self, secret_arn: str) -> str:
        self.requested.append(secret_arn)
        return self._secret


def _create_destination_table() -> None:
    boto3.client("dynamodb", region_name=_REGION).create_table(
        TableName=RESOURCE_NAME_ENVIRONMENT["WORKFLOW_DESTINATION_TABLE"],
        KeySchema=[
            {"AttributeName": "tenant_code", "KeyType": "HASH"},
            {"AttributeName": "destination_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "tenant_code", "AttributeType": "S"},
            {"AttributeName": "destination_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


class TestOutboundDestination:
    def test_https_destinations_are_accepted(self) -> None:
        destination = OutboundDestination(
            destination_id="crm", url="https://crm.example.com/hook", secret_arn="arn:secret"
        )
        assert destination.hostname == "crm.example.com"

    def test_plain_http_is_refused(self) -> None:
        with pytest.raises(ValueError, match="absolute https"):
            OutboundDestination(
                destination_id="crm", url="http://crm.example.com/hook", secret_arn="arn:secret"
            )

    def test_a_relative_url_is_refused(self) -> None:
        with pytest.raises(ValueError, match="absolute https"):
            OutboundDestination(destination_id="crm", url="/hook", secret_arn="arn:secret")

    def test_a_file_url_is_refused(self) -> None:
        with pytest.raises(ValueError, match="absolute https"):
            OutboundDestination(
                destination_id="crm", url="file:///etc/passwd", secret_arn="arn:secret"
            )


class TestSignature:
    def test_signature_is_hmac_sha256_of_the_body(self) -> None:
        expected = hmac.new(b"secret", b"body", hashlib.sha256).hexdigest()
        assert sign_outbound_payload("secret", "body") == expected

    def test_a_different_secret_produces_a_different_signature(self) -> None:
        assert sign_outbound_payload("a", "body") != sign_outbound_payload("b", "body")


@mock_aws
class TestDestinationAllowlist:
    def test_a_registered_destination_resolves(self) -> None:
        _create_destination_table()
        allowlist = DestinationAllowlist(region_name=_REGION)
        allowlist.register(
            "demo",
            OutboundDestination(
                destination_id="crm", url="https://crm.example.com/hook", secret_arn="arn:secret"
            ),
        )
        assert allowlist.resolve("demo", "crm").url == "https://crm.example.com/hook"

    def test_an_unregistered_destination_is_refused(self) -> None:
        _create_destination_table()
        allowlist = DestinationAllowlist(region_name=_REGION)
        with pytest.raises(DestinationNotAllowedError):
            allowlist.resolve("demo", "attacker-controlled")

    def test_one_tenants_destination_is_not_visible_to_another(self) -> None:
        _create_destination_table()
        allowlist = DestinationAllowlist(region_name=_REGION)
        allowlist.register(
            "demo",
            OutboundDestination(
                destination_id="crm", url="https://crm.example.com/hook", secret_arn="arn:secret"
            ),
        )
        with pytest.raises(DestinationNotAllowedError):
            allowlist.resolve("acme", "crm")


class TestSendNotificationAction:
    def test_a_non_sns_arn_is_refused(self) -> None:
        action = SendNotificationAction(region_name=_REGION, sns_client=_RecordingClient())
        with pytest.raises(ValueError, match="SNS topic ARN"):
            action.execute({"topic_arn": "https://evil.example.com"}, _CONTEXT)

    def test_an_empty_arn_is_refused(self) -> None:
        action = SendNotificationAction(region_name=_REGION, sns_client=_RecordingClient())
        with pytest.raises(ValueError):
            action.execute({}, _CONTEXT)

    def test_publish_carries_the_execution_context_and_condition_values(self) -> None:
        client = _RecordingClient({"MessageId": "m-1"})
        action = SendNotificationAction(region_name=_REGION, sns_client=client)
        result = action.execute(
            {"topic_arn": "arn:aws:sns:us-east-1:1:alerts", "subject": "Hi", "body": "there"},
            _CONTEXT,
        )
        assert result == {"message_id": "m-1"}
        message = json.loads(client.calls[0]["Message"])
        assert message["tenant_code"] == "demo"
        assert message["execution_id"] == "exec-1"
        assert message["condition_values"] == {"open_exceptions": 12.0}

    def test_subject_is_truncated_to_the_sns_limit(self) -> None:
        client = _RecordingClient()
        action = SendNotificationAction(region_name=_REGION, sns_client=client)
        action.execute(
            {"topic_arn": "arn:aws:sns:us-east-1:1:alerts", "subject": "x" * 300}, _CONTEXT
        )
        assert len(client.calls[0]["Subject"]) == 100

    def test_describe_names_the_topic_for_a_dry_run(self) -> None:
        action = SendNotificationAction(region_name=_REGION, sns_client=_RecordingClient())
        described = action.describe({"topic_arn": "arn:aws:sns:us-east-1:1:alerts"})
        assert "arn:aws:sns:us-east-1:1:alerts" in described

    def test_destination_key_is_per_topic(self) -> None:
        action = SendNotificationAction(region_name=_REGION, sns_client=_RecordingClient())
        assert action.destination({"topic_arn": "arn:a"}) != action.destination(
            {"topic_arn": "arn:b"}
        )


class TestWriteExceptionAction:
    class _RecordingRepository:
        def __init__(self) -> None:
            self.recorded: list[Any] = []

        def record(self, exception: Any) -> str:
            self.recorded.append(exception)
            return "demo#rule#1"

    def test_the_exception_is_written_to_the_shared_dl_02_store(self) -> None:
        repository = self._RecordingRepository()
        action = WriteExceptionAction(repository=repository)
        result = action.execute(
            {"rule_id": "amount_positive", "entity_id": "ar_invoice", "severity": "error"},
            _CONTEXT,
        )
        assert result == {"exception_key": "demo#rule#1"}
        written = repository.recorded[0]
        assert written.tenant_code == "demo"
        assert written.rule_id == "amount_positive"
        assert written.severity.value == "error"

    def test_severity_defaults_to_warn(self) -> None:
        repository = self._RecordingRepository()
        WriteExceptionAction(repository=repository).execute({"rule_id": "r"}, _CONTEXT)
        assert repository.recorded[0].severity.value == "warn"

    def test_an_unknown_severity_is_refused(self) -> None:
        repository = self._RecordingRepository()
        with pytest.raises(ValueError):
            WriteExceptionAction(repository=repository).execute(
                {"rule_id": "r", "severity": "catastrophic"}, _CONTEXT
            )

    def test_describe_names_the_rule(self) -> None:
        action = WriteExceptionAction(repository=self._RecordingRepository())
        assert "amount_positive" in action.describe({"rule_id": "amount_positive"})


class TestCreateApprovalTaskAction:
    class _RecordingTasks:
        def __init__(self) -> None:
            self.created: list[dict[str, Any]] = []

        def create_task(self, **kwargs: Any) -> str:
            self.created.append(kwargs)
            return "task-1"

    def test_the_task_carries_the_execution_it_came_from(self) -> None:
        tasks = self._RecordingTasks()
        action = CreateApprovalTaskAction(task_repository=tasks)
        assert action.execute({"assignee": "controller", "title": "Approve"}, _CONTEXT) == {
            "task_id": "task-1"
        }
        assert tasks.created[0]["execution_id"] == "exec-1"
        assert tasks.created[0]["assignee"] == "controller"

    def test_due_after_hours_defaults_to_one_day(self) -> None:
        tasks = self._RecordingTasks()
        CreateApprovalTaskAction(task_repository=tasks).execute({}, _CONTEXT)
        assert tasks.created[0]["due_after_hours"] == 24

    def test_describe_names_the_assignee(self) -> None:
        action = CreateApprovalTaskAction(task_repository=self._RecordingTasks())
        assert "controller" in action.describe({"assignee": "controller"})


class TestInvokePipelineRunAction:
    def test_a_non_step_functions_arn_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="Step Functions ARN"):
            InvokePipelineRunAction(
                region_name=_REGION, state_machine_arn="arn:aws:lambda:us-east-1:1:function:x"
            )

    def test_the_execution_name_is_the_idempotency_key(self) -> None:
        client = _RecordingClient({"executionArn": "arn:exec"})
        action = InvokePipelineRunAction(
            region_name=_REGION,
            state_machine_arn="arn:aws:states:us-east-1:1:stateMachine:datalake-workflow",
            sfn_client=client,
        )
        action.execute({"source_id": "salesforce", "entity_id": "account"}, _CONTEXT)
        assert client.calls[0]["name"] == "wf-exec-1"

    def test_the_execution_name_stays_within_the_step_functions_limit(self) -> None:
        client = _RecordingClient()
        action = InvokePipelineRunAction(
            region_name=_REGION,
            state_machine_arn="arn:aws:states:us-east-1:1:stateMachine:datalake-workflow",
            sfn_client=client,
        )
        long_context = ActionContext(
            tenant_code="demo",
            workflow_id="wf-1",
            execution_id="e" * 200,
            correlation_id="run-1",
            environment="dev",
        )
        action.execute({}, long_context)
        assert len(client.calls[0]["name"]) <= 80

    def test_the_run_input_is_tenant_scoped(self) -> None:
        client = _RecordingClient()
        action = InvokePipelineRunAction(
            region_name=_REGION,
            state_machine_arn="arn:aws:states:us-east-1:1:stateMachine:datalake-workflow",
            sfn_client=client,
        )
        action.execute({"source_id": "salesforce", "entity_id": "account"}, _CONTEXT)
        payload = json.loads(client.calls[0]["input"])
        assert payload["tenant_code"] == "demo"
        assert payload["is_replay"] is False

    def test_describe_names_the_target_entity(self) -> None:
        action = InvokePipelineRunAction(
            region_name=_REGION,
            state_machine_arn="arn:aws:states:us-east-1:1:stateMachine:datalake-workflow",
            sfn_client=_RecordingClient(),
        )
        assert "account" in action.describe({"entity_id": "account"})


class TestRunSavedQueryAction:
    class _Runner:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self.rows = rows
            self.calls: list[dict[str, str]] = []

        def run(self, tenant_code: str, query_id: str) -> list[dict[str, Any]]:
            self.calls.append({"tenant_code": tenant_code, "query_id": query_id})
            return self.rows

    def test_the_row_count_is_returned_and_the_tenant_is_passed_through(self) -> None:
        runner = self._Runner([{"a": 1}, {"a": 2}])
        action = RunSavedQueryAction(saved_query_runner=runner)
        assert action.execute({"saved_query_id": "q-1"}, _CONTEXT) == {"row_count": 2}
        assert runner.calls[0] == {"tenant_code": "demo", "query_id": "q-1"}

    def test_describe_names_the_query(self) -> None:
        action = RunSavedQueryAction(saved_query_runner=self._Runner([]))
        assert "q-1" in action.describe({"saved_query_id": "q-1"})


@mock_aws
class TestCallOutboundWebhookAction:
    def _action(
        self, *, status_code: int = 200
    ) -> tuple[CallOutboundWebhookAction, _RecordingSession, _StaticSecretReader]:
        _create_destination_table()
        allowlist = DestinationAllowlist(region_name=_REGION)
        allowlist.register(
            "demo",
            OutboundDestination(
                destination_id="crm", url="https://crm.example.com/hook", secret_arn="arn:secret"
            ),
        )
        session = _RecordingSession(status_code=status_code)
        secrets = _StaticSecretReader()
        return (
            CallOutboundWebhookAction(
                allowlist=allowlist, secret_reader=secrets, http_session=session
            ),
            session,
            secrets,
        )

    def test_only_the_allowlisted_url_is_called(self) -> None:
        action, session, _ = self._action()
        action.execute({"destination_id": "crm", "body": "payload"}, _CONTEXT)
        assert session.posts[0]["url"] == "https://crm.example.com/hook"

    def test_a_payload_supplied_url_is_never_called(self) -> None:
        action, session, _ = self._action()
        with pytest.raises(DestinationNotAllowedError):
            action.execute({"destination_id": "https://evil.example.com"}, _CONTEXT)
        assert session.posts == []

    def test_the_body_is_signed_with_the_destination_secret(self) -> None:
        action, session, secrets = self._action()
        action.execute({"destination_id": "crm", "body": "payload"}, _CONTEXT)
        sent = session.posts[0]
        assert sent["headers"]["X-datalake-Signature"] == sign_outbound_payload(
            "shhh", sent["data"]
        )
        assert secrets.requested == ["arn:secret"]

    def test_the_secret_never_appears_in_the_request(self) -> None:
        action, session, _ = self._action()
        action.execute({"destination_id": "crm", "body": "payload"}, _CONTEXT)
        sent = session.posts[0]
        assert "shhh" not in sent["data"]
        assert "shhh" not in json.dumps(sent["headers"])

    def test_a_timeout_is_always_set(self) -> None:
        action, session, _ = self._action()
        action.execute({"destination_id": "crm"}, _CONTEXT)
        assert session.posts[0]["timeout"] == 15

    def test_a_4xx_or_5xx_response_fails_the_action(self) -> None:
        action, _, _ = self._action(status_code=503)
        with pytest.raises(RuntimeError, match="503"):
            action.execute({"destination_id": "crm"}, _CONTEXT)

    def test_a_2xx_response_reports_the_status(self) -> None:
        action, _, _ = self._action(status_code=204)
        assert action.execute({"destination_id": "crm"}, _CONTEXT) == {"status_code": 204}

    def test_destination_key_is_per_destination_id(self) -> None:
        action, _, _ = self._action()
        assert action.destination({"destination_id": "crm"}) == "webhook:crm"

    def test_describe_names_the_destination(self) -> None:
        action, _, _ = self._action()
        assert "crm" in action.describe({"destination_id": "crm"})


class TestGenerateReportAction:
    def test_report_generation_is_enqueued_for_the_enterprise_platform(self) -> None:
        client = _RecordingClient()
        action = GenerateReportAction(
            region_name=_REGION, queue_url="https://sqs/reports", sqs_client=client
        )
        assert action.execute({"report_id": "r-1", "recipients": "a@b.c"}, _CONTEXT) == {
            "queued": True
        }
        body = json.loads(client.calls[0]["MessageBody"])
        assert body["report_id"] == "r-1"
        assert body["tenant_code"] == "demo"
        assert body["correlation_id"] == "run-1"

    def test_all_report_actions_share_one_circuit(self) -> None:
        action = GenerateReportAction(
            region_name=_REGION, queue_url="https://sqs/reports", sqs_client=_RecordingClient()
        )
        assert action.destination({"report_id": "r-1"}) == "report-distribution"

    def test_describe_names_the_recipients(self) -> None:
        action = GenerateReportAction(
            region_name=_REGION, queue_url="https://sqs/reports", sqs_client=_RecordingClient()
        )
        assert "a@b.c" in action.describe({"report_id": "r-1", "recipients": "a@b.c"})


class TestInvokeConnectorWritebackAction:
    def test_the_writeback_payload_carries_tenant_connection_and_records(self) -> None:
        client = _RecordingClient({"StatusCode": 202})
        action = InvokeConnectorWritebackAction(
            region_name=_REGION, function_name="datalake-writeback", lambda_client=client
        )
        result = action.execute(
            {
                "source_id": "salesforce",
                "entity_id": "account",
                "connection_id": "sf-west",
                "records": json.dumps([{"Id": "1"}]),
            },
            _CONTEXT,
        )
        assert result == {"status_code": 202}
        payload = json.loads(client.calls[0]["Payload"].decode("utf-8"))
        assert payload["tenant_code"] == "demo"
        assert payload["connection_id"] == "sf-west"
        assert payload["records"] == [{"Id": "1"}]

    def test_a_blank_connection_id_becomes_none_not_an_empty_string(self) -> None:
        client = _RecordingClient()
        action = InvokeConnectorWritebackAction(
            region_name=_REGION, function_name="datalake-writeback", lambda_client=client
        )
        action.execute({"connection_id": ""}, _CONTEXT)
        payload = json.loads(client.calls[0]["Payload"].decode("utf-8"))
        assert payload["connection_id"] is None

    def test_malformed_records_json_is_rejected(self) -> None:
        action = InvokeConnectorWritebackAction(
            region_name=_REGION,
            function_name="datalake-writeback",
            lambda_client=_RecordingClient(),
        )
        with pytest.raises(json.JSONDecodeError):
            action.execute({"records": "not json"}, _CONTEXT)

    def test_the_invocation_is_asynchronous_so_a_slow_target_cannot_stall_the_engine(self) -> None:
        client = _RecordingClient()
        action = InvokeConnectorWritebackAction(
            region_name=_REGION, function_name="datalake-writeback", lambda_client=client
        )
        action.execute({}, _CONTEXT)
        assert client.calls[0]["InvocationType"] == "Event"

    def test_describe_names_the_source(self) -> None:
        action = InvokeConnectorWritebackAction(
            region_name=_REGION,
            function_name="datalake-writeback",
            lambda_client=_RecordingClient(),
        )
        assert "salesforce" in action.describe({"source_id": "salesforce"})


class TestRegisterDefaultActions:
    def setup_method(self, method: object = None) -> None:
        action_registry.reset()

    def teardown_method(self, method: object = None) -> None:
        action_registry.reset()

    def test_handlers_are_registered_under_their_declared_kind(self) -> None:
        register_default_actions(
            [SendNotificationAction(region_name=_REGION, sns_client=_RecordingClient())]
        )
        assert action_registry.registered_kinds() == [ActionKind.SEND_NOTIFICATION.value]

    def test_registration_is_idempotent_for_a_warm_container(self) -> None:
        handler = SendNotificationAction(region_name=_REGION, sns_client=_RecordingClient())
        register_default_actions([handler])
        register_default_actions([handler])
        assert action_registry.registered_kinds() == [ActionKind.SEND_NOTIFICATION.value]
