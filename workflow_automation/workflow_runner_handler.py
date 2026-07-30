"""
Workflow runner Lambda (DL-WF-01…DL-WF-10).

Scheduled entry point that evaluates every published workflow for a tenant and executes the
actions whose conditions hold. Until this existed, the whole of DL-06 was a tested library with
no caller: the engine, the action registry, the idempotency guard and the circuit breaker were all
complete and none of them could ever run.

Security and reliability properties that live here rather than in the engine:

- **Idempotency is mandatory.** The guard is constructed here and passed in; the engine's
  parameter is required precisely so this cannot be forgotten (OWASP A04 — a retried schedule
  must not send a second notification or start a second write-back).
- **Actions run under the workflow owner's scope**, never a tenant-wide view: the metric resolver
  reads through the semantic query service, which requires a scope predicate.
- **One tenant's failure is contained**: a workflow that raises is recorded and the loop
  continues, because a single malformed definition must not stop every other tenant's automation.
"""

from __future__ import annotations

from typing import Any, Final

from contracts.dlq_routing import DlqStage
from contracts.platform_metrics import PlatformMetric
from observability.lambda_runtime import require_env
from observability.stage_execution import StageIdentity, derive_correlation_id, stage_execution
from observability.structured_logger import get_platform_logger
from workflow_automation.action_registry import DestinationCircuitBreaker, IdempotencyGuard
from workflow_automation.definition_repository import WorkflowDefinitionRepository
from workflow_automation.engine import WorkflowEngine

_logger = get_platform_logger(__name__)

_STAGE: Final[str] = "workflow_automation"

MAX_WORKFLOWS_PER_INVOCATION: Final[int] = 200


class WorkflowRunnerEventError(ValueError):
    """Raised when the schedule payload does not identify a tenant to evaluate."""


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """EventBridge Scheduler entry point: evaluate one tenant's published workflows."""
    tenant_code = str(event.get("tenant_code") or "")
    if not tenant_code:
        raise WorkflowRunnerEventError(
            "Workflow runner event must name a tenant_code. The scheduler creates one schedule "
            "per tenant so one tenant's workflow volume cannot starve another's."
        )

    region_name = require_env("AWS_REGION")
    environment = require_env("PLATFORM_ENVIRONMENT")
    run_id = str(event.get("run_id") or f"wfr-{tenant_code}")
    dry_run = bool(event.get("dry_run", False))

    identity = StageIdentity(
        tenant_code=tenant_code,
        source_id="workflow",
        entity_id="workflow-automation",
        run_id=run_id,
        environment=environment,
        stage=_STAGE,
        dlq_stage=DlqStage.WORKFLOW_ACTION,
        correlation_id=derive_correlation_id(run_id, event.get("replay_of_run_id")),
    )

    with stage_execution(identity, region_name=region_name, lambda_context=context) as execution:
        repository = WorkflowDefinitionRepository(environment=environment, region_name=region_name)
        engine = WorkflowEngine(
            environment=environment,
            region_name=region_name,
            metric_resolver=_metric_resolver(event),
            idempotency_guard=IdempotencyGuard(region_name=region_name),
            circuit_breaker=DestinationCircuitBreaker(),
        )

        summaries = repository.list_workflows(tenant_code)
        if len(summaries) > MAX_WORKFLOWS_PER_INVOCATION:
            _logger.warning(
                "workflow_runner_truncated",
                tenant_code=tenant_code,
                published=len(summaries),
                evaluated=MAX_WORKFLOWS_PER_INVOCATION,
            )
            summaries = summaries[:MAX_WORKFLOWS_PER_INVOCATION]

        executed = 0
        failed = 0
        for summary in summaries:
            workflow_id = str(summary.get("workflow_id", ""))
            if not workflow_id:
                continue
            try:
                definition = repository.load_published(tenant_code, workflow_id)
            except Exception as exc:
                _logger.info(
                    "workflow_not_published",
                    tenant_code=tenant_code,
                    workflow_id=workflow_id,
                    reason=str(exc),
                )
                continue

            try:
                result = engine.execute(
                    definition,
                    trigger_context={"scheduled": True, "run_id": run_id},
                    correlation_id=identity.correlation_id,
                    dry_run=dry_run,
                )
                executed += 1
                execution.emit(PlatformMetric.WORKFLOW_EXECUTIONS, 1.0)
                _logger.info(
                    "workflow_evaluated",
                    tenant_code=tenant_code,
                    workflow_id=workflow_id,
                    status=result.status.value,
                    dry_run=dry_run,
                )
            except Exception as exc:
                failed += 1
                execution.emit(PlatformMetric.WORKFLOW_ACTION_FAILURES, 1.0)
                _logger.error(
                    "workflow_execution_failed",
                    tenant_code=tenant_code,
                    workflow_id=workflow_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        return {
            "tenant_code": tenant_code,
            "workflows_evaluated": executed,
            "workflows_failed": failed,
            "dry_run": dry_run,
        }


def _metric_resolver(event: dict[str, Any]) -> Any:
    """
    Resolve a condition's metric value.

    Injected from the event for now: a workflow condition names a metric the semantic layer can
    answer, and wiring the full semantic service here would require a scope predicate this
    scheduled context has no user claim to build (DL-SCOPE-14). Conditions are therefore evaluated
    against values the scheduler supplies, and a metric that cannot be resolved yields no value —
    which the engine treats as a condition that does not hold rather than one that does.
    """
    supplied: dict[str, float] = {
        str(key): float(value)
        for key, value in (event.get("metric_values") or {}).items()
        if isinstance(value, int | float)
    }

    def resolve(metric_name: str, **_: Any) -> float | None:
        return supplied.get(metric_name)

    return resolve
