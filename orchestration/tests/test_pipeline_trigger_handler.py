"""
Tests for orchestration/pipeline_trigger/pipeline_trigger_handler.py

Covers:
  - TriggerMessage Pydantic validation
  - _process_record happy path
  - ExecutionAlreadyExists idempotency
  - Invalid JSON / validation failure handling
  - Execution name sanitisation
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from orchestration.pipeline_trigger.pipeline_trigger_handler import (
    TriggerMessage,
    _process_record,
    lambda_handler,
)

# ---------------------------------------------------------------------------
# TriggerMessage validation
# ---------------------------------------------------------------------------


class TestTriggerMessageValidation:
    def test_valid_message_parses(self) -> None:
        msg = TriggerMessage.model_validate(
            {
                "source_id": "salesforce",
                "entity_id": "salesforce-account",
                "environment": "dev",
                "connector_params": {"object_name": "Account"},
                "tenant_code": "demo",
            }
        )
        assert msg.source_id == "salesforce"
        assert msg.tenant_code == "demo"
        assert msg.is_replay is False  # default

    def test_missing_tenant_code_is_rejected(self) -> None:
        """ARCH-17: tenant_code has no fail-open default — a message that
        omits it must be rejected, not silently run as the demo tenant."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="tenant_code"):
            TriggerMessage.model_validate(
                {
                    "source_id": "salesforce",
                    "entity_id": "salesforce-account",
                    "environment": "dev",
                }
            )

    def test_custom_tenant_code(self) -> None:
        msg = TriggerMessage.model_validate(
            {
                "source_id": "salesforce",
                "entity_id": "salesforce-account",
                "environment": "dev",
                "tenant_code": "acme-corp",
            }
        )
        assert msg.tenant_code == "acme-corp"

    def test_invalid_environment_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TriggerMessage.model_validate(
                {
                    "source_id": "salesforce",
                    "entity_id": "salesforce-account",
                    "environment": "PRODUCTION",  # invalid
                }
            )

    def test_invalid_source_id_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TriggerMessage.model_validate(
                {
                    "source_id": "InvalidID!",
                    "entity_id": "salesforce-account",
                    "environment": "dev",
                }
            )

    def test_invalid_tenant_code_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TriggerMessage.model_validate(
                {
                    "source_id": "salesforce",
                    "entity_id": "salesforce-account",
                    "environment": "dev",
                    "tenant_code": "UPPER_CASE",  # invalid
                }
            )

    def test_extra_fields_forbidden(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TriggerMessage.model_validate(
                {
                    "source_id": "salesforce",
                    "entity_id": "salesforce-account",
                    "environment": "dev",
                    "unknown_field": "should_fail",
                }
            )

    def test_missing_source_id_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TriggerMessage.model_validate(
                {
                    "entity_id": "salesforce-account",
                    "environment": "dev",
                }
            )

    def test_staging_environment_valid(self) -> None:
        msg = TriggerMessage.model_validate(
            {
                "source_id": "netsuite",
                "entity_id": "netsuite-customer",
                "environment": "staging",
                "tenant_code": "demo",
            }
        )
        assert msg.environment == "staging"

    def test_prod_environment_valid(self) -> None:
        msg = TriggerMessage.model_validate(
            {
                "source_id": "netsuite",
                "entity_id": "netsuite-customer",
                "environment": "prod",
                "tenant_code": "demo",
            }
        )
        assert msg.environment == "prod"


# ---------------------------------------------------------------------------
# _process_record
# ---------------------------------------------------------------------------


class TestProcessRecord:
    def _make_sqs_record(self, body: dict[str, Any]) -> dict[str, Any]:
        return {
            "messageId": "test-msg-id",
            "body": json.dumps(body),
        }

    def _valid_body(self) -> dict[str, Any]:
        return {
            "source_id": "salesforce",
            "entity_id": "salesforce-account",
            "environment": "dev",
            "connector_params": {"object_name": "Account"},
            "tenant_code": "demo",
            "schedule_tick_iso": "2026-07-07T02:00:00",
        }

    def test_starts_step_functions_execution(self) -> None:
        mock_sfn = MagicMock()
        mock_sfn.start_execution.return_value = {
            "executionArn": "arn:aws:states:us-east-1:123:execution:sm:exec-001"
        }

        with patch("orchestration.pipeline_trigger.pipeline_trigger_handler._sfn_client", mock_sfn):
            _process_record(self._make_sqs_record(self._valid_body()), "arn:sfn:test")

        mock_sfn.start_execution.assert_called_once()
        call_kwargs = mock_sfn.start_execution.call_args[1]
        assert call_kwargs["stateMachineArn"] == "arn:sfn:test"
        parsed_input = json.loads(call_kwargs["input"])
        assert parsed_input["source_id"] == "salesforce"
        assert parsed_input["tenant_code"] == "demo"

    def test_execution_already_exists_is_no_op(self) -> None:
        mock_sfn = MagicMock()
        mock_sfn.start_execution.side_effect = ClientError(
            {"Error": {"Code": "ExecutionAlreadyExists", "Message": "already exists"}},
            "StartExecution",
        )

        with patch("orchestration.pipeline_trigger.pipeline_trigger_handler._sfn_client", mock_sfn):
            # Should not raise
            _process_record(self._make_sqs_record(self._valid_body()), "arn:sfn:test")

    def test_invalid_json_body_raises_value_error(self) -> None:
        record = {"messageId": "bad-msg", "body": "not valid json {{{"}
        with pytest.raises(ValueError, match="invalid JSON"):
            _process_record(record, "arn:sfn:test")

    def test_invalid_source_id_raises_value_error(self) -> None:
        body = self._valid_body()
        body["source_id"] = "INVALID_ID!"
        with pytest.raises(ValueError, match="failed validation"):
            _process_record(self._make_sqs_record(body), "arn:sfn:test")

    def test_missing_tenant_code_raises_value_error(self) -> None:
        """ARCH-17: a record whose body omits tenant_code must be rejected by
        _process_record (via TriggerMessage validation), never defaulted to
        "demo" and started as a real Step Functions execution."""
        body = self._valid_body()
        del body["tenant_code"]
        with pytest.raises(ValueError, match="failed validation"):
            _process_record(self._make_sqs_record(body), "arn:sfn:test")

    def test_sfn_client_error_propagates(self) -> None:
        mock_sfn = MagicMock()
        mock_sfn.start_execution.side_effect = ClientError(
            {"Error": {"Code": "StateMachineDoesNotExist", "Message": "not found"}},
            "StartExecution",
        )

        with patch("orchestration.pipeline_trigger.pipeline_trigger_handler._sfn_client", mock_sfn):
            with pytest.raises(ClientError):
                _process_record(self._make_sqs_record(self._valid_body()), "arn:sfn:test")

    def test_execution_name_derived_from_schedule_tick(self) -> None:
        mock_sfn = MagicMock()
        mock_sfn.start_execution.return_value = {"executionArn": "arn:fake"}
        body = self._valid_body()
        body["schedule_tick_iso"] = "2026-07-07T02:00:00"

        with patch("orchestration.pipeline_trigger.pipeline_trigger_handler._sfn_client", mock_sfn):
            _process_record(self._make_sqs_record(body), "arn:sfn:test")

        call_kwargs = mock_sfn.start_execution.call_args[1]
        exec_name = call_kwargs["name"]
        # Must contain source_id, entity_id, and tick components
        assert "salesforce" in exec_name
        assert "salesforce-account" in exec_name
        assert len(exec_name) <= 80

    def test_execution_name_max_80_chars(self) -> None:
        """Execution name must never exceed 80 characters even with long IDs."""
        mock_sfn = MagicMock()
        mock_sfn.start_execution.return_value = {"executionArn": "arn:fake"}
        body = {
            "source_id": "salesforce",
            "entity_id": "salesforce-account",
            "environment": "dev",
            "tenant_code": "demo",
            "schedule_tick_iso": "2026-07-07T02:00:00.000000",
        }

        with patch("orchestration.pipeline_trigger.pipeline_trigger_handler._sfn_client", mock_sfn):
            _process_record({"messageId": "x", "body": json.dumps(body)}, "arn:sfn:test")

        exec_name = mock_sfn.start_execution.call_args[1]["name"]
        assert len(exec_name) <= 80

    def test_no_records_in_event_is_noop(self) -> None:
        mock_sfn = MagicMock()
        with (
            patch("orchestration.pipeline_trigger.pipeline_trigger_handler._sfn_client", mock_sfn),
            patch.dict(
                "os.environ", {"STATE_MACHINE_ARN": "arn:sfn:test", "AWS_REGION": "us-east-1"}
            ),
        ):
            lambda_handler({"Records": []}, None)
        mock_sfn.start_execution.assert_not_called()

    def test_lambda_handler_processes_single_record(self) -> None:
        mock_sfn = MagicMock()
        mock_sfn.start_execution.return_value = {"executionArn": "arn:fake"}

        event = {"Records": [{"messageId": "m1", "body": json.dumps(self._valid_body())}]}

        with (
            patch("orchestration.pipeline_trigger.pipeline_trigger_handler._sfn_client", mock_sfn),
            patch.dict(
                "os.environ", {"STATE_MACHINE_ARN": "arn:sfn:test", "AWS_REGION": "us-east-1"}
            ),
        ):
            lambda_handler(event, None)

        mock_sfn.start_execution.assert_called_once()


class TestBatchSizeContract:
    def _valid_body(self):
        return {
            "source_id": "salesforce",
            "entity_id": "salesforce-account",
            "environment": "dev",
            "connector_params": {},
            "tenant_code": "demo",
        }

    def test_multiple_records_raises_value_error(self) -> None:
        """Handler must reject events with more than 1 SQS record (batch_size contract)."""
        import json
        from unittest.mock import patch

        record = {"messageId": "m1", "body": json.dumps(self._valid_body())}
        event = {"Records": [record, record]}  # 2 records — contract violation

        with patch.dict(
            "os.environ", {"STATE_MACHINE_ARN": "arn:sfn:test", "AWS_REGION": "us-east-1"}
        ):
            with pytest.raises(ValueError, match="batch_size must be 1"):
                lambda_handler(event, None)

    def test_single_record_is_accepted(self) -> None:
        """Handler must accept events with exactly 1 SQS record."""
        import json
        from unittest.mock import MagicMock, patch

        mock_sfn = MagicMock()
        mock_sfn.start_execution.return_value = {"executionArn": "arn:fake"}
        record = {"messageId": "m1", "body": json.dumps(self._valid_body())}
        event = {"Records": [record]}

        with (
            patch("orchestration.pipeline_trigger.pipeline_trigger_handler._sfn_client", mock_sfn),
            patch.dict(
                "os.environ", {"STATE_MACHINE_ARN": "arn:sfn:test", "AWS_REGION": "us-east-1"}
            ),
        ):
            lambda_handler(event, None)  # Must not raise
        mock_sfn.start_execution.assert_called_once()
