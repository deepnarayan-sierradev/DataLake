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

from connector_runtime.run_lifecycle.run_lifecycle import (
    RunCoordinator,
    generate_run_id,
    make_partial_run_id,
)
from contracts.observability_contract import PipelineStage, RunStatus
from contracts.pipeline_stage_contract import PipelineStageContract

_REGION = "us-east-1"
_ENV = "dev"
_AUDIT_TABLE = "EdlRunAuditLog"
_DLQ_NAME = "EdlExtractionFailureDlq"


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
# source_entity_key / started_at GSI population (ARCH-18)
# ---------------------------------------------------------------------------


@mock_aws
class TestAuditRecordGsiFields:
    """
    Regression coverage for ARCH-18: before this fix, emit_stage() /
    emit_checkpoint_stage() never populated source_entity_key or started_at,
    so the source-entity-time-index GSI was only ever fed by
    dlq_processor_handler's failure-path writes — a query for a source/
    entity's run history silently missed every successful run.
    """

    def _make_coord(self, tenant_code: str = "demo", create_table: bool = True) -> RunCoordinator:
        if create_table:
            _create_audit_table(None)
        return RunCoordinator(
            environment=_ENV,
            region_name=_REGION,
            source_id="salesforce",
            entity_id="salesforce-account",
            tenant_code=tenant_code,
        )

    def test_emit_stage_populates_tenant_scoped_source_entity_key(self) -> None:
        coord = self._make_coord(tenant_code="acme-corp")
        coord.emit_stage(stage=PipelineStage.EXTRACTION, status=RunStatus.SUCCESS)

        ddb = boto3.resource("dynamodb", region_name=_REGION)
        item = ddb.Table(_AUDIT_TABLE).get_item(
            Key={"run_id": coord.run_id, "stage": "extraction"},
            ConsistentRead=True,
        )["Item"]
        # Must match dlq_processor_handler's "{tenant_code}#{source_id}#{entity_id}"
        # format exactly so both write paths land in the same GSI partition.
        assert item["source_entity_key"] == "acme-corp#salesforce#salesforce-account"

    def test_emit_stage_populates_started_at(self) -> None:
        coord = self._make_coord()
        coord.emit_stage(stage=PipelineStage.EXTRACTION, status=RunStatus.SUCCESS)

        ddb = boto3.resource("dynamodb", region_name=_REGION)
        item = ddb.Table(_AUDIT_TABLE).get_item(
            Key={"run_id": coord.run_id, "stage": "extraction"},
            ConsistentRead=True,
        )["Item"]
        assert item["started_at"] == coord.started_at.isoformat()

    def test_two_tenants_get_distinct_source_entity_keys(self) -> None:
        """Same source/entity, different tenants — GSI partitions must not mix."""
        coord_a = self._make_coord(tenant_code="acme-corp")
        coord_a.emit_stage(stage=PipelineStage.EXTRACTION, status=RunStatus.SUCCESS)
        coord_b = self._make_coord(tenant_code="globex-eu", create_table=False)
        coord_b.emit_stage(stage=PipelineStage.EXTRACTION, status=RunStatus.SUCCESS)

        ddb = boto3.resource("dynamodb", region_name=_REGION)
        table = ddb.Table(_AUDIT_TABLE)
        item_a = table.get_item(
            Key={"run_id": coord_a.run_id, "stage": "extraction"}, ConsistentRead=True
        )["Item"]
        item_b = table.get_item(
            Key={"run_id": coord_b.run_id, "stage": "extraction"}, ConsistentRead=True
        )["Item"]
        assert item_a["source_entity_key"] != item_b["source_entity_key"]

    def test_checkpoint_stage_also_populates_gsi_fields(self) -> None:
        coord = self._make_coord(tenant_code="acme-corp")
        coord.emit_checkpoint_stage(part_number=1, record_count=10)

        ddb = boto3.resource("dynamodb", region_name=_REGION)
        item = ddb.Table(_AUDIT_TABLE).get_item(
            Key={"run_id": make_partial_run_id(coord.run_id, 1), "stage": "run_completion"},
            ConsistentRead=True,
        )["Item"]
        assert item["source_entity_key"] == "acme-corp#salesforce#salesforce-account"
        assert item["started_at"] == coord.started_at.isoformat()


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


