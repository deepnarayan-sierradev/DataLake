"""
Tests for RunReplayController and DlqEntry.

Coverage:
  - parse_dlq_entry() returns DlqEntry from valid JSON
  - parse_dlq_entry() raises ReplayValidationError for invalid JSON
  - parse_dlq_entry() raises ReplayValidationError for non-object JSON
  - parse_dlq_entry() raises ReplayValidationError for missing required fields
  - parse_dlq_entry() raises ReplayValidationError for invalid run_id format
  - parse_dlq_entry() raises ReplayValidationError for invalid source_id format
  - parse_dlq_entry() raises ReplayValidationError for invalid entity_id format
  - parse_dlq_entry() raises ReplayValidationError for unknown environment
  - start_replay_execution() calls Step Functions StartExecution with correct input
  - start_replay_execution() sets is_replay=True in the Step Functions input
  - start_replay_execution() sets replay_of_run_id to original run_id
  - start_replay_execution() uses a deterministic execution name from the run_id
  - start_replay_execution() returns existing ARN on ExecutionAlreadyExists (idempotent)
  - start_replay_execution() re-raises non-idempotent ClientError from SFN API
  - RunReplayController constructor rejects empty state_machine_arn
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from orchestration.step_functions.run_replay_controller import (
    DlqEntry,
    ReplayValidationError,
    RunReplayController,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_STATE_MACHINE_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:extraction-pipeline"
_REGION = "us-east-1"
_SOURCE = "netsuite"
_ENTITY = "netsuite-customer"
_RUN_ID = "run-20260612-143022123456-a3f9c1d2"
_EXECUTION_ARN = (
    "arn:aws:states:us-east-1:123456789012:execution:extraction-pipeline:replay-run-20260612"
)

_VALID_ENTRY_BODY = json.dumps(
    {
        "run_id": _RUN_ID,
        "source_id": _SOURCE,
        "entity_id": _ENTITY,
        "environment": "dev",
        "failed_stage": "extraction",
        "error_code": "transient_network",
        "error_message": "Connection timed out",
        "enqueued_at": datetime.now(UTC).isoformat(),
    }
)

_CONNECTOR_PARAMS = {"record_type": "customer"}


def _make_controller() -> tuple[RunReplayController, MagicMock]:
    """Return controller + mock SFN client."""
    mock_sfn = MagicMock()
    mock_sfn.start_execution.return_value = {"executionArn": _EXECUTION_ARN}

    with patch("boto3.client", return_value=mock_sfn):
        controller = RunReplayController(
            state_machine_arn=_STATE_MACHINE_ARN,
            region_name=_REGION,
        )
    return controller, mock_sfn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestParseDlqEntry:
    def test_valid_entry_parsed_correctly(self) -> None:
        controller, _ = _make_controller()
        entry = controller.parse_dlq_entry(_VALID_ENTRY_BODY)

        assert isinstance(entry, DlqEntry)
        assert entry.run_id == _RUN_ID
        assert entry.source_id == _SOURCE
        assert entry.entity_id == _ENTITY
        assert entry.environment == "dev"
        assert entry.failed_stage == "extraction"
        assert entry.error_code == "transient_network"

    def test_invalid_json_raises(self) -> None:
        controller, _ = _make_controller()
        with pytest.raises(ReplayValidationError, match="not valid JSON"):
            controller.parse_dlq_entry("not-json{{{")

    def test_non_object_json_raises(self) -> None:
        controller, _ = _make_controller()
        with pytest.raises(ReplayValidationError, match="JSON object"):
            controller.parse_dlq_entry("[1, 2, 3]")

    def test_missing_required_fields_raises(self) -> None:
        controller, _ = _make_controller()
        incomplete = json.dumps({"run_id": _RUN_ID, "source_id": _SOURCE})
        with pytest.raises(ReplayValidationError, match="missing required fields"):
            controller.parse_dlq_entry(incomplete)

    def test_invalid_source_id_raises(self) -> None:
        controller, _ = _make_controller()
        raw = json.loads(_VALID_ENTRY_BODY)
        raw["source_id"] = "Invalid_Source"
        with pytest.raises(ReplayValidationError, match="source_id"):
            controller.parse_dlq_entry(json.dumps(raw))

    def test_invalid_entity_id_raises(self) -> None:
        controller, _ = _make_controller()
        raw = json.loads(_VALID_ENTRY_BODY)
        raw["entity_id"] = "INVALID-entity-ID!"
        with pytest.raises(ReplayValidationError, match="entity_id"):
            controller.parse_dlq_entry(json.dumps(raw))

    def test_invalid_run_id_raises(self) -> None:
        controller, _ = _make_controller()
        raw = json.loads(_VALID_ENTRY_BODY)
        raw["run_id"] = "../../../bad-run-id"
        with pytest.raises(ReplayValidationError, match="run_id"):
            controller.parse_dlq_entry(json.dumps(raw))

    def test_unknown_environment_raises(self) -> None:
        controller, _ = _make_controller()
        raw = json.loads(_VALID_ENTRY_BODY)
        raw["environment"] = "production"  # not in known environments
        with pytest.raises(ReplayValidationError, match="environment"):
            controller.parse_dlq_entry(json.dumps(raw))

    def test_entry_is_frozen(self) -> None:
        controller, _ = _make_controller()
        entry = controller.parse_dlq_entry(_VALID_ENTRY_BODY)
        with pytest.raises((AttributeError, TypeError)):
            entry.run_id = "tampered"  # type: ignore[misc]


class TestStartReplayExecution:
    def test_start_execution_called_with_correct_arn(self) -> None:
        controller, mock_sfn = _make_controller()
        entry = controller.parse_dlq_entry(_VALID_ENTRY_BODY)

        controller.start_replay_execution(entry, _CONNECTOR_PARAMS)

        mock_sfn.start_execution.assert_called_once()
        call_kwargs = mock_sfn.start_execution.call_args[1]
        assert call_kwargs["stateMachineArn"] == _STATE_MACHINE_ARN

    def test_input_payload_has_is_replay_true(self) -> None:
        controller, mock_sfn = _make_controller()
        entry = controller.parse_dlq_entry(_VALID_ENTRY_BODY)

        controller.start_replay_execution(entry, _CONNECTOR_PARAMS)

        call_kwargs = mock_sfn.start_execution.call_args[1]
        payload = json.loads(call_kwargs["input"])
        assert payload["is_replay"] is True

    def test_input_payload_carries_original_run_id(self) -> None:
        controller, mock_sfn = _make_controller()
        entry = controller.parse_dlq_entry(_VALID_ENTRY_BODY)

        controller.start_replay_execution(entry, _CONNECTOR_PARAMS)

        call_kwargs = mock_sfn.start_execution.call_args[1]
        payload = json.loads(call_kwargs["input"])
        assert payload["replay_of_run_id"] == _RUN_ID

    def test_input_payload_carries_connector_params(self) -> None:
        controller, mock_sfn = _make_controller()
        entry = controller.parse_dlq_entry(_VALID_ENTRY_BODY)

        controller.start_replay_execution(entry, _CONNECTOR_PARAMS)

        call_kwargs = mock_sfn.start_execution.call_args[1]
        payload = json.loads(call_kwargs["input"])
        assert payload["connector_params"] == _CONNECTOR_PARAMS

    def test_execution_name_contains_run_id(self) -> None:
        controller, mock_sfn = _make_controller()
        entry = controller.parse_dlq_entry(_VALID_ENTRY_BODY)

        controller.start_replay_execution(entry, _CONNECTOR_PARAMS)

        call_kwargs = mock_sfn.start_execution.call_args[1]
        assert _RUN_ID in call_kwargs["name"]
        assert "replay" in call_kwargs["name"]

    def test_execution_name_max_80_chars(self) -> None:
        controller, mock_sfn = _make_controller()
        # Use an entry with a long run_id to test truncation
        long_run_id = "run-20260612-143022123456-" + ("a" * 60)
        raw = json.loads(_VALID_ENTRY_BODY)
        raw["run_id"] = long_run_id
        entry = controller.parse_dlq_entry(json.dumps(raw))

        controller.start_replay_execution(entry, _CONNECTOR_PARAMS)

        call_kwargs = mock_sfn.start_execution.call_args[1]
        assert len(call_kwargs["name"]) <= 80

    def test_returns_execution_arn(self) -> None:
        controller, _ = _make_controller()
        entry = controller.parse_dlq_entry(_VALID_ENTRY_BODY)

        arn = controller.start_replay_execution(entry, _CONNECTOR_PARAMS)
        assert arn == _EXECUTION_ARN

    def test_sfn_client_error_propagates(self) -> None:
        controller, mock_sfn = _make_controller()
        mock_sfn.start_execution.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Access denied"}},
            "StartExecution",
        )
        entry = controller.parse_dlq_entry(_VALID_ENTRY_BODY)

        with pytest.raises(ClientError):
            controller.start_replay_execution(entry, _CONNECTOR_PARAMS)

    def test_execution_already_exists_returns_idempotent_arn(self) -> None:
        """Duplicate SQS delivery should not trigger a second execution."""
        controller, mock_sfn = _make_controller()
        mock_sfn.start_execution.side_effect = ClientError(
            {"Error": {"Code": "ExecutionAlreadyExists", "Message": "Already exists"}},
            "StartExecution",
        )
        entry = controller.parse_dlq_entry(_VALID_ENTRY_BODY)

        arn = controller.start_replay_execution(entry, _CONNECTOR_PARAMS)

        # Should return a deterministic ARN derived from the state machine ARN
        assert "execution" in arn
        assert "replay" in arn
        assert entry.run_id in arn


class TestConstructorValidation:
    def test_empty_state_machine_arn_raises(self) -> None:
        with pytest.raises(ValueError, match="state_machine_arn"):
            RunReplayController(state_machine_arn="", region_name=_REGION)
