"""
Tests for the extraction pipeline Lambda handler.

Coverage:
  - Happy path: valid event + env vars → ExtractionWorkflow invoked, result returned
  - is_replay=True: replay_of_run_id forwarded to workflow.execute()
  - Missing required event field raises ValueError
  - Invalid source_id format raises ValueError
  - Invalid entity_id format raises ValueError
  - Unknown environment raises ValueError
  - connector_params not a dict raises ValueError
  - Missing RAW_S3_BUCKET env var raises RuntimeError
  - Missing SCHEMA_SNAPSHOT_S3_BUCKET env var raises RuntimeError
  - Missing AWS_REGION env var raises RuntimeError
  - Unknown source_id raises KeyError (not registered in registry)
  - Result is a dict (dataclasses.asdict) with expected keys
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from connector_runtime.extraction_pipeline_handler import lambda_handler
from orchestration.step_functions.extraction_workflow import ExtractionWorkflowResult

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------
_SOURCE = "salesforce"
_ENTITY = "salesforce-account"
_ENV = "dev"
_REGION = "us-east-1"
_RAW_BUCKET = "dev-raw-layer"
_SNAPSHOT_BUCKET = "dev-edl-schema-snapshots"

_VALID_EVENT: dict[str, object] = {
    "source_id": _SOURCE,
    "entity_id": _ENTITY,
    "environment": _ENV,
    "connector_params": {"object_name": "Account"},
    "is_replay": False,
}

_ENV_VARS = {
    "AWS_REGION": _REGION,
    "RAW_S3_BUCKET": _RAW_BUCKET,
    "SCHEMA_SNAPSHOT_S3_BUCKET": _SNAPSHOT_BUCKET,
}

_FAKE_RESULT = ExtractionWorkflowResult(
    run_id="run-20260612-120000000000-ab12cd34",
    source_id=_SOURCE,
    entity_id=_ENTITY,
    record_count=10,
    schema_fingerprint="abc123",
    raw_s3_prefix="salesforce/salesforce-account/extraction_date=2026-06-12/",
    drift_classification="no_drift",
    transformation_blocked=False,
    started_at=datetime.now(UTC).isoformat(),
    completed_at=datetime.now(UTC).isoformat(),
)


def _patch_workflow() -> MagicMock:
    """Return a mock ExtractionWorkflow whose execute() returns _FAKE_RESULT."""
    mock_workflow_cls = MagicMock()
    mock_workflow_instance = MagicMock()
    mock_workflow_instance.execute.return_value = _FAKE_RESULT
    mock_workflow_cls.return_value = mock_workflow_instance
    return mock_workflow_cls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_returns_result_dict(self) -> None:
        with (
            patch.dict("os.environ", _ENV_VARS),
            patch("connector_runtime.extraction_pipeline_handler.RunCoordinator") as mock_coord_cls,
            patch("connector_runtime.extraction_pipeline_handler.ConfigurationRepositoryClient"),
            patch("connector_runtime.extraction_pipeline_handler.WatermarkRepository"),
            patch("connector_runtime.extraction_pipeline_handler.SchemaSnapshotRepository"),
            patch("connector_runtime.extraction_pipeline_handler.SchemaDriftEvaluator"),
            patch("connector_runtime.extraction_pipeline_handler.connector_registry") as mock_reg,
            patch(
                "connector_runtime.extraction_pipeline_handler.ExtractionWorkflow"
            ) as mock_wf_cls,
        ):
            mock_coord = MagicMock()
            mock_coord_cls.return_value = mock_coord
            mock_reg.resolve_builder.return_value = MagicMock(
                return_value=(MagicMock(), MagicMock())
            )
            mock_wf_instance = MagicMock()
            mock_wf_instance.execute.return_value = _FAKE_RESULT
            mock_wf_cls.return_value = mock_wf_instance

            result = lambda_handler(_VALID_EVENT, None)

        assert isinstance(result, dict)
        assert result["source_id"] == _SOURCE
        assert result["entity_id"] == _ENTITY
        assert result["record_count"] == 10

    def test_result_has_all_workflow_result_fields(self) -> None:
        expected_keys = {f.name for f in dataclasses.fields(ExtractionWorkflowResult)}
        with (
            patch.dict("os.environ", _ENV_VARS),
            patch("connector_runtime.extraction_pipeline_handler.RunCoordinator"),
            patch("connector_runtime.extraction_pipeline_handler.ConfigurationRepositoryClient"),
            patch("connector_runtime.extraction_pipeline_handler.WatermarkRepository"),
            patch("connector_runtime.extraction_pipeline_handler.SchemaSnapshotRepository"),
            patch("connector_runtime.extraction_pipeline_handler.SchemaDriftEvaluator"),
            patch("connector_runtime.extraction_pipeline_handler.connector_registry") as mock_reg,
            patch(
                "connector_runtime.extraction_pipeline_handler.ExtractionWorkflow"
            ) as mock_wf_cls,
        ):
            mock_reg.resolve_builder.return_value = MagicMock(
                return_value=(MagicMock(), MagicMock())
            )
            mock_wf_instance = MagicMock()
            mock_wf_instance.execute.return_value = _FAKE_RESULT
            mock_wf_cls.return_value = mock_wf_instance

            result = lambda_handler(_VALID_EVENT, None)

        assert expected_keys == set(result.keys())

    def test_is_replay_forwarded_to_execute(self) -> None:
        replay_event = {**_VALID_EVENT, "is_replay": True, "replay_of_run_id": "run-orig"}
        with (
            patch.dict("os.environ", _ENV_VARS),
            patch("connector_runtime.extraction_pipeline_handler.RunCoordinator"),
            patch("connector_runtime.extraction_pipeline_handler.ConfigurationRepositoryClient"),
            patch("connector_runtime.extraction_pipeline_handler.WatermarkRepository"),
            patch("connector_runtime.extraction_pipeline_handler.SchemaSnapshotRepository"),
            patch("connector_runtime.extraction_pipeline_handler.SchemaDriftEvaluator"),
            patch("connector_runtime.extraction_pipeline_handler.connector_registry") as mock_reg,
            patch(
                "connector_runtime.extraction_pipeline_handler.ExtractionWorkflow"
            ) as mock_wf_cls,
        ):
            mock_reg.resolve_builder.return_value = MagicMock(
                return_value=(MagicMock(), MagicMock())
            )
            mock_wf_instance = MagicMock()
            mock_wf_instance.execute.return_value = _FAKE_RESULT
            mock_wf_cls.return_value = mock_wf_instance

            lambda_handler(replay_event, None)

        mock_wf_instance.execute.assert_called_once_with(
            is_replay=True,
            replay_of_run_id="run-orig",
        )

    def test_registry_resolve_builder_called_with_source_id(self) -> None:
        with (
            patch.dict("os.environ", _ENV_VARS),
            patch("connector_runtime.extraction_pipeline_handler.RunCoordinator"),
            patch("connector_runtime.extraction_pipeline_handler.ConfigurationRepositoryClient"),
            patch("connector_runtime.extraction_pipeline_handler.WatermarkRepository"),
            patch("connector_runtime.extraction_pipeline_handler.SchemaSnapshotRepository"),
            patch("connector_runtime.extraction_pipeline_handler.SchemaDriftEvaluator"),
            patch("connector_runtime.extraction_pipeline_handler.connector_registry") as mock_reg,
            patch(
                "connector_runtime.extraction_pipeline_handler.ExtractionWorkflow"
            ) as mock_wf_cls,
        ):
            mock_reg.resolve_builder.return_value = MagicMock(
                return_value=(MagicMock(), MagicMock())
            )
            mock_wf_instance = MagicMock()
            mock_wf_instance.execute.return_value = _FAKE_RESULT
            mock_wf_cls.return_value = mock_wf_instance

            lambda_handler(_VALID_EVENT, None)

        mock_reg.resolve_builder.assert_called_once_with(_SOURCE)


class TestEventValidation:
    def test_missing_source_id_raises(self) -> None:
        event = {k: v for k, v in _VALID_EVENT.items() if k != "source_id"}
        with patch.dict("os.environ", _ENV_VARS), pytest.raises(ValueError, match="source_id"):
            lambda_handler(event, None)

    def test_missing_entity_id_raises(self) -> None:
        event = {k: v for k, v in _VALID_EVENT.items() if k != "entity_id"}
        with patch.dict("os.environ", _ENV_VARS), pytest.raises(ValueError, match="entity_id"):
            lambda_handler(event, None)

    def test_missing_environment_raises(self) -> None:
        event = {k: v for k, v in _VALID_EVENT.items() if k != "environment"}
        with patch.dict("os.environ", _ENV_VARS), pytest.raises(ValueError, match="environment"):
            lambda_handler(event, None)

    def test_missing_connector_params_raises(self) -> None:
        event = {k: v for k, v in _VALID_EVENT.items() if k != "connector_params"}
        with (
            patch.dict("os.environ", _ENV_VARS),
            pytest.raises(ValueError, match="connector_params"),
        ):
            lambda_handler(event, None)

    def test_invalid_source_id_raises(self) -> None:
        event = {**_VALID_EVENT, "source_id": "INVALID_SOURCE!"}
        with patch.dict("os.environ", _ENV_VARS), pytest.raises(ValueError, match="source_id"):
            lambda_handler(event, None)

    def test_invalid_entity_id_raises(self) -> None:
        event = {**_VALID_EVENT, "entity_id": "UPPER CASE ENTITY"}
        with patch.dict("os.environ", _ENV_VARS), pytest.raises(ValueError, match="entity_id"):
            lambda_handler(event, None)

    def test_unknown_environment_raises(self) -> None:
        event = {**_VALID_EVENT, "environment": "production"}
        with patch.dict("os.environ", _ENV_VARS), pytest.raises(ValueError, match="environment"):
            lambda_handler(event, None)

    def test_connector_params_not_dict_raises(self) -> None:
        event = {**_VALID_EVENT, "connector_params": "not-a-dict"}
        with (
            patch.dict("os.environ", _ENV_VARS),
            pytest.raises(ValueError, match="connector_params"),
        ):
            lambda_handler(event, None)


class TestEnvironmentVariableValidation:
    def test_missing_aws_region_raises(self) -> None:
        env = {k: v for k, v in _ENV_VARS.items() if k != "AWS_REGION"}
        with (
            patch.dict("os.environ", env, clear=True),
            pytest.raises(RuntimeError, match="AWS_REGION"),
        ):
            lambda_handler(_VALID_EVENT, None)

    def test_missing_raw_s3_bucket_raises(self) -> None:
        env = {k: v for k, v in _ENV_VARS.items() if k != "RAW_S3_BUCKET"}
        with (
            patch.dict("os.environ", env, clear=True),
            pytest.raises(RuntimeError, match="RAW_S3_BUCKET"),
        ):
            lambda_handler(_VALID_EVENT, None)

    def test_missing_schema_snapshot_bucket_raises(self) -> None:
        env = {k: v for k, v in _ENV_VARS.items() if k != "SCHEMA_SNAPSHOT_S3_BUCKET"}
        with (
            patch.dict("os.environ", env, clear=True),
            pytest.raises(RuntimeError, match="SCHEMA_SNAPSHOT_S3_BUCKET"),
        ):
            lambda_handler(_VALID_EVENT, None)


class TestUnknownSourceId:
    def test_unregistered_source_id_raises_key_error(self) -> None:
        event = {**_VALID_EVENT, "source_id": "unknown-source"}
        with (
            patch.dict("os.environ", _ENV_VARS),
            patch("connector_runtime.extraction_pipeline_handler.RunCoordinator"),
            patch("connector_runtime.extraction_pipeline_handler.ConfigurationRepositoryClient"),
            patch("connector_runtime.extraction_pipeline_handler.WatermarkRepository"),
            patch("connector_runtime.extraction_pipeline_handler.SchemaSnapshotRepository"),
            patch("connector_runtime.extraction_pipeline_handler.SchemaDriftEvaluator"),
            patch("connector_runtime.extraction_pipeline_handler.connector_registry") as mock_reg,
        ):
            mock_reg.resolve_builder.side_effect = KeyError("unknown-source")
            with pytest.raises(KeyError):
                lambda_handler(event, None)
