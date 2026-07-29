"""
The workflow engine (DL-WF-03, 07, 08, 09, 10) and its execution history.

An interpreter over the declarative definition, not a code generator. Conditions
short-circuit (chain of responsibility); actions are commands with idempotency keys; failures
retry with backoff then dead-letter; and a dry run reports the actions it *would* take without
performing them — the only safe way to let business users author automation against
production data.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

import boto3

from contracts.identifier_policy import validate_tenant_code
from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger
from workflow_automation.action_registry import (
    ActionContext,
    ActionOutcome,
    ActionResult,
    DestinationCircuitBreaker,
    IdempotencyGuard,
    action_registry,
    idempotency_key,
)
from workflow_automation.definition import (
    WorkflowAction,
    WorkflowCondition,
    WorkflowDefinition,
    WorkflowStatus,
)

_logger = get_platform_logger(__name__)

_EXECUTION_TABLE_NAME: Final[str] = "EdlWorkflowExecution"


class ExecutionStatus(StrEnum):
    """Terminal and in-flight execution states."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    CONDITIONS_NOT_MET = "conditions_not_met"
    PARTIALLY_FAILED = "partially_failed"
    FAILED = "failed"
    DRY_RUN = "dry_run"
    SKIPPED_NOT_PUBLISHED = "skipped_not_published"


class WorkflowDisabledError(Exception):
    """Raised when an execution is attempted against a non-published definition."""


@dataclass
class ConditionEvaluation:
    """One condition, its observed value, and whether it held."""

    metric: str
    operator: str
    threshold: float
    observed: float | None
    passed: bool
    detail: str = ""


@dataclass
class WorkflowExecution:
    """One execution's full record: trigger context, conditions, actions, outcomes."""

    tenant_code: str
    workflow_id: str
    workflow_version: str
    execution_id: str
    status: ExecutionStatus
    correlation_id: str
    trigger_context: dict[str, Any] = field(default_factory=dict)
    condition_evaluations: list[ConditionEvaluation] = field(default_factory=list)
    action_results: list[ActionResult] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str | None = None
    duration_ms: int = 0
    dry_run: bool = False

    @property
    def sort_key(self) -> str:
        return f"{self.workflow_id}#{self.execution_id}"

    @property
    def failed_actions(self) -> list[ActionResult]:
        return [r for r in self.action_results if r.is_failure]


MetricResolver = Any
"""Callable (tenant_code, condition) -> tuple[float | None, float | None] (observed, previous)."""