# ---------------------------------------------------------------------------
# make_partial_run_id (PERF-5)
# ---------------------------------------------------------------------------


class TestMakePartialRunId:
    def test_appends_part_suffix(self) -> None:
        assert make_partial_run_id("run-20260611-143022123456-a3f9c1d2", 1) == (
            "run-20260611-143022123456-a3f9c1d2-part1"
        )

    def test_different_part_numbers_produce_different_ids(self) -> None:
        base = generate_run_id()
        assert make_partial_run_id(base, 1) != make_partial_run_id(base, 2)

    def test_zero_part_number_rejected(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="part_number"):
            make_partial_run_id("run-x", 0)

    def test_negative_part_number_rejected(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="part_number"):
            make_partial_run_id("run-x", -1)


# ---------------------------------------------------------------------------
# RunCoordinator.emit_checkpoint_stage (PERF-5)
# ---------------------------------------------------------------------------


@mock_aws
class TestEmitCheckpointStage:
    def _make_coord(self) -> RunCoordinator:
        _create_audit_table(None)
        return RunCoordinator(
            environment=_ENV,
            region_name=_REGION,
            source_id="mysql-rds",
            entity_id="mysql-rds-orders",
        )

    def test_returns_contract_with_partial_run_id(self) -> None:
        coord = self._make_coord()
        contract = coord.emit_checkpoint_stage(part_number=1, record_count=5_000)
        assert contract.run_id == make_partial_run_id(coord.run_id, 1)
        assert contract.run_id != coord.run_id

    def test_status_is_partial(self) -> None:
        coord = self._make_coord()
        contract = coord.emit_checkpoint_stage(part_number=1, record_count=5_000)
        assert contract.status == RunStatus.PARTIAL

    def test_stage_is_run_completion(self) -> None:
        coord = self._make_coord()
        contract = coord.emit_checkpoint_stage(part_number=1, record_count=5_000)
        assert contract.stage == PipelineStage.RUN_COMPLETION

    def test_persists_under_distinct_dynamodb_key(self) -> None:
        coord = self._make_coord()
        coord.emit_checkpoint_stage(part_number=2, record_count=1_234)

        ddb = boto3.resource("dynamodb", region_name=_REGION)
        table = ddb.Table(_AUDIT_TABLE)

        partial_item = table.get_item(
            Key={"run_id": make_partial_run_id(coord.run_id, 2), "stage": "run_completion"},
            ConsistentRead=True,
        ).get("Item")
        assert partial_item is not None
        assert partial_item["record_count"] == 1_234
        assert partial_item["status"] == "partial"

        # The MAIN run_id's own audit trail is untouched by the checkpoint —
        # no item exists under the plain run_id for this stage.
        main_item = table.get_item(
            Key={"run_id": coord.run_id, "stage": "run_completion"},
            ConsistentRead=True,
        ).get("Item")
        assert main_item is None

    def test_does_not_raise_when_audit_table_missing(self) -> None:
        """Same best-effort semantics as emit_stage — never propagates."""
        coord = RunCoordinator(
            environment=_ENV,
            region_name=_REGION,
            source_id="mysql-rds",
            entity_id="mysql-rds-orders",
        )
        # Intentionally do NOT create the audit table.
        contract = coord.emit_checkpoint_stage(part_number=1, record_count=100)
        assert contract.status == RunStatus.PARTIAL

    def test_carries_error_message_and_extraction_window_end(self) -> None:
        from datetime import UTC, datetime

        coord = self._make_coord()
        window_end = datetime(2026, 6, 11, 11, tzinfo=UTC)
        contract = coord.emit_checkpoint_stage(
            part_number=1,
            record_count=2,
            extraction_window_end=window_end,
            error_message="max_records_per_lambda_run (2) reached",
        )
        assert contract.extraction_window_end == window_end
        assert contract.error_message == "max_records_per_lambda_run (2) reached"
