"""
Declarative workflow definitions (DL-WF-01 … DL-WF-04, DL-WF-10).

Versioned JSON per tenant describing `trigger`, `conditions`, `actions`, `on_failure`, and
`escalation`. Authored in the console, validated at publish, executed by the engine — no code
deploy to add a workflow.

The condition grammar is a **closed set of comparisons over semantic results**. There is no
expression language and no `eval`: that is a security decision as much as a design one
(OWASP A03, A04). Likewise the action set is a closed registry, so a workflow cannot execute
arbitrary code.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from contracts.identifier_policy import validate_tenant_code

WORKFLOW_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9\-]{1,63}$")
ACTION_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9\-_]{1,63}$")

MAX_ACTIONS_PER_WORKFLOW: Final[int] = 20
MAX_CONDITIONS_PER_WORKFLOW: Final[int] = 10


class TriggerKind(StrEnum):
    """Trigger types (DL-WF-02). `ML_SIGNAL` has no producer while DL-05 is deferred."""

    SCHEDULE = "schedule"
    PIPELINE_EVENT = "pipeline_event"
    DATA_CONDITION = "data_condition"
    ML_SIGNAL = "ml_signal"
    MANUAL = "manual"
    API_WEBHOOK = "api_webhook"


class PipelineEventKind(StrEnum):
    """Pipeline events the engine consumes from EventBridge and SQS."""

    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    QUALITY_GATE_BLOCKED = "quality_gate_blocked"
    RECONCILIATION_VARIANCE = "reconciliation_variance"
    EXCEPTION_RAISED = "exception_raised"


class ComparisonOperator(StrEnum):
    """The closed comparison set; deliberately no arithmetic and no string functions."""

    GREATER_THAN = "gt"
    GREATER_OR_EQUAL = "gte"
    LESS_THAN = "lt"
    LESS_OR_EQUAL = "lte"
    EQUALS = "eq"
    NOT_EQUALS = "ne"
    CHANGED_BY_PCT_ABOVE = "changed_by_pct_above"


class ActionKind(StrEnum):
    """Action types (DL-WF-04); each maps to a registered `WorkflowAction`."""

    SEND_NOTIFICATION = "send_notification"
    GENERATE_REPORT = "generate_report"
    CREATE_APPROVAL_TASK = "create_approval_task"
    WRITE_EXCEPTION = "write_exception"
    INVOKE_PIPELINE_RUN = "invoke_pipeline_run"
    INVOKE_CONNECTOR_WRITEBACK = "invoke_connector_writeback"
    CALL_OUTBOUND_WEBHOOK = "call_outbound_webhook"
    RUN_SAVED_QUERY = "run_saved_query"


EXTERNAL_EFFECT_ACTIONS: Final[frozenset[ActionKind]] = frozenset(
    {
        ActionKind.SEND_NOTIFICATION,
        ActionKind.GENERATE_REPORT,
        ActionKind.INVOKE_CONNECTOR_WRITEBACK,
        ActionKind.CALL_OUTBOUND_WEBHOOK,
    }
)


class WorkflowStatus(StrEnum):
    """Definition lifecycle; matches the DL-11 publish contract."""

    DRAFT = "draft"
    PUBLISHED = "published"
    DISABLED = "disabled"


class WorkflowTrigger(BaseModel):
    """What starts an execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: TriggerKind
    cron_expression: str | None = None
    pipeline_event: PipelineEventKind | None = None
    entity_id: str | None = None
    saved_query_id: str | None = None

    @model_validator(mode="after")
    def _validate_kind_requirements(self) -> WorkflowTrigger:
        if self.kind is TriggerKind.SCHEDULE and not self.cron_expression:
            raise ValueError("A schedule trigger requires a cron_expression.")
        if self.kind is TriggerKind.PIPELINE_EVENT and self.pipeline_event is None:
            raise ValueError("A pipeline_event trigger must name the event.")
        if self.kind is TriggerKind.DATA_CONDITION and not self.saved_query_id:
            raise ValueError(
                "A data_condition trigger must name the saved query whose result it watches — "
                "a threshold has to be measured against something the semantic layer defines."
            )
        return self


