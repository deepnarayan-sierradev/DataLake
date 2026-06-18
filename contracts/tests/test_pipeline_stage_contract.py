"""
Tests for PipelineStageContract — covering validators not exercised elsewhere.

Lines targeted:
  - Line 129: scrub_error_fields returns None when value is None
  - Line 136: validate_environment rejects invalid environment values
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from contracts.observability_contract import PipelineStage, RunStatus
from contracts.pipeline_stage_contract import DriftClassification, PipelineStageContract

_NOW = datetime(2026, 6, 11, 14, 0, 0, tzinfo=UTC)


def _make_contract(**kwargs: object) -> PipelineStageContract:
    defaults: dict[str, object] = {
        "run_id": "run-pipeline-test-001",
        "source_id": "salesforce",
        "entity_id": "salesforce-account",
        "stage": PipelineStage.EXTRACTION,
        "status": RunStatus.SUCCESS,
        "environment": "dev",
        "completed_at": _NOW,
    }
    defaults.update(kwargs)
    return PipelineStageContract(**defaults)  # type: ignore[arg-type]


class TestScrubErrorFieldsValidator:
    def test_none_error_message_is_accepted(self) -> None:
        """Line 129: when value is None, scrub_error_fields returns None."""
        contract = _make_contract(error_message=None, error_code=None)
        assert contract.error_message is None
        assert contract.error_code is None

    def test_sensitive_error_message_is_scrubbed(self) -> None:
        contract = _make_contract(
            status=RunStatus.FAILED,
            error_message="Connection failed: password=supersecret123",
        )
        assert "supersecret123" not in (contract.error_message or "")
        assert "[REDACTED]" in (contract.error_message or "")

    def test_clean_error_message_passes_through(self) -> None:
        contract = _make_contract(
            status=RunStatus.FAILED,
            error_message="Network timeout after 30 seconds",
        )
        assert contract.error_message == "Network timeout after 30 seconds"


class TestEnvironmentValidator:
    def test_dev_accepted(self) -> None:
        contract = _make_contract(environment="dev")
        assert contract.environment == "dev"

    def test_staging_accepted(self) -> None:
        contract = _make_contract(environment="staging")
        assert contract.environment == "staging"

    def test_prod_accepted(self) -> None:
        contract = _make_contract(environment="prod")
        assert contract.environment == "prod"

    def test_invalid_environment_raises(self) -> None:
        """Line 136: validate_environment raises ValueError for unknown env."""
        with pytest.raises(ValidationError, match="environment must be one of"):
            _make_contract(environment="local")

    def test_uppercase_environment_raises(self) -> None:
        with pytest.raises(ValidationError, match="environment must be one of"):
            _make_contract(environment="DEV")


class TestDriftClassification:
    def test_drift_classification_enum_values(self) -> None:
        assert DriftClassification.NO_DRIFT.value == "no_drift"
        assert DriftClassification.NON_BREAKING.value == "non_breaking"
        assert DriftClassification.POTENTIALLY_BREAKING.value == "potentially_breaking"
        assert DriftClassification.BREAKING.value == "breaking"

    def test_contract_accepts_drift_classification(self) -> None:
        contract = _make_contract(drift_classification=DriftClassification.BREAKING)
        assert contract.drift_classification == DriftClassification.BREAKING
