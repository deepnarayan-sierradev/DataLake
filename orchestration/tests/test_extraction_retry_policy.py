"""
Tests for ExtractionRetryPolicy and CircuitOpenError.

Coverage:
  - Transient error classifications are retry-eligible
  - Deterministic and UNKNOWN classifications are not retryable
  - should_retry() respects attempt limit
  - compute_delay_seconds() grows with attempt number (exponential)
  - compute_delay_seconds() is capped at max_delay_seconds
  - Circuit breaker opens after consecutive failure threshold
  - Circuit breaker resets on success
  - reset_circuit() clears state
  - Invalid constructor parameters raise ValueError
"""

from __future__ import annotations

import pytest

from connector_runtime.interfaces.connector_interface import ExtractionErrorClassification
from orchestration.step_functions.extraction_retry_policy import (
    CircuitOpenError,
    ExtractionRetryPolicy,
)

_SOURCE_ID = "salesforce"


def _policy(**kwargs: object) -> ExtractionRetryPolicy:
    defaults = {
        "max_transient_attempts": 3,
        "base_delay_seconds": 1.0,
        "max_delay_seconds": 60.0,
        "backoff_multiplier": 2.0,
        "circuit_open_threshold": 3,
        "jitter_fraction": 0.0,  # deterministic in tests
    }
    defaults.update(kwargs)
    return ExtractionRetryPolicy(**defaults)


class TestRetryEligibility:
    def test_transient_timeout_is_retryable(self) -> None:
        p = _policy()
        assert p.is_retryable(ExtractionErrorClassification.TRANSIENT_TIMEOUT) is True

    def test_transient_throttle_is_retryable(self) -> None:
        p = _policy()
        assert p.is_retryable(ExtractionErrorClassification.TRANSIENT_THROTTLE) is True

    def test_transient_network_is_retryable(self) -> None:
        p = _policy()
        assert p.is_retryable(ExtractionErrorClassification.TRANSIENT_NETWORK) is True

    def test_deterministic_invalid_credentials_not_retryable(self) -> None:
        p = _policy()
        assert (
            p.is_retryable(ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS) is False
        )

    def test_deterministic_invalid_object_not_retryable(self) -> None:
        p = _policy()
        assert p.is_retryable(ExtractionErrorClassification.DETERMINISTIC_INVALID_OBJECT) is False

    def test_deterministic_invalid_configuration_not_retryable(self) -> None:
        p = _policy()
        assert (
            p.is_retryable(ExtractionErrorClassification.DETERMINISTIC_INVALID_CONFIGURATION)
            is False
        )

    def test_deterministic_schema_violation_not_retryable(self) -> None:
        p = _policy()
        assert p.is_retryable(ExtractionErrorClassification.DETERMINISTIC_SCHEMA_VIOLATION) is False

    def test_unknown_not_retryable(self) -> None:
        p = _policy()
        assert p.is_retryable(ExtractionErrorClassification.UNKNOWN) is False


class TestShouldRetry:
    def test_first_transient_failure_should_retry(self) -> None:
        p = _policy(max_transient_attempts=3)
        assert p.should_retry(ExtractionErrorClassification.TRANSIENT_NETWORK, attempt=1) is True

    def test_second_transient_failure_should_retry(self) -> None:
        p = _policy(max_transient_attempts=3)
        assert p.should_retry(ExtractionErrorClassification.TRANSIENT_NETWORK, attempt=2) is True

    def test_at_max_attempts_no_more_retries(self) -> None:
        p = _policy(max_transient_attempts=3)
        # attempt=3 means third attempt just failed; no fourth attempt allowed
        assert p.should_retry(ExtractionErrorClassification.TRANSIENT_NETWORK, attempt=3) is False

    def test_beyond_max_attempts_no_retry(self) -> None:
        p = _policy(max_transient_attempts=3)
        assert p.should_retry(ExtractionErrorClassification.TRANSIENT_NETWORK, attempt=10) is False

    def test_deterministic_never_retried_even_at_attempt_1(self) -> None:
        p = _policy(max_transient_attempts=10)
        assert (
            p.should_retry(
                ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS, attempt=1
            )
            is False
        )

    def test_single_attempt_policy_never_retries(self) -> None:
        p = _policy(max_transient_attempts=1)
        assert p.should_retry(ExtractionErrorClassification.TRANSIENT_TIMEOUT, attempt=1) is False


