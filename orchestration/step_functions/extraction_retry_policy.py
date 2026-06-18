"""
Extraction retry policy for the Enterprise Data Lake platform.

Implements:
  - Transient-error retry eligibility with configurable attempt limits
  - Exponential back-off with uniform jitter (avoids thundering-herd)
  - Per-source circuit breaker: halts extraction for a source after too many
    consecutive failures, protecting the source and the platform control plane

Classification routing:
  - TRANSIENT_TIMEOUT, TRANSIENT_THROTTLE, TRANSIENT_NETWORK → retry-eligible
  - DETERMINISTIC_* → fail-fast immediately; no retry
  - UNKNOWN → fail-fast; manual review via DLQ

Security:
  - No sensitive data flows through this module.
  - Circuit state is in-process memory only — it resets on process restart.
    For distributed deployments, extend with a DynamoDB-backed counter.

OWASP A10 — Mishandling of Exceptional Conditions:
  - Deterministic failures never trigger retries, preventing infinite retry loops
    that could mask root causes or enable brute-force credential attacks.
"""

from __future__ import annotations

import random
import threading
from typing import Final

from connector_runtime.interfaces.connector_interface import ExtractionErrorClassification
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_TRANSIENT_CLASSIFICATIONS: Final[frozenset[ExtractionErrorClassification]] = frozenset(
    {
        ExtractionErrorClassification.TRANSIENT_TIMEOUT,
        ExtractionErrorClassification.TRANSIENT_THROTTLE,
        ExtractionErrorClassification.TRANSIENT_NETWORK,
    }
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CircuitOpenError(Exception):
    """
    Raised when the circuit breaker is open for a source_id.

    Indicates that the source has exceeded the consecutive failure threshold.
    The circuit must be reset (manually or after a cool-down period) before
    extraction resumes for that source.
    """


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class ExtractionRetryPolicy:
    """
    Retry decision and back-off delay computation for extraction runs.

    One instance should be shared across runs for the same process so the
    circuit-breaker state accumulates correctly.

    Thread-safety: the circuit breaker state dict is protected by an internal
    threading.Lock, making this class safe for use from multiple threads.
    Circuit breaker keys are scoped to (source_id, entity_id) so that failures
    for one entity do not block extraction of other entities from the same source.
    """

    def __init__(
        self,
        max_transient_attempts: int = 3,
        base_delay_seconds: float = 1.0,
        max_delay_seconds: float = 60.0,
        backoff_multiplier: float = 2.0,
        circuit_open_threshold: int = 5,
        jitter_fraction: float = 0.25,
    ) -> None:
        if max_transient_attempts < 1:
            raise ValueError("max_transient_attempts must be >= 1.")
        if base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds must be > 0.")
        if max_delay_seconds < base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds.")
        if backoff_multiplier < 1.0:
            raise ValueError("backoff_multiplier must be >= 1.0.")
        if circuit_open_threshold < 1:
            raise ValueError("circuit_open_threshold must be >= 1.")
        if not 0.0 <= jitter_fraction <= 1.0:
            raise ValueError("jitter_fraction must be in [0.0, 1.0].")

        self._max_transient_attempts = max_transient_attempts
        self._base_delay = base_delay_seconds
        self._max_delay = max_delay_seconds
        self._backoff_multiplier = backoff_multiplier
        self._circuit_open_threshold = circuit_open_threshold
        self._jitter_fraction = jitter_fraction
        # Mutable circuit-breaker state: circuit_key → consecutive failure count
        # Protected by _lock for thread safety.
        self._consecutive_failures: dict[str, int] = {}
        self._lock = threading.Lock()

    # ── Retry eligibility ───────────────────────────────────────────────────

    def is_retryable(self, classification: ExtractionErrorClassification) -> bool:
        """Return True when the classification permits a retry attempt."""
        return classification in _TRANSIENT_CLASSIFICATIONS

    def should_retry(
        self,
        classification: ExtractionErrorClassification,
        attempt: int,
    ) -> bool:
        """
        Return True when another extraction attempt should be made.

        Parameters
        ----------
        classification : ExtractionErrorClassification
            Classification of the most-recent failure.
        attempt : int
            1-based attempt number (1 = first attempt just failed).

        Returns True only when the classification is transient AND the attempt
        number is below the maximum allowed.  Deterministic and UNKNOWN errors
        always return False regardless of attempt count.
        """
        if not self.is_retryable(classification):
            return False
        return attempt < self._max_transient_attempts

    # ── Delay computation ───────────────────────────────────────────────────

    def compute_delay_seconds(self, attempt: int) -> float:
        """
        Compute the back-off delay for the next attempt.

        Uses exponential back-off capped at max_delay_seconds with uniform
        random jitter in the range [-jitter_fraction, +jitter_fraction] of
        the computed delay to prevent thundering-herd effects.

        Parameters
        ----------
        attempt : int
            1-based attempt number (attempt=1 → first retry delay).
        """
        raw_delay = min(
            self._base_delay * (self._backoff_multiplier ** (attempt - 1)),
            self._max_delay,
        )
        # S311 suppressed: this is a non-cryptographic use of random for jitter.
        jitter = raw_delay * self._jitter_fraction * random.uniform(-1.0, 1.0)  # noqa: S311
        return max(0.0, raw_delay + jitter)

    # ── Circuit breaker ─────────────────────────────────────────────────────

    @staticmethod
    def _circuit_key(source_id: str, entity_id: str = "") -> str:
        """Return the dict key for circuit breaker state.

        Scoping to (source_id, entity_id) prevents a failing entity from
        blocking extraction of healthy entities from the same source.
        """
        return f"{source_id}:{entity_id}"

    def record_failure(self, source_id: str, entity_id: str = "") -> None:
        """Increment the consecutive failure counter for source_id:entity_id."""
        key = self._circuit_key(source_id, entity_id)
        with self._lock:
            self._consecutive_failures[key] = self._consecutive_failures.get(key, 0) + 1
            count = self._consecutive_failures[key]
        _logger.info(
            "circuit_breaker_failure_recorded",
            source_id=source_id,
            entity_id=entity_id,
            consecutive_failures=count,
            circuit_open_threshold=self._circuit_open_threshold,
            circuit_open=count >= self._circuit_open_threshold,
        )

    def record_success(self, source_id: str, entity_id: str = "") -> None:
        """Reset the consecutive failure counter for source_id:entity_id on success."""
        key = self._circuit_key(source_id, entity_id)
        with self._lock:
            self._consecutive_failures[key] = 0

    def is_circuit_open(self, source_id: str, entity_id: str = "") -> bool:
        """True when consecutive failures for source_id:entity_id meet or exceed the threshold."""
        key = self._circuit_key(source_id, entity_id)
        with self._lock:
            return self._consecutive_failures.get(key, 0) >= self._circuit_open_threshold

    def reset_circuit(self, source_id: str, entity_id: str = "") -> None:
        """Manually reset the circuit breaker state for source_id:entity_id."""
        key = self._circuit_key(source_id, entity_id)
        with self._lock:
            self._consecutive_failures[key] = 0
        _logger.info("circuit_breaker_reset", source_id=source_id, entity_id=entity_id)

    def consecutive_failures(self, source_id: str, entity_id: str = "") -> int:
        """Return the current consecutive failure count for source_id:entity_id."""
        key = self._circuit_key(source_id, entity_id)
        with self._lock:
            return self._consecutive_failures.get(key, 0)

    @property
    def max_transient_attempts(self) -> int:
        """Maximum retry attempts for transient errors."""
        return self._max_transient_attempts

    @property
    def circuit_open_threshold(self) -> int:
        """Consecutive failure count at which the circuit opens."""
        return self._circuit_open_threshold