class WorkflowEngine:
    """Evaluates conditions and executes actions for one workflow definition."""

    def __init__(
        self,
        environment: str,
        region_name: str,
        metric_resolver: MetricResolver,
        # Required: optional at-most-once is no at-most-once. Pass None explicitly only for a
        # dry-run engine that performs no external effect (DL-WF-07).
        idempotency_guard: IdempotencyGuard | None,
        circuit_breaker: DestinationCircuitBreaker | None = None,
    ) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        self._metric_resolver = metric_resolver
        self._idempotency = idempotency_guard
        self._breaker = circuit_breaker or DestinationCircuitBreaker()
        table_name = os.environ.get("WORKFLOW_EXECUTION_TABLE") or _EXECUTION_TABLE_NAME
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)

    # ── Execution ─────────────────────────────────────────────────────────────

    def execute(
        self,
        definition: WorkflowDefinition,
        *,
        trigger_context: dict[str, Any] | None = None,
        correlation_id: str = "",
        dry_run: bool = False,
        execution_id: str | None = None,
    ) -> WorkflowExecution:
        """
        Run one execution, pinned to `definition`'s version for its whole life.

        A workflow that changes mid-execution is exactly the ambiguity `DL-CFG-01` prevents,
        so the caller resolves the version once and the engine never re-reads it.
        """
        validate_tenant_code(definition.tenant_code)
        resolved_execution_id = execution_id or f"wex-{uuid.uuid4().hex[:12]}"
        execution = WorkflowExecution(
            tenant_code=definition.tenant_code,
            workflow_id=definition.workflow_id,
            workflow_version=definition.version,
            execution_id=resolved_execution_id,
            status=ExecutionStatus.DRY_RUN if dry_run else ExecutionStatus.RUNNING,
            correlation_id=correlation_id or resolved_execution_id,
            trigger_context=dict(trigger_context or {}),
            dry_run=dry_run,
        )

        if definition.status is not WorkflowStatus.PUBLISHED and not dry_run:
            execution.status = ExecutionStatus.SKIPPED_NOT_PUBLISHED
            self._persist(execution)
            raise WorkflowDisabledError(
                f"Workflow {definition.workflow_id!r} is {definition.status.value!r}; only a "
                "published definition executes. A dry run is permitted on a draft."
            )

        started = datetime.now(UTC)
        conditions_hold = self._evaluate_conditions(definition, execution)
        if not conditions_hold:
            execution.status = ExecutionStatus.CONDITIONS_NOT_MET
            self._finish(execution, started)
            return execution

        context = ActionContext(
            tenant_code=definition.tenant_code,
            workflow_id=definition.workflow_id,
            execution_id=execution.execution_id,
            correlation_id=execution.correlation_id,
            environment=self._environment,
            trigger_context=execution.trigger_context,
            condition_values={
                e.metric: e.observed
                for e in execution.condition_evaluations
                if e.observed is not None
            },
            dry_run=dry_run,
            acting_as=definition.owner,
        )

        for action in definition.actions:
            execution.action_results.append(self._run_action(action, context))

        if execution.failed_actions and definition.on_failure_actions:
            for action in definition.on_failure_actions:
                execution.action_results.append(self._run_action(action, context))

        execution.status = self._terminal_status(execution, definition, dry_run)
        record_platform_metric(
            PlatformMetric.WORKFLOW_EXECUTIONS, 1.0, Status=execution.status.value
        )
        self._finish(execution, started)
        return execution

    def dry_run(
        self,
        definition: WorkflowDefinition,
        *,
        trigger_context: dict[str, Any] | None = None,
        correlation_id: str = "",
    ) -> WorkflowExecution:
        """Evaluate conditions for real; report actions without performing them (DL-WF-10)."""
        return self.execute(
            definition,
            trigger_context=trigger_context,
            correlation_id=correlation_id,
            dry_run=True,
        )

    # ── Conditions ────────────────────────────────────────────────────────────

    def _evaluate_conditions(
        self, definition: WorkflowDefinition, execution: WorkflowExecution
    ) -> bool:
        """Short-circuits on the first failing condition (chain of responsibility)."""
        for condition in definition.conditions:
            evaluation = self._evaluate_condition(definition.tenant_code, condition)
            record_platform_metric(PlatformMetric.WORKFLOW_CONDITION_EVALUATIONS)
            execution.condition_evaluations.append(evaluation)
            if not evaluation.passed:
                return False
        return True

    def _evaluate_condition(
        self, tenant_code: str, condition: WorkflowCondition
    ) -> ConditionEvaluation:
        try:
            observed, previous = self._metric_resolver(tenant_code, condition)
        except Exception as exc:
            # A condition that cannot be measured must not fire an action — fail closed.
            return ConditionEvaluation(
                metric=condition.metric,
                operator=condition.operator.value,
                threshold=condition.threshold,
                observed=None,
                passed=False,
                detail=f"condition could not be evaluated: {type(exc).__name__}",
            )
        if observed is None:
            return ConditionEvaluation(
                metric=condition.metric,
                operator=condition.operator.value,
                threshold=condition.threshold,
                observed=None,
                passed=False,
                detail="semantic query returned no value",
            )
        passed = condition.evaluate(float(observed), previous)
        return ConditionEvaluation(
            metric=condition.metric,
            operator=condition.operator.value,
            threshold=condition.threshold,
            observed=float(observed),
            passed=passed,
        )

    # ── Actions ───────────────────────────────────────────────────────────────

    def _run_action(self, action: WorkflowAction, context: ActionContext) -> ActionResult:
        try:
            handler = action_registry.resolve(action.kind)
        except KeyError as exc:
            return ActionResult(
                action_id=action.action_id,
                kind=action.kind,
                outcome=ActionOutcome.FAILED,
                detail=str(exc),
            )

        if context.dry_run:
            return ActionResult(
                action_id=action.action_id,
                kind=action.kind,
                outcome=ActionOutcome.SKIPPED_DRY_RUN,
                detail=handler.describe(action.parameters),
            )

        destination = handler.destination(action.parameters)
        if self._breaker.is_open(destination):
            return ActionResult(
                action_id=action.action_id,
                kind=action.kind,
                outcome=ActionOutcome.CIRCUIT_OPEN,
                detail=f"circuit open for destination {destination!r}",
            )

        key = idempotency_key(context.workflow_id, context.execution_id, action.action_id)
        if self._idempotency is not None and not self._idempotency.claim(context.tenant_code, key):
            return ActionResult(
                action_id=action.action_id,
                kind=action.kind,
                outcome=ActionOutcome.SKIPPED_DUPLICATE,
                detail=f"idempotency key {key!r} already claimed",
            )

        last_error = ""
        for attempt in range(action.retry_limit + 1):
            try:
                payload = handler.execute(action.parameters, context)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                _logger.warning(
                    "workflow_action_attempt_failed",
                    workflow_id=context.workflow_id,
                    execution_id=context.execution_id,
                    action_id=action.action_id,
                    attempt=attempt + 1,
                    error=last_error,
                )
                continue
            self._breaker.record_success(destination)
            record_platform_metric(
                PlatformMetric.WORKFLOW_ACTIONS_EXECUTED, 1.0, ActionType=action.kind.value
            )
            return ActionResult(
                action_id=action.action_id,
                kind=action.kind,
                outcome=ActionOutcome.EXECUTED,
                payload=payload,
            )

        record_platform_metric(
            PlatformMetric.WORKFLOW_ACTION_FAILURES, 1.0, ActionType=action.kind.value
        )
        if self._breaker.record_failure(destination):
            record_platform_metric(
                PlatformMetric.WORKFLOW_CIRCUIT_BREAKER_OPEN, 1.0, ActionType=action.kind.value
            )
        return ActionResult(
            action_id=action.action_id,
            kind=action.kind,
            outcome=ActionOutcome.FAILED,
            detail=f"exhausted {action.retry_limit + 1} attempt(s): {last_error}",
        )

    @staticmethod
    def _terminal_status(
        execution: WorkflowExecution, definition: WorkflowDefinition, dry_run: bool
    ) -> ExecutionStatus:
        if dry_run:
            return ExecutionStatus.DRY_RUN
        failed = execution.failed_actions
        if not failed:
            return ExecutionStatus.SUCCEEDED
        blocking = [r for r in failed if not definition.action(r.action_id).continue_on_failure]
        if len(blocking) == len(definition.actions):
            return ExecutionStatus.FAILED
        return ExecutionStatus.PARTIALLY_FAILED

    # ── History ───────────────────────────────────────────────────────────────

    def _finish(self, execution: WorkflowExecution, started: datetime) -> None:
        execution.completed_at = datetime.now(UTC).isoformat()
        execution.duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        self._persist(execution)

    def _persist(self, execution: WorkflowExecution) -> None:
        """Execution history is an audit record, so it is written even for a dry run."""
        self._table.put_item(
            Item={
                "tenant_code": execution.tenant_code,
                "execution_key": execution.sort_key,
                "workflow_id": execution.workflow_id,
                "workflow_version": execution.workflow_version,
                "execution_id": execution.execution_id,
                "status": execution.status.value,
                "status_started_at": f"{execution.status.value}#{execution.started_at}",
                "correlation_id": execution.correlation_id,
                "trigger_context": json.dumps(execution.trigger_context, separators=(",", ":")),
                "condition_evaluations": [
                    {
                        "metric": e.metric,
                        "operator": e.operator,
                        "threshold": json.dumps(e.threshold),
                        "observed": json.dumps(e.observed),
                        "passed": e.passed,
                        "detail": e.detail,
                    }
                    for e in execution.condition_evaluations
                ],
                "action_results": [
                    {
                        "action_id": r.action_id,
                        "kind": r.kind.value,
                        "outcome": r.outcome.value,
                        "detail": r.detail,
                    }
                    for r in execution.action_results
                ],
                "started_at": execution.started_at,
                "completed_at": execution.completed_at,
                "duration_ms": execution.duration_ms,
                "dry_run": execution.dry_run,
                "environment": self._environment,
            }
        )

    def list_executions(self, tenant_code: str, workflow_id: str) -> list[dict[str, Any]]:
        tenant_code = validate_tenant_code(tenant_code)
        response = self._table.query(
            KeyConditionExpression="tenant_code = :tc AND begins_with(execution_key, :wf)",
            ExpressionAttributeValues={":tc": tenant_code, ":wf": f"{workflow_id}#"},
        )
        return [dict(item) for item in response.get("Items", [])]


def batch_scheduled_workflows(
    definitions: list[WorkflowDefinition],
) -> dict[str, list[WorkflowDefinition]]:
    """
    Group schedule-triggered workflows by cron expression (DL-06 performance clause).

    A hundred workflows on the same cron become one evaluation pass, not a hundred concurrent
    executions.
    """
    batches: dict[str, list[WorkflowDefinition]] = {}
    for definition in definitions:
        expression = definition.trigger.cron_expression
        if expression:
            batches.setdefault(expression, []).append(definition)
    return batches
