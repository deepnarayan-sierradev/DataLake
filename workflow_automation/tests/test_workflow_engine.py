"""Workflow definition, engine, idempotency, circuit-breaker and task tests (DL-06)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from workflow_automation.action_registry import (
    ActionContext,
    ActionOutcome,
    DestinationCircuitBreaker,
    IdempotencyGuard,
    WorkflowActionHandler,
    action_registry,
    idempotency_key,
)
from workflow_automation.definition import (
    ActionKind,
    ComparisonOperator,
    EscalationPolicy,
    PipelineEventKind,
    TriggerKind,
    WorkflowAction,
    WorkflowCondition,
    WorkflowDefinition,
    WorkflowStatus,
    WorkflowTrigger,
)
from workflow_automation.definition_repository import (
    TaskState,
    WorkflowDefinitionRepository,
    WorkflowIntegrityError,
    WorkflowNotFoundError,
    WorkflowTaskRepository,
)
from workflow_automation.engine import (
    ExecutionStatus,
    WorkflowDisabledError,
    WorkflowEngine,
    batch_scheduled_workflows,
)

_REGION = "us-east-1"


def _table(name: str, pk: str, sk: str | None = None) -> None:
    key_schema = [{"AttributeName": pk, "KeyType": "HASH"}]
    attributes = [{"AttributeName": pk, "AttributeType": "S"}]
    if sk:
        key_schema.append({"AttributeName": sk, "KeyType": "RANGE"})
        attributes.append({"AttributeName": sk, "AttributeType": "S"})
    boto3.client("dynamodb", region_name=_REGION).create_table(
        TableName=name,
        KeySchema=key_schema,
        AttributeDefinitions=attributes,
        BillingMode="PAY_PER_REQUEST",
    )


class RecordingAction(WorkflowActionHandler):
    kind = ActionKind.WRITE_EXCEPTION

    def __init__(self, fail_times: int = 0) -> None:
        self.calls = 0
        self._fail_times = fail_times

    def describe(self, parameters):
        return f"write exception {parameters.get('rule_id', '')}"

    def execute(self, parameters, context):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("transient store failure")
        return {"recorded": True}


def _notification_action(action_id: str = "notify-owner") -> WorkflowAction:
    return WorkflowAction(
        action_id=action_id,
        kind=ActionKind.SEND_NOTIFICATION,
        parameters={"topic_arn": "arn:aws:sns:us-east-1:123456789012:edl", "subject": "hi"},
    )


def _definition(**overrides) -> WorkflowDefinition:
    base = {
        "tenant_code": "evive",
        "workflow_id": "revenue-drop-alert",
        "version": "v1",
        "name": "Revenue drop alert",
        "trigger": WorkflowTrigger(kind=TriggerKind.SCHEDULE, cron_expression="cron(0 8 * * ? *)"),
        "actions": (
            WorkflowAction(
                action_id="write-exception",
                kind=ActionKind.WRITE_EXCEPTION,
                parameters={"rule_id": "revenue-drop"},
                retry_limit=0,
            ),
        ),
        "owner": "role:cfo",
    }
    return WorkflowDefinition(**{**base, **overrides})


class TestDefinitionValidation:
    def test_a_workflow_with_no_actions_is_rejected(self):
        with pytest.raises(ValueError, match="declares no actions"):
            _definition(actions=())

    def test_schedule_trigger_requires_a_cron(self):
        with pytest.raises(ValueError, match="requires a cron_expression"):
            WorkflowTrigger(kind=TriggerKind.SCHEDULE)

    def test_pipeline_event_trigger_names_its_event(self):
        with pytest.raises(ValueError, match="must name the event"):
            WorkflowTrigger(kind=TriggerKind.PIPELINE_EVENT)

    def test_data_condition_trigger_needs_a_saved_query(self):
        with pytest.raises(ValueError, match="must name the saved query"):
            WorkflowTrigger(kind=TriggerKind.DATA_CONDITION)

    def test_valid_pipeline_event_trigger(self):
        trigger = WorkflowTrigger(
            kind=TriggerKind.PIPELINE_EVENT,
            pipeline_event=PipelineEventKind.QUALITY_GATE_BLOCKED,
        )
        assert trigger.pipeline_event is PipelineEventKind.QUALITY_GATE_BLOCKED

    def test_duplicate_action_ids_break_idempotency_and_are_rejected(self):
        with pytest.raises(ValueError, match="duplicate action_ids"):
            _definition(actions=(_notification_action(), _notification_action()))

    def test_external_effect_publish_requires_an_approver(self):
        with pytest.raises(ValueError, match="cannot be published without an approver"):
            _definition(
                actions=(_notification_action(),),
                status=WorkflowStatus.PUBLISHED,
                published_by="alice",
            )

    def test_maker_and_checker_must_differ(self):
        with pytest.raises(ValueError, match="approved by its own publisher"):
            _definition(
                actions=(_notification_action(),),
                status=WorkflowStatus.PUBLISHED,
                published_by="alice",
                approved_by="alice",
            )

    def test_external_effect_publish_with_a_distinct_approver(self):
        definition = _definition(
            actions=(_notification_action(),),
            status=WorkflowStatus.PUBLISHED,
            published_by="alice",
            approved_by="bob",
        )
        assert definition.requires_maker_checker is True

    def test_internal_only_workflow_needs_no_approver(self):
        definition = _definition(status=WorkflowStatus.PUBLISHED, published_by="alice")
        assert definition.requires_maker_checker is False

    def test_approval_action_requires_an_escalation_policy(self):
        with pytest.raises(ValueError, match="declares no escalation policy"):
            _definition(
                actions=(
                    WorkflowAction(
                        action_id="approve-it",
                        kind=ActionKind.CREATE_APPROVAL_TASK,
                        parameters={"assignee": "ops@example.test"},
                    ),
                )
            )

    def test_reminder_must_precede_the_due_date(self):
        with pytest.raises(ValueError, match="must precede due_after_hours"):
            EscalationPolicy(due_after_hours=12, reminder_after_hours=24)

    def test_action_id_charset_is_enforced(self):
        with pytest.raises(ValueError, match="must be lowercase"):
            WorkflowAction(action_id="Bad Action", kind=ActionKind.WRITE_EXCEPTION)

    def test_workflow_id_charset_is_enforced(self):
        with pytest.raises(ValueError, match="must be lowercase"):
            _definition(workflow_id="Bad_Workflow")

    def test_action_lookup(self):
        assert _definition().action("write-exception").kind is ActionKind.WRITE_EXCEPTION
        with pytest.raises(KeyError):
            _definition().action("nope")


class TestConditionGrammar:
    def _condition(self, operator: ComparisonOperator, threshold: float = 100.0):
        return WorkflowCondition(metric="revenue", operator=operator, threshold=threshold)

    @pytest.mark.parametrize(
        ("operator", "observed", "expected"),
        [
            (ComparisonOperator.GREATER_THAN, 101.0, True),
            (ComparisonOperator.GREATER_THAN, 100.0, False),
            (ComparisonOperator.GREATER_OR_EQUAL, 100.0, True),
            (ComparisonOperator.LESS_THAN, 99.0, True),
            (ComparisonOperator.LESS_OR_EQUAL, 100.0, True),
            (ComparisonOperator.EQUALS, 100.0, True),
            (ComparisonOperator.NOT_EQUALS, 99.0, True),
        ],
    )
    def test_comparisons(self, operator, observed, expected):
        assert self._condition(operator).evaluate(observed) is expected

    def test_change_by_pct_needs_a_previous_value(self):
        condition = self._condition(ComparisonOperator.CHANGED_BY_PCT_ABOVE, threshold=10.0)
        assert condition.evaluate(120.0, previous=None) is False
        assert condition.evaluate(120.0, previous=0) is False
        assert condition.evaluate(120.0, previous=100.0) is True
        assert condition.evaluate(105.0, previous=100.0) is False

    def test_condition_must_name_a_metric(self):
        with pytest.raises(ValueError, match="must name a metric"):
            WorkflowCondition(metric="  ", operator=ComparisonOperator.EQUALS, threshold=1)


class TestIdempotencyKey:
    def test_key_is_stable_for_the_same_triple(self):
        assert idempotency_key("wf", "ex", "act") == idempotency_key("wf", "ex", "act")

    def test_key_differs_per_execution(self):
        assert idempotency_key("wf", "ex1", "act") != idempotency_key("wf", "ex2", "act")

    def test_key_carries_the_action_id_for_debuggability(self):
        assert idempotency_key("wf", "ex", "notify").startswith("notify-")


class TestCircuitBreaker:
    def test_opens_after_the_threshold(self):
        clock = [0.0]
        breaker = DestinationCircuitBreaker(
            failure_threshold=2, cooldown_seconds=10, monotonic=lambda: clock[0]
        )
        assert breaker.record_failure("webhook:a") is False
        assert breaker.record_failure("webhook:a") is True
        assert breaker.is_open("webhook:a") is True

    def test_one_dead_destination_does_not_affect_others(self):
        breaker = DestinationCircuitBreaker(failure_threshold=1)
        breaker.record_failure("webhook:a")
        assert breaker.is_open("webhook:a") is True
        assert breaker.is_open("webhook:b") is False

    def test_half_opens_after_the_cooldown(self):
        clock = [0.0]
        breaker = DestinationCircuitBreaker(
            failure_threshold=1, cooldown_seconds=10, monotonic=lambda: clock[0]
        )
        breaker.record_failure("webhook:a")
        clock[0] = 11.0
        assert breaker.is_open("webhook:a") is False

    def test_success_resets_the_state(self):
        breaker = DestinationCircuitBreaker(failure_threshold=2)
        breaker.record_failure("webhook:a")
        breaker.record_success("webhook:a")
        assert breaker.record_failure("webhook:a") is False

    def test_open_destinations_are_listable(self):
        breaker = DestinationCircuitBreaker(failure_threshold=1)
        breaker.record_failure("webhook:a")
        assert breaker.open_destinations() == ["webhook:a"]


@mock_aws
class TestEngine:
    def _engine(self, resolver=None, breaker=None) -> WorkflowEngine:
        """
        Always carries a real guard. The `with_idempotency=False` variant used to construct the
        engine with `idempotency_guard=None`, which is the production configuration in which every
        retry re-fires an external action — so most of this class exercised an engine that could
        not have been deployed safely.
        """
        _table("EdlWorkflowExecution", "tenant_code", "execution_key")
        _table("EdlWorkflowIdempotency", "tenant_code", "idempotency_key")
        return WorkflowEngine(
            environment="dev",
            region_name=_REGION,
            metric_resolver=resolver or (lambda tenant, condition: (150.0, 100.0)),
            idempotency_guard=IdempotencyGuard(region_name=_REGION),
            circuit_breaker=breaker,
        )

    def setup_method(self, method=None):
        action_registry.reset()

    def teardown_method(self, method=None):
        action_registry.reset()

    def test_published_workflow_executes_its_actions(self):
        handler = RecordingAction()
        action_registry.register(handler)
        engine = self._engine()
        execution = engine.execute(_definition(status=WorkflowStatus.PUBLISHED))
        assert execution.status is ExecutionStatus.SUCCEEDED
        assert handler.calls == 1

    def test_draft_workflow_cannot_execute(self):
        action_registry.register(RecordingAction())
        engine = self._engine()
        with pytest.raises(WorkflowDisabledError, match="only a published definition executes"):
            engine.execute(_definition())

    def test_dry_run_performs_nothing(self):
        handler = RecordingAction()
        action_registry.register(handler)
        engine = self._engine()
        execution = engine.dry_run(_definition())
        assert execution.status is ExecutionStatus.DRY_RUN
        assert handler.calls == 0
        assert execution.action_results[0].outcome is ActionOutcome.SKIPPED_DRY_RUN
        assert "write exception revenue-drop" in execution.action_results[0].detail

    def test_conditions_short_circuit(self):
        handler = RecordingAction()
        action_registry.register(handler)
        engine = self._engine(resolver=lambda t, c: (5.0, None))
        definition = _definition(
            status=WorkflowStatus.PUBLISHED,
            conditions=(
                WorkflowCondition(
                    metric="revenue", operator=ComparisonOperator.GREATER_THAN, threshold=100
                ),
            ),
        )
        execution = engine.execute(definition)
        assert execution.status is ExecutionStatus.CONDITIONS_NOT_MET
        assert handler.calls == 0

    def test_unmeasurable_condition_fails_closed(self):
        action_registry.register(RecordingAction())

        def boom(tenant, condition):
            raise RuntimeError("athena unavailable")

        engine = self._engine(resolver=boom)
        definition = _definition(
            status=WorkflowStatus.PUBLISHED,
            conditions=(
                WorkflowCondition(
                    metric="revenue", operator=ComparisonOperator.GREATER_THAN, threshold=1
                ),
            ),
        )
        execution = engine.execute(definition)
        assert execution.status is ExecutionStatus.CONDITIONS_NOT_MET
        assert "could not be evaluated" in execution.condition_evaluations[0].detail

    def test_absent_metric_value_fails_closed(self):
        action_registry.register(RecordingAction())
        engine = self._engine(resolver=lambda t, c: (None, None))
        definition = _definition(
            status=WorkflowStatus.PUBLISHED,
            conditions=(
                WorkflowCondition(
                    metric="revenue", operator=ComparisonOperator.GREATER_THAN, threshold=1
                ),
            ),
        )
        assert engine.execute(definition).status is ExecutionStatus.CONDITIONS_NOT_MET

    def test_retry_then_succeed(self):
        handler = RecordingAction(fail_times=2)
        action_registry.register(handler)
        engine = self._engine()
        definition = _definition(
            status=WorkflowStatus.PUBLISHED,
            actions=(
                WorkflowAction(
                    action_id="write-exception",
                    kind=ActionKind.WRITE_EXCEPTION,
                    parameters={"rule_id": "revenue-drop"},
                    retry_limit=3,
                ),
            ),
        )
        execution = engine.execute(definition)
        assert execution.status is ExecutionStatus.SUCCEEDED
        assert handler.calls == 3

    def test_exhausted_retries_fail_the_execution(self):
        action_registry.register(RecordingAction(fail_times=99))
        engine = self._engine()
        execution = engine.execute(_definition(status=WorkflowStatus.PUBLISHED))
        assert execution.status is ExecutionStatus.FAILED
        assert "exhausted 1 attempt" in execution.failed_actions[0].detail

    def test_duplicate_trigger_produces_exactly_one_effect(self):
        handler = RecordingAction()
        action_registry.register(handler)
        engine = self._engine()
        definition = _definition(status=WorkflowStatus.PUBLISHED)
        first = engine.execute(definition, execution_id="wex-fixed")
        second = engine.execute(definition, execution_id="wex-fixed")
        assert handler.calls == 1
        assert first.action_results[0].outcome is ActionOutcome.EXECUTED
        assert second.action_results[0].outcome is ActionOutcome.SKIPPED_DUPLICATE

    def test_open_circuit_skips_the_action(self):
        action_registry.register(RecordingAction())
        breaker = DestinationCircuitBreaker(failure_threshold=1)
        breaker.record_failure("write_exception")
        engine = self._engine(breaker=breaker)
        execution = engine.execute(_definition(status=WorkflowStatus.PUBLISHED))
        assert execution.action_results[0].outcome is ActionOutcome.CIRCUIT_OPEN

    def test_unregistered_action_kind_fails_the_action(self):
        engine = self._engine()
        execution = engine.execute(_definition(status=WorkflowStatus.PUBLISHED))
        assert execution.status is ExecutionStatus.FAILED
        assert "No action handler registered" in execution.failed_actions[0].detail

    def test_on_failure_actions_run_after_a_failure(self):
        handler = RecordingAction(fail_times=99)
        action_registry.register(handler)
        engine = self._engine()
        definition = _definition(
            status=WorkflowStatus.PUBLISHED,
            on_failure_actions=(
                WorkflowAction(
                    action_id="failure-note",
                    kind=ActionKind.WRITE_EXCEPTION,
                    parameters={"rule_id": "workflow-failed"},
                    retry_limit=0,
                ),
            ),
        )
        execution = engine.execute(definition)
        assert len(execution.action_results) == 2

    def test_execution_history_is_persisted(self):
        action_registry.register(RecordingAction())
        engine = self._engine()
        engine.execute(_definition(status=WorkflowStatus.PUBLISHED))
        history = engine.list_executions("evive", "revenue-drop-alert")
        assert len(history) == 1
        assert history[0]["status"] == ExecutionStatus.SUCCEEDED.value

    def test_dry_run_is_also_persisted_as_audit(self):
        action_registry.register(RecordingAction())
        engine = self._engine()
        engine.dry_run(_definition())
        assert engine.list_executions("evive", "revenue-drop-alert")[0]["dry_run"] is True


class TestScheduleBatching:
    def test_workflows_sharing_a_cron_batch_together(self):
        first = _definition()
        second = _definition(workflow_id="second-alert")
        third = _definition(
            workflow_id="third-alert",
            trigger=WorkflowTrigger(kind=TriggerKind.SCHEDULE, cron_expression="cron(0 9 * * ? *)"),
        )
        batches = batch_scheduled_workflows([first, second, third])
        assert len(batches["cron(0 8 * * ? *)"]) == 2
        assert len(batches["cron(0 9 * * ? *)"]) == 1

    def test_non_schedule_workflows_are_excluded(self):
        manual = _definition(trigger=WorkflowTrigger(kind=TriggerKind.MANUAL))
        assert batch_scheduled_workflows([manual]) == {}


@mock_aws
class TestDefinitionRepository:
    def _repository(self) -> WorkflowDefinitionRepository:
        _table("EdlWorkflowDefinition", "tenant_code", "workflow_key")
        return WorkflowDefinitionRepository(environment="dev", region_name=_REGION)

    def test_save_and_load_round_trip(self):
        repository = self._repository()
        repository.save(_definition())
        loaded = repository.load("evive", "revenue-drop-alert", "v1")
        assert loaded.name == "Revenue drop alert"

    def test_publish_writes_the_pointer(self):
        repository = self._repository()
        published = repository.publish(_definition(), published_by="alice")
        assert published.status is WorkflowStatus.PUBLISHED
        assert repository.load_published("evive", "revenue-drop-alert").version == "v1"

    def test_publish_of_an_external_effect_workflow_needs_an_approver(self):
        repository = self._repository()
        with pytest.raises(ValueError, match="without an approver"):
            repository.publish(_definition(actions=(_notification_action(),)), published_by="alice")

    def test_versions_coexist_so_a_publish_is_version_bumping(self):
        repository = self._repository()
        repository.save(_definition())
        repository.save(_definition(version="v2", name="Revenue drop alert v2"))
        assert repository.load("evive", "revenue-drop-alert", "v1").name == "Revenue drop alert"
        assert repository.load("evive", "revenue-drop-alert", "v2").name == (
            "Revenue drop alert v2"
        )

    def test_tampered_body_fails_closed(self):
        repository = self._repository()
        repository.save(_definition())
        repository._table.update_item(
            Key={"tenant_code": "evive", "workflow_key": "revenue-drop-alert#v1"},
            UpdateExpression="SET content_hash = :h",
            ExpressionAttributeValues={":h": "0" * 64},
        )
        repository.invalidate("evive", "revenue-drop-alert")
        with pytest.raises(WorkflowIntegrityError, match="possibly-tampered"):
            repository.load("evive", "revenue-drop-alert", "v1")

    def test_missing_version_raises(self):
        repository = self._repository()
        with pytest.raises(WorkflowNotFoundError):
            repository.load("evive", "revenue-drop-alert", "v9")

    def test_unpublished_workflow_has_no_published_pointer(self):
        repository = self._repository()
        repository.save(_definition())
        with pytest.raises(WorkflowNotFoundError, match="no published version"):
            repository.load_published("evive", "revenue-drop-alert")

    def test_disable_transitions_the_status(self):
        repository = self._repository()
        repository.publish(_definition(), published_by="alice")
        disabled = repository.disable("evive", "revenue-drop-alert", "v1")
        assert disabled.status is WorkflowStatus.DISABLED

    def test_listing_excludes_the_pointer_row(self):
        repository = self._repository()
        repository.publish(_definition(), published_by="alice")
        assert len(repository.list_workflows("evive")) == 1


@mock_aws
class TestTaskRepository:
    def _repository(self) -> WorkflowTaskRepository:
        _table("EdlWorkflowTask", "tenant_code", "task_id")
        return WorkflowTaskRepository(environment="dev", region_name=_REGION)

    def _task(self, repository: WorkflowTaskRepository, due_after_hours: int = 24) -> str:
        return repository.create_task(
            tenant_code="evive",
            workflow_id="revenue-drop-alert",
            execution_id="wex-1",
            assignee="ops@example.test",
            title="Approve the restatement",
            due_after_hours=due_after_hours,
        )

    def test_create_and_list(self):
        repository = self._repository()
        task_id = self._task(repository)
        tasks = repository.list_tasks("evive", assignee="ops@example.test", state=TaskState.OPEN)
        assert [t["task_id"] for t in tasks] == [task_id]

    def test_unassigned_task_is_rejected(self):
        repository = self._repository()
        with pytest.raises(ValueError, match="must have an assignee"):
            repository.create_task(
                tenant_code="evive",
                workflow_id="w",
                execution_id="e",
                assignee="",
                title="t",
            )

    def test_approve_records_the_actor(self):
        repository = self._repository()
        task_id = self._task(repository)
        repository.decide(
            "evive", task_id, state=TaskState.APPROVED, decided_by="cfo@example.test", comment="ok"
        )
        task = repository.list_tasks("evive")[0]
        assert task["state"] == TaskState.APPROVED.value
        assert task["decided_by"] == "cfo@example.test"

    def test_a_second_decision_is_refused(self):
        repository = self._repository()
        task_id = self._task(repository)
        repository.decide("evive", task_id, state=TaskState.APPROVED, decided_by="a", comment="ok")
        with pytest.raises(ValueError, match="already reached a terminal state"):
            repository.decide(
                "evive", task_id, state=TaskState.REJECTED, decided_by="b", comment="no"
            )

    def test_overdue_tasks_are_the_escalation_input(self):
        repository = self._repository()
        self._task(repository, due_after_hours=1)
        assert repository.overdue_tasks("evive") == []
        future = datetime.now(UTC) + timedelta(hours=2)
        assert len(repository.overdue_tasks("evive", now=future)) == 1

    def test_escalation_transition_carries_no_decision_maker(self):
        repository = self._repository()
        task_id = self._task(repository)
        repository.mark_state("evive", task_id, TaskState.ESCALATED)
        assert repository.list_tasks("evive")[0]["state"] == TaskState.ESCALATED.value

    def test_decision_after_escalation_is_still_permitted(self):
        repository = self._repository()
        task_id = self._task(repository)
        repository.mark_state("evive", task_id, TaskState.ESCALATED)
        repository.decide(
            "evive", task_id, state=TaskState.APPROVED, decided_by="cfo", comment="late but ok"
        )
        assert repository.list_tasks("evive")[0]["state"] == TaskState.APPROVED.value


class TestActionContext:
    def test_context_carries_the_owner_not_a_service_identity(self):
        context = ActionContext(
            tenant_code="evive",
            workflow_id="w",
            execution_id="e",
            correlation_id="c",
            environment="dev",
            acting_as="role:cfo",
        )
        assert context.acting_as == "role:cfo"
        assert context.dry_run is False
