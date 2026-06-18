"""
Source onboarding registry.

Enforces the mandatory gate sequence for onboarding a new data source.
New sources must pass all gates before extraction can be activated.

Gate sequence (spec §10.2):
  1. SOURCE_REGISTRATION      — source_id, owner, SLA, data classification
  2. CREDENTIAL_REGISTRATION  — Secrets Manager entry confirmed + rotation schedule
  3. ENTITY_MAPPING           — at least one entity config record exists
  4. EXTRACTION_PROFILE       — dry-run in dev succeeded; schema snapshot captured
  5. SECURITY_GOVERNANCE      — security review + classification policy confirmed
  6. ACCEPTANCE_VALIDATION    — canary run passed; record counts and quality checks confirmed

Security (OWASP A01, A02):
  - Onboarding records stored in DynamoDB with write access restricted to
    governance service role only.
  - source_id validated against stable-identifier pattern before any persistence.
  - All gate state transitions are immutably logged (audit trail).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

import boto3

from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_STABLE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9\-]{1,63}$")

# Minimum character length for waiver justification notes (OWASP A01).
# A short note like "ok" is not a meaningful audit record; require a real rationale.
_WAIVER_MIN_NOTES_LEN: Final[int] = 20

# Gate ordering — a source cannot advance past gate N without passing gates 1..N
_GATE_ORDER: Final[tuple[OnboardingGate, ...]] = ()  # populated after StrEnum defined


class OnboardingGate(StrEnum):
    SOURCE_REGISTRATION = "source_registration"
    CREDENTIAL_REGISTRATION = "credential_registration"
    ENTITY_MAPPING = "entity_mapping"
    EXTRACTION_PROFILE = "extraction_profile"
    SECURITY_GOVERNANCE = "security_governance"
    ACCEPTANCE_VALIDATION = "acceptance_validation"


_GATE_ORDER_LIST: Final[list[OnboardingGate]] = [
    OnboardingGate.SOURCE_REGISTRATION,
    OnboardingGate.CREDENTIAL_REGISTRATION,
    OnboardingGate.ENTITY_MAPPING,
    OnboardingGate.EXTRACTION_PROFILE,
    OnboardingGate.SECURITY_GOVERNANCE,
    OnboardingGate.ACCEPTANCE_VALIDATION,
]


class OnboardingGateStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    WAIVED = "waived"  # waiver requires explicit justification


@dataclass(frozen=True)
class SourceRegistrationRecord:
    """Complete metadata for a registered source."""

    source_id: str
    owner: str
    sla_tier: str  # e.g., "critical", "standard", "best_effort"
    data_classification: str
    environment: str
    registered_at: str  # ISO-8601 UTC


@dataclass(frozen=True)
class GateTransitionRecord:
    """Immutable record of a gate status change."""

    source_id: str
    gate: OnboardingGate
    new_status: OnboardingGateStatus
    reviewer: str
    notes: str
    transitioned_at: str  # ISO-8601 UTC


@dataclass(frozen=True)
class SourceOnboardingState:
    """Current onboarding state for one source."""

    source_id: str
    environment: str
    gate_statuses: dict[str, str]  # gate.value → status.value
    is_activation_permitted: bool
    last_updated_at: str


class SourceOnboardingRegistryClient:
    """
    DynamoDB-backed registry that tracks source onboarding gate progress.

    Table name: {environment}-source-onboarding-registry
    PK: source_id (string)
    """

    def __init__(self, environment: str, region_name: str) -> None:
        self._environment = environment
        self._region_name = region_name
        self._table_name = f"{environment}-source-onboarding-registry"
        self._dynamodb: Any = boto3.resource("dynamodb", region_name=region_name)

    def register_source(
        self,
        source_id: str,
        owner: str,
        sla_tier: str,
        data_classification: str,
    ) -> SourceRegistrationRecord:
        """
        Register a new source and initialise all gates to PENDING.

        Raises:
            OnboardingValidationError if source_id is invalid or already registered.
        """
        if not _STABLE_ID_PATTERN.match(source_id):
            raise OnboardingValidationError(
                f"source_id {source_id!r} does not match stable identifier pattern"
            )

        registered_at = datetime.now(UTC).isoformat()

        item: dict[str, Any] = {
            "source_id": source_id,
            "owner": owner,
            "sla_tier": sla_tier,
            "data_classification": data_classification,
            "environment": self._environment,
            "registered_at": registered_at,
            "last_updated_at": registered_at,
        }
        # Initialise all gates to PENDING
        for gate in _GATE_ORDER_LIST:
            item[f"gate_{gate.value}"] = OnboardingGateStatus.PENDING.value

        table = self._dynamodb.Table(self._table_name)
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(source_id)",
        )

        _logger.info(
            "source_registered",
            source_id=source_id,
            environment=self._environment,
            owner=owner,
        )

        return SourceRegistrationRecord(
            source_id=source_id,
            owner=owner,
            sla_tier=sla_tier,
            data_classification=data_classification,
            environment=self._environment,
            registered_at=registered_at,
        )

    def advance_gate(
        self,
        source_id: str,
        gate: OnboardingGate,
        status: OnboardingGateStatus,
        reviewer: str,
        notes: str = "",
    ) -> GateTransitionRecord:
        """
        Advance a gate to the given status.

        Rules:
          - PASSED gates can only be set after all prior gates are PASSED or WAIVED.
          - FAILED and WAIVED can be set at any time.
          - WAIVED requires a non-empty notes justification.

        Raises:
            OnboardingValidationError on rule violation.
            OnboardingSourceNotFoundError if source is not registered.
        """
        if status == OnboardingGateStatus.WAIVED and not notes.strip():
            raise OnboardingValidationError("Waiver requires a non-empty notes justification")

        if status == OnboardingGateStatus.WAIVED and len(notes.strip()) < _WAIVER_MIN_NOTES_LEN:
            raise OnboardingValidationError(
                f"Waiver notes must be at least {_WAIVER_MIN_NOTES_LEN} characters to constitute "
                f"a meaningful audit record; received {len(notes.strip())} characters."
            )

        # Emit a warning-level log for every waiver so operations teams can
        # detect suspicious waiver activity via CloudWatch Logs Insights (OWASP A01).
        if status == OnboardingGateStatus.WAIVED:
            _logger.warning(
                "onboarding_gate_waived",
                source_id=source_id,
                gate=gate.value,
                reviewer=reviewer,
                notes_length=len(notes.strip()),
            )

        current_state = self.get_state(source_id)
        gate_statuses = current_state.gate_statuses

        if status == OnboardingGateStatus.PASSED:
            gate_idx = _GATE_ORDER_LIST.index(gate)
            for prior_gate in _GATE_ORDER_LIST[:gate_idx]:
                prior_status = gate_statuses.get(
                    prior_gate.value, OnboardingGateStatus.PENDING.value
                )
                if prior_status not in (
                    OnboardingGateStatus.PASSED.value,
                    OnboardingGateStatus.WAIVED.value,
                ):
                    raise OnboardingValidationError(
                        f"Cannot pass gate {gate.value!r}: "
                        f"prior gate {prior_gate.value!r} is {prior_status!r}"
                    )

        transitioned_at = datetime.now(UTC).isoformat()
        table = self._dynamodb.Table(self._table_name)
        table.update_item(
            Key={"source_id": source_id},
            UpdateExpression=(f"SET gate_{gate.value} = :status, last_updated_at = :ts"),
            ExpressionAttributeValues={":status": status.value, ":ts": transitioned_at},
        )

        _logger.info(
            "onboarding_gate_advanced",
            source_id=source_id,
            gate=gate.value,
            new_status=status.value,
            reviewer=reviewer,
        )

        return GateTransitionRecord(
            source_id=source_id,
            gate=gate,
            new_status=status,
            reviewer=reviewer,
            notes=notes,
            transitioned_at=transitioned_at,
        )

    def get_state(self, source_id: str) -> SourceOnboardingState:
        """
        Return the current onboarding state for a source.

        Raises OnboardingSourceNotFoundError if not registered.
        """
        table = self._dynamodb.Table(self._table_name)
        response = table.get_item(Key={"source_id": source_id})
        item = response.get("Item")
        if item is None:
            raise OnboardingSourceNotFoundError(source_id)

        gate_statuses = {
            gate.value: str(item.get(f"gate_{gate.value}", OnboardingGateStatus.PENDING.value))
            for gate in _GATE_ORDER_LIST
        }

        is_activation_permitted = all(
            gate_statuses[g.value]
            in (
                OnboardingGateStatus.PASSED.value,
                OnboardingGateStatus.WAIVED.value,
            )
            for g in _GATE_ORDER_LIST
        )

        return SourceOnboardingState(
            source_id=source_id,
            environment=self._environment,
            gate_statuses=gate_statuses,
            is_activation_permitted=is_activation_permitted,
            last_updated_at=str(item.get("last_updated_at", "")),
        )

    def is_source_activation_permitted(self, source_id: str) -> bool:
        """
        Convenience method: True if all gates are PASSED or WAIVED.
        Returns False (not raises) if source is not registered.
        """
        try:
            return self.get_state(source_id).is_activation_permitted
        except OnboardingSourceNotFoundError:
            return False


class OnboardingValidationError(Exception):
    """Raised when an onboarding gate transition violates business rules."""


class OnboardingSourceNotFoundError(Exception):
    def __init__(self, source_id: str) -> None:
        super().__init__(f"Source not registered in onboarding registry: {source_id!r}")
