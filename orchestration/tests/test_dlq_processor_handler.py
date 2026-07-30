"""
Tests for orchestration/dlq_processor/dlq_processor_handler.py

Covers:
  - DLQMessage Pydantic validation
  - Audit record write
  - SNS notification
  - Invalid JSON / validation failure handling
  - Auto-replay logic
  - Graceful handling of DynamoDB write failures
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from conftest import RESOURCE_NAME_ENVIRONMENT
from orchestration.dlq_processor.dlq_processor_handler import (
    DLQMessage,
    _process_dlq_record,
    _replay_failed_run,
    _send_sns_notification,
    _write_audit_record,
)


class TestDLQMessageValidation:
    def test_valid_message_parses(self) -> None:
        msg = DLQMessage.model_validate(
            {
                "run_id": "run-20260707-143022-a3f9c1d2",
                "source_id": "salesforce",
                "entity_id": "salesforce-account",
                "environment": "dev",
                "failure_reason": "connector timeout",
                "failure_stage": "extraction",
                "tenant_code": "demo",
            }
        )
        assert msg.source_id == "salesforce"
        assert msg.is_replay is False

    def test_defaults_for_optional_fields(self) -> None:
        msg = DLQMessage.model_validate(
            {
                "run_id": "run-001-abc",
                "source_id": "salesforce",
                "entity_id": "salesforce-account",
                "environment": "dev",
                "tenant_code": "demo",
            }
        )
        assert msg.failure_reason == "unknown"
        assert msg.failure_stage == "unknown"

    def test_invalid_environment_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DLQMessage.model_validate(
                {
                    "run_id": "run-001",
                    "source_id": "salesforce",
                    "entity_id": "salesforce-account",
                    "environment": "PRODUCTION",
                    "tenant_code": "demo",
                }
            )

    def test_invalid_source_id_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DLQMessage.model_validate(
                {
                    "run_id": "run-001",
                    "source_id": "INVALID!",
                    "entity_id": "salesforce-account",
                    "environment": "dev",
                    "tenant_code": "demo",
                }
            )

    def test_missing_tenant_code_is_rejected(self) -> None:
        """ARCH-17: tenant_code has no fail-open default — a DLQ message that
        omits it must fail validation, not be replayed under the demo tenant.
        The prior back-compat rationale (messages predating the fix) is void
        since dev DLQ contents are demo-only."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="tenant_code"):
            DLQMessage.model_validate(
                {
                    "run_id": "run-001",
                    "source_id": "salesforce",
                    "entity_id": "salesforce-account",
                    "environment": "dev",
                }
            )

    def test_extra_fields_allowed(self) -> None:
        """DLQ messages allow extra fields (preserve original payload)."""
        msg = DLQMessage.model_validate(
            {
                "run_id": "run-001-abc",
                "source_id": "salesforce",
                "entity_id": "salesforce-account",
                "environment": "dev",
                "tenant_code": "demo",
                "extra_context": "preserved",
            }
        )
        assert msg.source_id == "salesforce"

    def test_all_environments_valid(self) -> None:
        for env in ("dev", "uat", "prod"):
            msg = DLQMessage.model_validate(
                {
                    "run_id": "run-abc",
                    "source_id": "salesforce",
                    "entity_id": "salesforce-account",
                    "environment": env,
                    "tenant_code": "demo",
                }
            )
            assert msg.environment == env


