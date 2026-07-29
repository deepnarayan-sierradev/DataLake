"""
Closed action registry, idempotency, and per-destination circuit breakers
(DL-WF-04, DL-WF-07, DL-WF-09).

Command pattern: every action carries an idempotency key derived from
`(workflow_id, execution_id, action_id)`, so a retry never sends a duplicate notification or
a duplicate write-back — retry semantics live in one place, not in each action.

A destination allowlist plus a per-destination circuit breaker means one dead webhook cannot
stall the engine, and no user-supplied URL is ever called (OWASP A05, A10).
"""

from __future__ import annotations

import abc
import hashlib
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError

from contracts.identifier_policy import validate_tenant_code
from observability.structured_logger import get_platform_logger
from workflow_automation.definition import ActionKind

_logger = get_platform_logger(__name__)

_IDEMPOTENCY_TABLE_NAME: Final[str] = "EdlWorkflowIdempotency"
_BREAKER_TABLE_NAME: Final[str] = "EdlWorkflowCircuitBreaker"

# Idempotency records outlive any plausible retry window without growing unbounded.
IDEMPOTENCY_TTL_SECONDS: Final[int] = 7 * 24 * 3_600

# Circuit breaker: N consecutive failures opens it for the cool-down period.
CIRCUIT_FAILURE_THRESHOLD: Final[int] = 5
CIRCUIT_COOLDOWN_SECONDS: Final[float] = 300.0


def idempotency_key(workflow_id: str, execution_id: str, action_id: str) -> str:
    """`(workflow, execution, action)` — the only derivation, so retries always collide."""
    raw = f"{workflow_id}#{execution_id}#{action_id}"
    return f"{action_id}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


class ActionOutcome(StrEnum):
    """What one action attempt produced."""

    EXECUTED = "executed"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    SKIPPED_DRY_RUN = "skipped_dry_run"
    FAILED = "failed"
    CIRCUIT_OPEN = "circuit_open"


@dataclass
class ActionResult:
    """The outcome plus whatever the action needs to report."""

    action_id: str
    kind: ActionKind
    outcome: ActionOutcome
    detail: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def is_failure(self) -> bool:
        return self.outcome in (ActionOutcome.FAILED, ActionOutcome.CIRCUIT_OPEN)


@dataclass
class ActionContext:
    """Everything an action handler may read; it never receives raw request input."""

    tenant_code: str
    workflow_id: str
    execution_id: str
    correlation_id: str
    environment: str
    trigger_context: dict[str, Any] = field(default_factory=dict)
    condition_values: dict[str, float] = field(default_factory=dict)
    dry_run: bool = False
    # Actions execute under the workflow owner's effective permissions, never an elevated
    # service identity (DL-WF-04, OWASP A01).
    acting_as: str = ""


class WorkflowActionHandler(abc.ABC):
    """Port every action kind implements."""

    kind: ActionKind

    @abc.abstractmethod
    def describe(self, parameters: dict[str, str]) -> str:
        """What this action *would* do — the dry-run output (DL-WF-10)."""
        raise NotImplementedError

    @abc.abstractmethod
    def execute(self, parameters: dict[str, str], context: ActionContext) -> dict[str, Any]:
        raise NotImplementedError

    def destination(self, parameters: dict[str, str]) -> str:
        """Circuit-breaker key; actions with no external destination share the default."""
        return f"{self.kind.value}"


class ActionRegistry:
    """The closed action set; a new action is a registration, not a code branch."""

    def __init__(self) -> None:
        self._handlers: dict[ActionKind, WorkflowActionHandler] = {}

    def register(self, handler: WorkflowActionHandler) -> None:
        if handler.kind in self._handlers:
            raise ValueError(f"An action handler for {handler.kind.value!r} is already registered.")
        self._handlers[handler.kind] = handler

    def resolve(self, kind: ActionKind) -> WorkflowActionHandler:
        handler = self._handlers.get(kind)
        if handler is None:
            raise KeyError(
                f"No action handler registered for {kind.value!r}. Registered: "
                f"{sorted(k.value for k in self._handlers)}."
            )
        return handler

    def registered_kinds(self) -> list[str]:
        return sorted(k.value for k in self._handlers)

    def reset(self) -> None:
        """Testing only."""
        self._handlers.clear()


action_registry: Final[ActionRegistry] = ActionRegistry()


