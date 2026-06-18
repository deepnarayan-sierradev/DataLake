"""
Tests for the Run Lifecycle Coordinator (2.5).

Covers:
  - generate_run_id: format, uniqueness, not a sequential integer
  - RunCoordinator.emit_stage: returns PipelineStageContract, persists to DynamoDB
  - RunCoordinator.enqueue_dlq_entry: sends SQS message to DLQ
  - Audit log write failure does not propagate
  - DLQ URL resolution failure is silently logged
"""

from __future__ import annotations

import json
import re

import boto3
from moto import mock_aws

from connector_runtime.run_lifecycle.run_lifecycle import RunCoordinator, generate_run_id
from contracts.observability_contract import PipelineStage, RunStatus
from contracts.pipeline_stage_contract import PipelineStageContract

_REGION = "us-east-1"
_ENV = "dev"
_AUDIT_TABLE = f"{_ENV}-run-audit-log"
_DLQ_NAME = f"{_ENV}-extraction-dlq"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_audit_table(dynamodb: object) -> None:
    import boto3 as _boto3

    ddb = _boto3.resource("dynamodb", region_name=_REGION)
    ddb.create_table(
        TableName=_AUDIT_TABLE,
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


def _create_dlq(sqs: object) -> str:
    import boto3 as _boto3

    client = _boto3.client("sqs", region_name=_REGION)
    response = client.create_queue(QueueName=_DLQ_NAME)
    url: str = response["QueueUrl"]
    return url


# ---------------------------------------------------------------------------
# generate_run_id
# ---------------------------------------------------------------------------


class TestGenerateRunId:
    _RUN_ID_PATTERN = re.compile(r"^run-\d{8}-\d{6}\d{6}-[0-9a-f]{8}$")

    def test_format_matches_expected_pattern(self) -> None:
        run_id = generate_run_id()
        assert self._RUN_ID_PATTERN.match(run_id), f"Unexpected format: {run_id}"

    def test_not_a_sequential_integer(self) -> None:
        run_id = generate_run_id()
        assert not run_id.isdigit(), f"run_id must not be a bare integer: {run_id}"

    def test_two_calls_produce_different_ids(self) -> None:
        ids = {generate_run_id() for _ in range(20)}
        assert len(ids) == 20, "Expected all run_ids to be unique"

    def test_starts_with_run_prefix(self) -> None:
        assert generate_run_id().startswith("run-")


# ---------------------------------------------------------------------------
# RunCoordinator
# ---------------------------------------------------------------------------


class TestRunCoordinator:
    @mock_aws
    def test_run_id_is_immutable_and_correctly_formatted(self) -> None:
        boto3.resource("dynamodb", region_name=_REGION).create_table(
            TableName=_AUDIT_TABLE,
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
        coord = RunCoordinator(
            environment=_ENV,
            region_name=_REGION,
            source_id="salesforce",
            entity_id="salesforce-account",
        )
        run_id = coord.run_id
        assert run_id.startswith("run-")
        assert not run_id.isdigit()

    @mock_aws
    def test_emit_stage_returns_pipeline_stage_contract(self) -> None:
        boto3.resource("dynamodb", region_name=_REGION).create_table(
            TableName=_AUDIT_TABLE,
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
        coord = RunCoordinator(
            environment=_ENV,
            region_name=_REGION,
            source_id="salesforce",
            entity_id="salesforce-account",
        )
        contract = coord.emit_stage(
            stage=PipelineStage.CONFIGURATION_LOAD,
            status=RunStatus.SUCCESS,
            duration_ms=45,
        )
        assert isinstance(contract, PipelineStageContract)
        assert contract.stage == PipelineStage.CONFIGURATION_LOAD
        assert contract.status == RunStatus.SUCCESS
        assert contract.run_id == coord.run_id

    @mock_aws
    def test_emit_stage_persists_to_dynamodb(self) -> None:
        ddb = boto3.resource("dynamodb", region_name=_REGION)
        ddb.create_table(
            TableName=_AUDIT_TABLE,
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
        coord = RunCoordinator(
            environment=_ENV,
            region_name=_REGION,
            source_id="salesforce",
            entity_id="salesforce-account",
        )
        coord.emit_stage(
            stage=PipelineStage.EXTRACTION,
            status=RunStatus.SUCCESS,
            duration_ms=2000,
        )
        table = ddb.Table(_AUDIT_TABLE)
        response = table.get_item(
            Key={"run_id": coord.run_id, "stage": "extraction"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        assert item is not None
        assert item["status"] == "success"

    @mock_aws
    def test_enqueue_dlq_entry_sends_sqs_message(self) -> None:
        # Create audit table (required by coordinator construction)
        boto3.resource("dynamodb", region_name=_REGION).create_table(
            TableName=_AUDIT_TABLE,
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
        sqs = boto3.client("sqs", region_name=_REGION)
        queue_url = sqs.create_queue(QueueName=_DLQ_NAME)["QueueUrl"]

        coord = RunCoordinator(
            environment=_ENV,
            region_name=_REGION,
            source_id="salesforce",
            entity_id="salesforce-account",
        )
        coord.enqueue_dlq_entry(
            error_message="Extraction failed after retries",
            error_code="deterministic_invalid_credentials",
            failed_stage=PipelineStage.CREDENTIAL_RETRIEVAL,
        )

        messages = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1)
        assert "Messages" in messages
        body = json.loads(messages["Messages"][0]["Body"])
        assert body["run_id"] == coord.run_id
        assert body["error_code"] == "deterministic_invalid_credentials"
        assert body["failed_stage"] == "credential_retrieval"

    @mock_aws
    def test_audit_write_failure_does_not_propagate(self) -> None:
        """If the audit table does not exist, emit_stage must not raise."""
        # Intentionally do NOT create the audit table
        coord = RunCoordinator(
            environment=_ENV,
            region_name=_REGION,
            source_id="salesforce",
            entity_id="salesforce-account",
        )
        # Should not raise even though the table doesn't exist
        contract = coord.emit_stage(
            stage=PipelineStage.CONFIGURATION_LOAD,
            status=RunStatus.FAILED,
        )
        assert contract.status == RunStatus.FAILED

    @mock_aws
    def test_dlq_resolution_failure_is_silent(self) -> None:
        """If the DLQ queue doesn't exist, enqueue_dlq_entry must not raise."""
        boto3.resource("dynamodb", region_name=_REGION).create_table(
            TableName=_AUDIT_TABLE,
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
        coord = RunCoordinator(
            environment=_ENV,
            region_name=_REGION,
            source_id="salesforce",
            entity_id="salesforce-account",
        )
        # Should not raise even though the DLQ doesn't exist
        coord.enqueue_dlq_entry(
            error_message="Something went wrong",
            error_code="unknown",
            failed_stage=PipelineStage.EXTRACTION,
        )


# ---------------------------------------------------------------------------
# Regression tests for fixed bugs
# ---------------------------------------------------------------------------


class TestDlqScrubbing:
    """
    Regression test for Bug #2: DLQ payload error_message was not scrubbed.

    enqueue_dlq_entry() builds its own payload dict that bypasses
    PipelineStageContract validators.  scrub_sensitive_values() must be applied
    explicitly before the message is sent.
    """

    @mock_aws
    def test_dlq_message_error_message_is_scrubbed(self) -> None:
        boto3.resource("dynamodb", region_name=_REGION).create_table(
            TableName=_AUDIT_TABLE,
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
        sqs = boto3.client("sqs", region_name=_REGION)
        queue_url = sqs.create_queue(QueueName=_DLQ_NAME)["QueueUrl"]

        coord = RunCoordinator(
            environment=_ENV,
            region_name=_REGION,
            source_id="salesforce",
            entity_id="salesforce-account",
        )
        # Pass a message containing a sensitive pattern
        coord.enqueue_dlq_entry(
            error_message="Auth failed: token=sup3rs3cr3t expired",
            error_code="deterministic_invalid_credentials",
            failed_stage=PipelineStage.CREDENTIAL_RETRIEVAL,
        )

        messages = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1)
        body = json.loads(messages["Messages"][0]["Body"])
        # The raw secret value must not appear in the SQS message body.
        assert "sup3rs3cr3t" not in body["error_message"]
        # The message is present but scrubbed
        assert body["error_message"] != ""

    @mock_aws
    def test_dlq_url_is_cached_after_first_resolution(self) -> None:
        """DLQ URL should be resolved only once per RunCoordinator instance."""
        boto3.resource("dynamodb", region_name=_REGION).create_table(
            TableName=_AUDIT_TABLE,
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
        sqs = boto3.client("sqs", region_name=_REGION)
        sqs.create_queue(QueueName=_DLQ_NAME)

        coord = RunCoordinator(
            environment=_ENV,
            region_name=_REGION,
            source_id="salesforce",
            entity_id="salesforce-account",
        )
        # First enqueue — resolves and caches the URL
        coord.enqueue_dlq_entry("error one", "unknown", PipelineStage.EXTRACTION)
        first_cached = coord._dlq_url
        # Second enqueue — should use cached URL
        coord.enqueue_dlq_entry("error two", "unknown", PipelineStage.RAW_WRITE)
        assert coord._dlq_url is first_cached  # same string object (cached)


# ---------------------------------------------------------------------------
# Properties: source_id, entity_id, started_at
# ---------------------------------------------------------------------------


@mock_aws
class TestRunCoordinatorProperties:
    def _make_coord(self) -> RunCoordinator:
        ddb = boto3.resource("dynamodb", region_name=_REGION)
        ddb.create_table(
            TableName=_AUDIT_TABLE,
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
        return RunCoordinator(
            environment=_ENV,
            region_name=_REGION,
            source_id="salesforce",
            entity_id="salesforce-account",
        )

    def test_source_id_property(self) -> None:
        coord = self._make_coord()
        assert coord.source_id == "salesforce"

    def test_entity_id_property(self) -> None:
        coord = self._make_coord()
        assert coord.entity_id == "salesforce-account"

    def test_started_at_is_recent_utc(self) -> None:
        from datetime import UTC, datetime
        coord = self._make_coord()
        now = datetime.now(tz=UTC)
        assert abs((coord.started_at - now).total_seconds()) < 5

    def test_empty_environment_raises(self) -> None:
        import pytest
        with pytest.raises(ValueError, match="environment must not be empty"):
            RunCoordinator(
                environment="",
                region_name=_REGION,
                source_id="salesforce",
                entity_id="salesforce-account",
            )

    def test_dlq_send_failure_is_logged_not_raised(self) -> None:
        """SQS send failure inside enqueue_dlq_entry must not propagate."""
        from unittest.mock import MagicMock

        from botocore.exceptions import ClientError

        coord = self._make_coord()
        coord._sqs.send_message = MagicMock(  # type: ignore[attr-defined]
            side_effect=ClientError(
                {"Error": {"Code": "QueueDoesNotExist", "Message": ""}},
                "SendMessage",
            )
        )
        # Should not raise; DLQ failure is logged and swallowed
        coord.enqueue_dlq_entry("some error", "unknown", PipelineStage.EXTRACTION)