class WorkflowCondition(BaseModel):
    """A semantic query result compared against a threshold (DL-WF-03)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric: str
    operator: ComparisonOperator
    threshold: float
    entity: str | None = None
    saved_query_id: str | None = None

    @field_validator("metric")
    @classmethod
    def _validate_metric(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A condition must name a metric.")
        return value

    def evaluate(self, observed: float, previous: float | None = None) -> bool:
        """Compare an observed semantic result against the threshold."""
        if self.operator is ComparisonOperator.GREATER_THAN:
            return observed > self.threshold
        if self.operator is ComparisonOperator.GREATER_OR_EQUAL:
            return observed >= self.threshold
        if self.operator is ComparisonOperator.LESS_THAN:
            return observed < self.threshold
        if self.operator is ComparisonOperator.LESS_OR_EQUAL:
            return observed <= self.threshold
        if self.operator is ComparisonOperator.EQUALS:
            return observed == self.threshold
        if self.operator is ComparisonOperator.NOT_EQUALS:
            return observed != self.threshold
        if previous is None or previous == 0:
            return False
        change_pct = abs(observed - previous) / abs(previous) * 100
        return change_pct > self.threshold


class WorkflowAction(BaseModel):
    """One action, with the parameters its registered handler expects."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: str
    kind: ActionKind
    parameters: dict[str, str] = Field(default_factory=dict)
    retry_limit: int = Field(default=3, ge=0, le=10)
    continue_on_failure: bool = False

    @field_validator("action_id")
    @classmethod
    def _validate_action_id(cls, value: str) -> str:
        if not ACTION_ID_PATTERN.match(value):
            raise ValueError(
                f"action_id {value!r} must be lowercase letters, digits, hyphens, or "
                "underscores (2-64 chars, starting with a letter)."
            )
        return value

    @property
    def has_external_effect(self) -> bool:
        return self.kind in EXTERNAL_EFFECT_ACTIONS


class EscalationPolicy(BaseModel):
    """What happens when an approval task breaches its due date (DL-WF-05)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    due_after_hours: int = Field(default=24, ge=1, le=8_760)
    reminder_after_hours: int = Field(default=12, ge=1, le=8_760)
    escalate_to: str = ""

    @model_validator(mode="after")
    def _validate_ordering(self) -> EscalationPolicy:
        if self.reminder_after_hours >= self.due_after_hours:
            raise ValueError(
                "reminder_after_hours must precede due_after_hours, or the reminder arrives "
                "after the breach it exists to prevent."
            )
        return self


class WorkflowDefinition(BaseModel):
    """One versioned, publishable workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_code: str
    workflow_id: str
    version: str
    name: str = Field(min_length=1, max_length=200)
    trigger: WorkflowTrigger
    actions: tuple[WorkflowAction, ...]
    conditions: tuple[WorkflowCondition, ...] = ()
    on_failure_actions: tuple[WorkflowAction, ...] = ()
    escalation: EscalationPolicy | None = None
    status: WorkflowStatus = WorkflowStatus.DRAFT
    owner: str = ""
    published_by: str | None = None
    approved_by: str | None = None
    description: str = ""

    @field_validator("tenant_code")
    @classmethod
    def _validate_tenant(cls, value: str) -> str:
        return validate_tenant_code(value)

    @field_validator("workflow_id")
    @classmethod
    def _validate_workflow_id(cls, value: str) -> str:
        if not WORKFLOW_ID_PATTERN.match(value):
            raise ValueError(
                f"workflow_id {value!r} must be lowercase letters, digits, and hyphens "
                "(2-64 chars, starting with a letter)."
            )
        return value

    @model_validator(mode="after")
    def _validate_shape(self) -> WorkflowDefinition:
        if not self.actions:
            raise ValueError(
                f"workflow {self.workflow_id!r} declares no actions; a workflow that does "
                "nothing is a configuration error, not a no-op."
            )
        if len(self.actions) > MAX_ACTIONS_PER_WORKFLOW:
            raise ValueError(
                f"workflow {self.workflow_id!r} declares {len(self.actions)} actions, above the "
                f"cap of {MAX_ACTIONS_PER_WORKFLOW}."
            )
        if len(self.conditions) > MAX_CONDITIONS_PER_WORKFLOW:
            raise ValueError(
                f"workflow {self.workflow_id!r} declares {len(self.conditions)} conditions, "
                f"above the cap of {MAX_CONDITIONS_PER_WORKFLOW}."
            )
        action_ids = [a.action_id for a in (*self.actions, *self.on_failure_actions)]
        if len(set(action_ids)) != len(action_ids):
            raise ValueError(
                f"workflow {self.workflow_id!r} has duplicate action_ids; the idempotency key "
                "is derived from (workflow, execution, action) and must be unique."
            )
        if self.requires_maker_checker and self.status is WorkflowStatus.PUBLISHED:
            if not self.approved_by:
                raise ValueError(
                    f"workflow {self.workflow_id!r} has an external-effect action and cannot be "
                    "published without an approver (DL-WF-01, OWASP A08)."
                )
            if self.approved_by == self.published_by:
                raise ValueError(
                    f"workflow {self.workflow_id!r} was approved by its own publisher; maker and "
                    "checker must differ."
                )
        if any(a.kind is ActionKind.CREATE_APPROVAL_TASK for a in self.actions):
            if self.escalation is None:
                raise ValueError(
                    f"workflow {self.workflow_id!r} creates an approval task but declares no "
                    "escalation policy; an approval with no due date never escalates."
                )
        return self

    @property
    def requires_maker_checker(self) -> bool:
        return any(action.has_external_effect for action in self.actions)

    @property
    def sort_key(self) -> str:
        return f"{self.workflow_id}#{self.version}"

    def action(self, action_id: str) -> WorkflowAction:
        for candidate in (*self.actions, *self.on_failure_actions):
            if candidate.action_id == action_id:
                return candidate
        raise KeyError(f"workflow {self.workflow_id!r} has no action {action_id!r}.")

    def to_body(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
