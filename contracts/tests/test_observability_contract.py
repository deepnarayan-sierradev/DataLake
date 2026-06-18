"""
Tests for the observability contract — StructuredLogEvent validation
and sensitive value scrubbing.

Security focus: these tests are the primary gate ensuring that credentials,
tokens, and PII can never reach the log pipeline.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from contracts.observability_contract import (
    PipelineStage,
    RunStatus,
    StructuredLogEvent,
    scrub_sensitive_values,
)

# ---------------------------------------------------------------------------
# scrub_sensitive_values
# ---------------------------------------------------------------------------


class TestScrubSensitiveValues:
    def test_scrubs_password_equals_pattern(self) -> None:
        result = scrub_sensitive_values("connection string password=supersecret123")
        assert "supersecret123" not in result
        assert "[REDACTED]" in result

    def test_scrubs_token_colon_pattern(self) -> None:
        result = scrub_sensitive_values("Received token: eyJhbGciOiJIUzI1NiJ9.payload")
        assert "eyJhbGciOiJIUzI1NiJ9" not in result
        assert "[REDACTED]" in result

    def test_scrubs_bearer_authorization(self) -> None:
        result = scrub_sensitive_values("Authorization: Bearer abc123tokenvalue")
        assert "abc123tokenvalue" not in result

    def test_scrubs_aws_access_key_pattern(self) -> None:
        result = scrub_sensitive_values("Using key: AKIAIOSFODNN7EXAMPLE for signing")
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "[REDACTED]" in result

    def test_scrubs_api_key_pattern(self) -> None:
        result = scrub_sensitive_values("api_key=prod-secret-key-value-here")
        assert "prod-secret-key-value-here" not in result

    def test_passthrough_clean_business_message(self) -> None:
        clean = "Extraction completed for salesforce-account: 45000 records written to raw layer."
        assert scrub_sensitive_values(clean) == clean

    def test_multiple_sensitive_patterns_all_redacted(self) -> None:
        text = "password=abc token=xyz AKIAIOSFODNN7EXAMPLE"
        result = scrub_sensitive_values(text)
        assert "abc" not in result
        assert "xyz" not in result
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert result.count("[REDACTED]") >= 2


# ---------------------------------------------------------------------------
# StructuredLogEvent — valid construction
# ---------------------------------------------------------------------------


class TestStructuredLogEventValidConstruction:
    def _valid_event(self, **overrides: object) -> StructuredLogEvent:
        defaults: dict[str, object] = {
            "run_id": "run-2026-06-11-salesforce-account-001",
            "source_id": "salesforce",
            "entity_id": "salesforce-account",
            "stage": PipelineStage.EXTRACTION,
            "status": RunStatus.SUCCESS,
            "duration_ms": 4200,
            "retry_count": 0,
        }
        return StructuredLogEvent(**{**defaults, **overrides})

    def test_minimal_valid_event(self) -> None:
        event = self._valid_event()
        assert event.source_id == "salesforce"
        assert event.entity_id == "salesforce-account"
        assert event.retry_count == 0
        assert event.record_count is None

    def test_event_with_all_optional_fields(self) -> None:
        event = self._valid_event(
            message="Bulk API 2.0 job completed.",
            record_count=45_000,
            schema_version="v3",
            environment="prod",
        )
        assert event.record_count == 45_000
        assert event.schema_version == "v3"

    def test_event_is_immutable(self) -> None:
        event = self._valid_event()
        with pytest.raises((ValidationError, TypeError)):
            event.run_id = "tampered"  # pydantic frozen: raises ValidationError at runtime

    def test_netsuite_source_id_accepted(self) -> None:
        event = self._valid_event(source_id="netsuite", entity_id="netsuite-customer")
        assert event.source_id == "netsuite"

    def test_mysql_rds_source_id_accepted(self) -> None:
        event = self._valid_event(source_id="mysql-rds", entity_id="mysql-rds-order")
        assert event.source_id == "mysql-rds"

    def test_zero_duration_accepted_for_start_event(self) -> None:
        event = self._valid_event(duration_ms=0, status=RunStatus.STARTED)
        assert event.duration_ms == 0


# ---------------------------------------------------------------------------
# StructuredLogEvent — rejection of sensitive content
# ---------------------------------------------------------------------------


class TestStructuredLogEventSensitiveContentRejection:
    def _base(self) -> dict[str, object]:
        return {
            "run_id": "run-001",
            "source_id": "salesforce",
            "entity_id": "salesforce-account",
            "stage": PipelineStage.EXTRACTION,
            "status": RunStatus.FAILED,
            "duration_ms": 100,
            "retry_count": 1,
        }

    def test_rejects_message_containing_token(self) -> None:
        with pytest.raises(ValidationError, match="sensitive pattern"):
            StructuredLogEvent(**{**self._base(), "message": "Failed: token=abc123secret"})

    def test_rejects_message_containing_bearer(self) -> None:
        with pytest.raises(ValidationError, match="sensitive pattern"):
            StructuredLogEvent(**{**self._base(), "message": "Authorization: Bearer somejwttoken"})

    def test_rejects_message_containing_aws_key(self) -> None:
        with pytest.raises(ValidationError, match="sensitive pattern"):
            StructuredLogEvent(**{**self._base(), "message": "Key AKIAIOSFODNN7EXAMPLE used"})

    def test_rejects_error_classification_containing_secret(self) -> None:
        with pytest.raises(ValidationError, match="sensitive pattern"):
            StructuredLogEvent(**{**self._base(), "error_classification": "secret=leaked_here"})


# ---------------------------------------------------------------------------
# StructuredLogEvent — identifier format validation
# ---------------------------------------------------------------------------


class TestStructuredLogEventIdentifierValidation:
    def _base(self) -> dict[str, object]:
        return {
            "run_id": "run-001",
            "stage": PipelineStage.EXTRACTION,
            "status": RunStatus.SUCCESS,
            "duration_ms": 100,
            "retry_count": 0,
        }

    @pytest.mark.parametrize(
        "bad_id",
        [
            "Salesforce",  # uppercase not allowed
            "salesforce_account",  # underscore not allowed
            "PHASE1",  # prohibited generic names uppercase
            "helper",  # prohibited generic name
            "1salesforce",  # must start with lowercase letter
            "",  # empty not allowed
            "a" * 65,  # too long (>64 chars)
        ],
    )
    def test_rejects_invalid_source_id(self, bad_id: str) -> None:
        with pytest.raises(ValidationError):
            StructuredLogEvent(
                **{**self._base(), "source_id": bad_id, "entity_id": "salesforce-account"}
            )

    @pytest.mark.parametrize(
        "good_id",
        ["salesforce", "netsuite", "mysql-rds", "dynamics365", "hubspot"],
    )
    def test_accepts_valid_source_ids(self, good_id: str) -> None:
        event = StructuredLogEvent(
            **{**self._base(), "source_id": good_id, "entity_id": f"{good_id}-entity"}
        )
        assert event.source_id == good_id


# ---------------------------------------------------------------------------
# StructuredLogEvent — numeric field validation
# ---------------------------------------------------------------------------


class TestStructuredLogEventNumericValidation:
    def _base(self) -> dict[str, object]:
        return {
            "run_id": "run-001",
            "source_id": "salesforce",
            "entity_id": "salesforce-account",
            "stage": PipelineStage.EXTRACTION,
            "status": RunStatus.SUCCESS,
        }

    def test_rejects_negative_duration_ms(self) -> None:
        with pytest.raises(ValidationError):
            StructuredLogEvent(**{**self._base(), "duration_ms": -1, "retry_count": 0})

    def test_rejects_negative_retry_count(self) -> None:
        with pytest.raises(ValidationError):
            StructuredLogEvent(**{**self._base(), "duration_ms": 100, "retry_count": -1})

    def test_rejects_negative_record_count(self) -> None:
        with pytest.raises(ValidationError):
            StructuredLogEvent(
                **{**self._base(), "duration_ms": 100, "retry_count": 0, "record_count": -5}
            )


# ---------------------------------------------------------------------------
# StructuredLogEvent — run_id enumeration prevention
# ---------------------------------------------------------------------------


class TestStructuredLogEventRunIdValidation:
    def _base(self) -> dict[str, object]:
        return {
            "source_id": "salesforce",
            "entity_id": "salesforce-account",
            "stage": PipelineStage.EXTRACTION,
            "status": RunStatus.SUCCESS,
            "duration_ms": 100,
            "retry_count": 0,
        }

    @pytest.mark.parametrize("bad_run_id", ["1", "42", "999999", "0"])
    def test_rejects_sequential_integer_run_id(self, bad_run_id: str) -> None:
        with pytest.raises(ValidationError, match="sequential integer"):
            StructuredLogEvent(**{**self._base(), "run_id": bad_run_id})

    @pytest.mark.parametrize(
        "good_run_id",
        [
            "run-001",
            "run-2026-06-11-salesforce-account-001",
            "run-a3f9c1d2-8b4e-4e1d-b3a2-c1d2e3f4a5b6",
            "extraction-20260611-netsuite-customer",
        ],
    )
    def test_accepts_valid_run_ids(self, good_run_id: str) -> None:
        event = StructuredLogEvent(**{**self._base(), "run_id": good_run_id})
        assert event.run_id == good_run_id