class TestBackoffDelay:
    def test_attempt_1_returns_base_delay(self) -> None:
        p = _policy(base_delay_seconds=2.0, backoff_multiplier=2.0, jitter_fraction=0.0)
        assert p.compute_delay_seconds(attempt=1) == pytest.approx(2.0)

    def test_attempt_2_doubles_base_delay(self) -> None:
        p = _policy(base_delay_seconds=2.0, backoff_multiplier=2.0, jitter_fraction=0.0)
        assert p.compute_delay_seconds(attempt=2) == pytest.approx(4.0)

    def test_attempt_3_quadruples_base_delay(self) -> None:
        p = _policy(base_delay_seconds=2.0, backoff_multiplier=2.0, jitter_fraction=0.0)
        assert p.compute_delay_seconds(attempt=3) == pytest.approx(8.0)

    def test_delay_capped_at_max(self) -> None:
        p = _policy(
            base_delay_seconds=1.0,
            backoff_multiplier=10.0,
            max_delay_seconds=30.0,
            jitter_fraction=0.0,
        )
        # 1 * 10^10 >> 30, but must be capped
        assert p.compute_delay_seconds(attempt=10) == pytest.approx(30.0)

    def test_delay_with_jitter_is_non_negative(self) -> None:
        p = _policy(base_delay_seconds=1.0, jitter_fraction=1.0)
        for attempt in range(1, 5):
            assert p.compute_delay_seconds(attempt) >= 0.0

    def test_delay_increases_across_attempts(self) -> None:
        p = _policy(
            base_delay_seconds=1.0,
            backoff_multiplier=2.0,
            max_delay_seconds=1000.0,
            jitter_fraction=0.0,
        )
        delays = [p.compute_delay_seconds(i) for i in range(1, 6)]
        for i in range(1, len(delays)):
            assert delays[i] > delays[i - 1]


class TestCircuitBreaker:
    def test_circuit_closed_by_default(self) -> None:
        p = _policy(circuit_open_threshold=3)
        assert p.is_circuit_open(_SOURCE_ID) is False

    def test_circuit_opens_at_threshold(self) -> None:
        p = _policy(circuit_open_threshold=3)
        p.record_failure(_SOURCE_ID)
        p.record_failure(_SOURCE_ID)
        assert p.is_circuit_open(_SOURCE_ID) is False
        p.record_failure(_SOURCE_ID)
        assert p.is_circuit_open(_SOURCE_ID) is True

    def test_circuit_stays_open_beyond_threshold(self) -> None:
        p = _policy(circuit_open_threshold=2)
        p.record_failure(_SOURCE_ID)
        p.record_failure(_SOURCE_ID)
        p.record_failure(_SOURCE_ID)
        assert p.is_circuit_open(_SOURCE_ID) is True

    def test_record_success_resets_circuit(self) -> None:
        p = _policy(circuit_open_threshold=2)
        p.record_failure(_SOURCE_ID)
        p.record_failure(_SOURCE_ID)
        assert p.is_circuit_open(_SOURCE_ID) is True
        p.record_success(_SOURCE_ID)
        assert p.is_circuit_open(_SOURCE_ID) is False

    def test_reset_circuit_clears_state(self) -> None:
        p = _policy(circuit_open_threshold=2)
        p.record_failure(_SOURCE_ID)
        p.record_failure(_SOURCE_ID)
        assert p.is_circuit_open(_SOURCE_ID) is True
        p.reset_circuit(_SOURCE_ID)
        assert p.is_circuit_open(_SOURCE_ID) is False
        assert p.consecutive_failures(_SOURCE_ID) == 0

    def test_consecutive_failures_increments(self) -> None:
        p = _policy(circuit_open_threshold=10)
        for i in range(1, 5):
            p.record_failure(_SOURCE_ID)
            assert p.consecutive_failures(_SOURCE_ID) == i

    def test_circuit_is_per_source(self) -> None:
        p = _policy(circuit_open_threshold=2)
        p.record_failure("salesforce")
        p.record_failure("salesforce")
        assert p.is_circuit_open("salesforce") is True
        assert p.is_circuit_open("netsuite") is False

    def test_record_success_on_unknown_source_does_not_raise(self) -> None:
        p = _policy()
        p.record_success("new-source")  # should not raise
        assert p.consecutive_failures("new-source") == 0


class TestConstructorValidation:
    def test_invalid_max_transient_attempts_raises(self) -> None:
        with pytest.raises(ValueError, match="max_transient_attempts"):
            ExtractionRetryPolicy(max_transient_attempts=0)

    def test_invalid_base_delay_raises(self) -> None:
        with pytest.raises(ValueError, match="base_delay_seconds"):
            ExtractionRetryPolicy(base_delay_seconds=0.0)

    def test_max_delay_less_than_base_raises(self) -> None:
        with pytest.raises(ValueError, match="max_delay_seconds"):
            ExtractionRetryPolicy(base_delay_seconds=10.0, max_delay_seconds=5.0)

    def test_invalid_backoff_multiplier_raises(self) -> None:
        with pytest.raises(ValueError, match="backoff_multiplier"):
            ExtractionRetryPolicy(backoff_multiplier=0.5)

    def test_invalid_circuit_open_threshold_raises(self) -> None:
        with pytest.raises(ValueError, match="circuit_open_threshold"):
            ExtractionRetryPolicy(circuit_open_threshold=0)

    def test_invalid_jitter_fraction_raises(self) -> None:
        with pytest.raises(ValueError, match="jitter_fraction"):
            ExtractionRetryPolicy(jitter_fraction=1.5)


class TestCircuitOpenErrorImported:
    def test_circuit_open_error_is_exception(self) -> None:
        err = CircuitOpenError("salesforce circuit open")
        assert isinstance(err, Exception)
        assert "salesforce" in str(err)