class TestWriteAuditRecord:
    @mock_aws
    def test_writes_item_to_dynamodb(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName=RESOURCE_NAME_ENVIRONMENT["AUDIT_LOG_TABLE"],
            KeySchema=[
                {"AttributeName": "run_id", "KeyType": "HASH"},
                {"AttributeName": "stage", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "run_id", "AttributeType": "S"},
                {"AttributeName": "stage", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        msg = DLQMessage.model_validate(
            {
                "run_id": "run-test-audit",
                "source_id": "salesforce",
                "entity_id": "salesforce-account",
                "environment": "dev",
                "failure_reason": "timeout",
                "failure_stage": "extraction",
                "tenant_code": "demo",
            }
        )

        with patch("orchestration.dlq_processor.dlq_processor_handler._dynamodb", dynamodb):
            _write_audit_record(
                audit_table_name=RESOURCE_NAME_ENVIRONMENT["AUDIT_LOG_TABLE"],
                msg=msg,
                received_at="2026-07-07T12:00:00+00:00",
            )

        table = dynamodb.Table(RESOURCE_NAME_ENVIRONMENT["AUDIT_LOG_TABLE"])
        item = table.get_item(Key={"run_id": "run-test-audit", "stage": "dlq_received"})
        assert "Item" in item
        assert item["Item"]["source_id"] == "salesforce"
        assert item["Item"]["failure_reason"] == "timeout"
        assert item["Item"]["source_entity_key"] == "demo#salesforce#salesforce-account"

    @mock_aws
    def test_source_entity_key_is_tenant_scoped(self) -> None:
        """ARCH-18 regression: two tenants with the same source/entity must
        get distinct source_entity_key values so the GSI never mixes their
        run history."""
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName=RESOURCE_NAME_ENVIRONMENT["AUDIT_LOG_TABLE"],
            KeySchema=[
                {"AttributeName": "run_id", "KeyType": "HASH"},
                {"AttributeName": "stage", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "run_id", "AttributeType": "S"},
                {"AttributeName": "stage", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table = dynamodb.Table(RESOURCE_NAME_ENVIRONMENT["AUDIT_LOG_TABLE"])

        msg_a = DLQMessage.model_validate(
            {
                "run_id": "run-tenant-a",
                "source_id": "salesforce",
                "entity_id": "salesforce-account",
                "environment": "dev",
                "tenant_code": "acme-corp",
            }
        )
        msg_b = DLQMessage.model_validate(
            {
                "run_id": "run-tenant-b",
                "source_id": "salesforce",
                "entity_id": "salesforce-account",
                "environment": "dev",
                "tenant_code": "globex-eu",
            }
        )

        with patch("orchestration.dlq_processor.dlq_processor_handler._dynamodb", dynamodb):
            _write_audit_record(
                RESOURCE_NAME_ENVIRONMENT["AUDIT_LOG_TABLE"], msg_a, "2026-07-08T00:00:00+00:00"
            )
            _write_audit_record(
                RESOURCE_NAME_ENVIRONMENT["AUDIT_LOG_TABLE"], msg_b, "2026-07-08T00:00:01+00:00"
            )

        item_a = table.get_item(Key={"run_id": "run-tenant-a", "stage": "dlq_received"})["Item"]
        item_b = table.get_item(Key={"run_id": "run-tenant-b", "stage": "dlq_received"})["Item"]
        assert item_a["source_entity_key"] != item_b["source_entity_key"]
        assert item_a["source_entity_key"] == "acme-corp#salesforce#salesforce-account"
        assert item_b["source_entity_key"] == "globex-eu#salesforce#salesforce-account"

    def test_dynamodb_write_failure_is_swallowed(self) -> None:
        """Audit write failure must not prevent SNS notification."""
        mock_dynamodb = MagicMock()
        mock_table = MagicMock()
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}},
            "PutItem",
        )
        mock_dynamodb.Table.return_value = mock_table

        msg = DLQMessage.model_validate(
            {
                "run_id": "run-fail-audit",
                "source_id": "salesforce",
                "entity_id": "salesforce-account",
                "environment": "dev",
                "tenant_code": "demo",
            }
        )

        with patch("orchestration.dlq_processor.dlq_processor_handler._dynamodb", mock_dynamodb):
            _write_audit_record("table", msg, "2026-07-07T12:00:00+00:00")


class TestSendSnsNotification:
    def test_publishes_to_sns(self) -> None:
        mock_sns = MagicMock()
        mock_sns.publish.return_value = {"MessageId": "test-msg-id"}

        msg = DLQMessage.model_validate(
            {
                "run_id": "run-notify-test",
                "source_id": "salesforce",
                "entity_id": "salesforce-account",
                "environment": "dev",
                "failure_reason": "rate limit",
                "tenant_code": "demo",
            }
        )

        with patch("orchestration.dlq_processor.dlq_processor_handler._sns", mock_sns):
            _send_sns_notification(
                topic_arn="arn:aws:sns:us-east-1:123:datalake-platform-alerts-dev",
                msg=msg,
                environment="dev",
            )

        mock_sns.publish.assert_called_once()
        call_kwargs = mock_sns.publish.call_args[1]
        assert "DEV" in call_kwargs["Subject"]
        assert "salesforce" in call_kwargs["Subject"]

        body = json.loads(call_kwargs["Message"])
        assert body["run_id"] == "run-notify-test"
        assert body["source_id"] == "salesforce"
        assert body["failure_reason"] == "rate limit"

    def test_subject_truncated_to_100_chars(self) -> None:
        mock_sns = MagicMock()
        msg = DLQMessage.model_validate(
            {
                "run_id": "run-x",
                "source_id": "a" * 30,
                "entity_id": "b-entity",
                "environment": "dev",
                "tenant_code": "demo",
            }
        )

        with patch("orchestration.dlq_processor.dlq_processor_handler._sns", mock_sns):
            _send_sns_notification("arn:fake", msg, "dev")

        subject = mock_sns.publish.call_args[1]["Subject"]
        assert len(subject) <= 100

    def test_sns_failure_is_swallowed(self) -> None:
        mock_sns = MagicMock()
        mock_sns.publish.side_effect = ClientError(
            {"Error": {"Code": "TopicArn", "Message": "not found"}},
            "Publish",
        )

        msg = DLQMessage.model_validate(
            {
                "run_id": "run-x",
                "source_id": "salesforce",
                "entity_id": "salesforce-account",
                "environment": "dev",
                "tenant_code": "demo",
            }
        )

        with patch("orchestration.dlq_processor.dlq_processor_handler._sns", mock_sns):
            _send_sns_notification("arn:fake", msg, "dev")


class TestReplayFailedRun:
    def test_starts_replay_execution(self) -> None:
        mock_sfn = MagicMock()
        mock_sfn.start_execution.return_value = {"executionArn": "arn:fake"}

        msg = DLQMessage.model_validate(
            {
                "run_id": "run-original-abc",
                "source_id": "salesforce",
                "entity_id": "salesforce-account",
                "environment": "dev",
                "connector_params": {"object_name": "Account"},
                "tenant_code": "demo",
            }
        )

        with patch("orchestration.dlq_processor.dlq_processor_handler._sfn", mock_sfn):
            _replay_failed_run("arn:sfn:test", msg)

        mock_sfn.start_execution.assert_called_once()
        call_kwargs = mock_sfn.start_execution.call_args[1]
        parsed_input = json.loads(call_kwargs["input"])
        assert parsed_input["is_replay"] is True
        assert parsed_input["replay_of_run_id"] == "run-original-abc"

    def test_execution_already_exists_is_noop(self) -> None:
        mock_sfn = MagicMock()
        mock_sfn.start_execution.side_effect = ClientError(
            {"Error": {"Code": "ExecutionAlreadyExists", "Message": "dup"}},
            "StartExecution",
        )

        msg = DLQMessage.model_validate(
            {
                "run_id": "run-dup",
                "source_id": "salesforce",
                "entity_id": "salesforce-account",
                "environment": "dev",
                "tenant_code": "demo",
            }
        )

        with patch("orchestration.dlq_processor.dlq_processor_handler._sfn", mock_sfn):
            _replay_failed_run("arn:sfn:test", msg)

    def test_replay_preserves_tenant_code(self) -> None:
        """Regression: a DLQ-triggered replay must not silently default to
        another tenant — tenant_code must survive into the replay's SFN input."""
        mock_sfn = MagicMock()
        mock_sfn.start_execution.return_value = {"executionArn": "arn:fake"}

        msg = DLQMessage.model_validate(
            {
                "run_id": "run-tenant-preserve",
                "source_id": "salesforce",
                "entity_id": "salesforce-account",
                "environment": "dev",
                "tenant_code": "acme-corp",
            }
        )

        with patch("orchestration.dlq_processor.dlq_processor_handler._sfn", mock_sfn):
            _replay_failed_run("arn:sfn:test", msg)

        parsed_input = json.loads(mock_sfn.start_execution.call_args[1]["input"])
        assert parsed_input["tenant_code"] == "acme-corp"

    def test_replay_execution_name_contains_run_id(self) -> None:
        mock_sfn = MagicMock()
        mock_sfn.start_execution.return_value = {"executionArn": "arn:fake"}

        msg = DLQMessage.model_validate(
            {
                "run_id": "run-abc-def-123",
                "source_id": "salesforce",
                "entity_id": "salesforce-account",
                "environment": "dev",
                "tenant_code": "demo",
            }
        )

        with patch("orchestration.dlq_processor.dlq_processor_handler._sfn", mock_sfn):
            _replay_failed_run("arn:sfn:test", msg)

        exec_name = mock_sfn.start_execution.call_args[1]["name"]
        assert "run-abc-def-123" in exec_name
        assert "replay" in exec_name
        assert len(exec_name) <= 80


class TestProcessDLQRecord:
    def _make_record(self, body: dict[str, Any]) -> dict[str, Any]:
        return {"messageId": "test-dlq-msg", "body": json.dumps(body)}

    def _valid_body(self) -> dict[str, Any]:
        return {
            "run_id": "run-dlq-test-abc",
            "source_id": "salesforce",
            "entity_id": "salesforce-account",
            "environment": "dev",
            "failure_reason": "Lambda timeout",
            "failure_stage": "extraction",
            "tenant_code": "demo",
        }

    def test_invalid_json_raises(self) -> None:
        record = {"messageId": "bad", "body": "not json {{{"}
        with pytest.raises(ValueError, match="invalid JSON"):
            _process_dlq_record(record, "table", "arn:sns", "", "dev", False)

    def test_invalid_message_schema_raises(self) -> None:
        record = self._make_record(
            {"source_id": "INVALID!", "entity_id": "x", "environment": "dev"}
        )
        with pytest.raises(ValueError, match="failed validation"):
            _process_dlq_record(record, "table", "arn:sns", "", "dev", False)

    def test_missing_tenant_code_raises_and_is_not_replayed(self) -> None:
        """ARCH-17: a DLQ message missing tenant_code must fail validation
        (logged as a structured error by _process_dlq_record) rather than
        being audited/replayed under the demo tenant."""
        body = self._valid_body()
        del body["tenant_code"]
        with pytest.raises(ValueError, match="failed validation"):
            _process_dlq_record(self._make_record(body), "table", "arn:sns", "", "dev", False)

    def test_happy_path_audit_and_notify(self) -> None:
        mock_dynamodb = MagicMock()
        mock_table = MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        mock_sns = MagicMock()
        mock_sns.publish.return_value = {"MessageId": "sns-id"}

        with (
            patch("orchestration.dlq_processor.dlq_processor_handler._dynamodb", mock_dynamodb),
            patch("orchestration.dlq_processor.dlq_processor_handler._sns", mock_sns),
        ):
            _process_dlq_record(
                self._make_record(self._valid_body()),
                "audit-table",
                "arn:sns:test",
                "",
                "dev",
                False,
            )

        mock_table.put_item.assert_called_once()
        mock_sns.publish.assert_called_once()

    def test_auto_replay_false_does_not_call_sfn(self) -> None:
        mock_sfn = MagicMock()

        with (
            patch("orchestration.dlq_processor.dlq_processor_handler._dynamodb", MagicMock()),
            patch("orchestration.dlq_processor.dlq_processor_handler._sns", MagicMock()),
            patch("orchestration.dlq_processor.dlq_processor_handler._sfn", mock_sfn),
        ):
            _process_dlq_record(
                self._make_record(self._valid_body()),
                "t",
                "arn",
                "arn:sfn:test",
                "dev",
                False,
            )

        mock_sfn.start_execution.assert_not_called()

    def test_auto_replay_true_calls_sfn(self) -> None:
        mock_sfn = MagicMock()
        mock_sfn.start_execution.return_value = {"executionArn": "arn:fake"}

        with (
            patch("orchestration.dlq_processor.dlq_processor_handler._dynamodb", MagicMock()),
            patch("orchestration.dlq_processor.dlq_processor_handler._sns", MagicMock()),
            patch("orchestration.dlq_processor.dlq_processor_handler._sfn", mock_sfn),
        ):
            _process_dlq_record(
                self._make_record(self._valid_body()),
                "t",
                "arn",
                "arn:sfn:test",
                "dev",
                True,
            )

        mock_sfn.start_execution.assert_called_once()
        call_kwargs = mock_sfn.start_execution.call_args[1]
        parsed = json.loads(call_kwargs["input"])
        assert parsed["is_replay"] is True