class IdempotencyGuard:
    """Conditional-write guard that makes an action at-most-once per execution."""

    def __init__(self, region_name: str, table_name: str | None = None) -> None:
        resolved = (
            table_name or os.environ.get("WORKFLOW_IDEMPOTENCY_TABLE") or (_IDEMPOTENCY_TABLE_NAME)
        )
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(resolved)

    def claim(self, tenant_code: str, key: str) -> bool:
        """True when this caller won the claim; False when the action already ran."""
        expires_at = int(
            (datetime.now(UTC) + timedelta(seconds=IDEMPOTENCY_TTL_SECONDS)).timestamp()
        )
        try:
            self._table.put_item(
                Item={
                    "tenant_code": validate_tenant_code(tenant_code),
                    "idempotency_key": key,
                    "claimed_at": datetime.now(UTC).isoformat(),
                    "expires_at": expires_at,
                },
                ConditionExpression=(
                    "attribute_not_exists(tenant_code) AND attribute_not_exists(idempotency_key)"
                ),
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True


@dataclass
class CircuitBreakerState:
    """Per-destination failure state."""

    consecutive_failures: int = 0
    opened_at: float | None = None

    def is_open(self, now: float, cooldown_seconds: float) -> bool:
        if self.opened_at is None:
            return False
        if now - self.opened_at >= cooldown_seconds:
            # Half-open: allow one probe rather than staying open forever.
            self.opened_at = None
            self.consecutive_failures = 0
            return False
        return True


class DestinationCircuitBreaker:
    """
    One breaker per external destination, so a dead webhook is isolated (DL-WF-09).

    **State is process-local.** With concurrent or recycled Lambda containers each starts with a
    clean breaker, so the failure threshold is rarely reached across the fleet and a dead
    destination keeps being retried. The guarantee therefore holds *within one warm container*,
    not across the fleet — which is honest but weaker than the requirement implies.

    `DurableDestinationCircuitBreaker` below is the fleet-wide version; it shares this class's
    interface so the engine takes either. This one remains the default because a breaker that
    reads DynamoDB on every action adds a round trip to the common (healthy) path.
    """

    def __init__(
        self,
        failure_threshold: int = CIRCUIT_FAILURE_THRESHOLD,
        cooldown_seconds: float = CIRCUIT_COOLDOWN_SECONDS,
        monotonic: Any | None = None,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._monotonic = monotonic or time.monotonic
        self._states: dict[str, CircuitBreakerState] = {}

    def is_open(self, destination: str) -> bool:
        state = self._states.get(destination)
        return bool(state and state.is_open(self._monotonic(), self._cooldown_seconds))

    def record_success(self, destination: str) -> None:
        self._states.pop(destination, None)

    def record_failure(self, destination: str) -> bool:
        """Returns True when this failure opened the circuit."""
        state = self._states.setdefault(destination, CircuitBreakerState())
        state.consecutive_failures += 1
        if state.consecutive_failures >= self._failure_threshold and state.opened_at is None:
            state.opened_at = self._monotonic()
            _logger.warning(
                "workflow_destination_circuit_opened",
                destination=destination,
                consecutive_failures=state.consecutive_failures,
            )
            return True
        return False

    def open_destinations(self) -> list[str]:
        now = self._monotonic()
        return sorted(
            destination
            for destination, state in self._states.items()
            if state.is_open(now, self._cooldown_seconds)
        )


class DurableDestinationCircuitBreaker(DestinationCircuitBreaker):
    """
    Fleet-wide breaker: failure counts live in DynamoDB, keyed `(tenant_code, destination)`.

    Needed because the in-memory breaker cannot open under Lambda concurrency — every container
    starts at zero consecutive failures, so five failures spread across five containers never
    trips a five-failure threshold and a dead destination is retried indefinitely (DL-WF-09).

    Conditional-update counter, TTL-expired so a destination that recovers is not penalised
    forever. A DynamoDB failure degrades to the in-memory behaviour rather than failing the
    action: a breaker is a protection, and losing it must not become an outage.
    """

    def __init__(
        self,
        tenant_code: str,
        region_name: str,
        table_name: str | None = None,
        failure_threshold: int = CIRCUIT_FAILURE_THRESHOLD,
        cooldown_seconds: float = CIRCUIT_COOLDOWN_SECONDS,
    ) -> None:
        super().__init__(failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds)
        self._tenant_code = validate_tenant_code(tenant_code)
        resolved = table_name or os.environ.get("WORKFLOW_BREAKER_TABLE") or _BREAKER_TABLE_NAME
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(resolved)

    def is_open(self, destination: str) -> bool:
        if super().is_open(destination):
            return True
        try:
            response = self._table.get_item(
                Key={"tenant_code": self._tenant_code, "destination": destination}
            )
        except ClientError as exc:
            _logger.warning("durable_circuit_read_failed", destination=destination, error=str(exc))
            return False
        item = response.get("Item")
        if not item:
            return False
        # DynamoDB returns a broad union; narrow before arithmetic.
        raw_opened = item.get("opened_at")
        opened_at = float(str(raw_opened)) if raw_opened is not None else 0.0
        if not opened_at:
            return False
        return (time.time() - opened_at) < self._cooldown_seconds

    def record_success(self, destination: str) -> None:
        super().record_success(destination)
        try:
            self._table.delete_item(
                Key={"tenant_code": self._tenant_code, "destination": destination}
            )
        except ClientError as exc:
            _logger.warning("durable_circuit_clear_failed", destination=destination, error=str(exc))

    def record_failure(self, destination: str) -> bool:
        opened_locally = super().record_failure(destination)
        expires_at = int(time.time() + self._cooldown_seconds * 10)
        try:
            updated = self._table.update_item(
                Key={"tenant_code": self._tenant_code, "destination": destination},
                UpdateExpression=("ADD consecutive_failures :one SET expires_at = :ttl"),
                ExpressionAttributeValues={":one": 1, ":ttl": expires_at},
                ReturnValues="UPDATED_NEW",
            )
        except ClientError as exc:
            _logger.warning(
                "durable_circuit_increment_failed", destination=destination, error=str(exc)
            )
            return opened_locally

        raw_failures = updated.get("Attributes", {}).get("consecutive_failures")
        failures = int(str(raw_failures)) if raw_failures is not None else 0
        if failures < self._failure_threshold:
            return opened_locally
        try:
            self._table.update_item(
                Key={"tenant_code": self._tenant_code, "destination": destination},
                UpdateExpression="SET opened_at = :now",
                # Only the failure that crosses the threshold opens it; a later one must not
                # extend the cooldown indefinitely and strand a recovered destination.
                ConditionExpression="attribute_not_exists(opened_at)",
                ExpressionAttributeValues={":now": int(time.time())},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                _logger.warning(
                    "durable_circuit_open_failed", destination=destination, error=str(exc)
                )
            return opened_locally
        _logger.warning(
            "workflow_destination_circuit_opened_fleet_wide",
            tenant_code=self._tenant_code,
            destination=destination,
            consecutive_failures=failures,
        )
        return True
