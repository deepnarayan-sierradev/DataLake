"""
AWS Lambda handler for the extraction pipeline Step Functions task.

This is the entry point that Step Functions invokes for each extraction run.
It receives the execution input, wires all platform dependencies, and delegates
to ExtractionWorkflow for the full 10-stage pipeline.

Step Functions execution input schema:
  {
    "source_id":        str   — stable source identifier
    "entity_id":        str   — stable entity identifier
    "environment":      str   — "dev" | "staging" | "prod"
    "connector_params": dict  — source-specific non-secret parameters
    "tenant_code":      str   — tenant identity for this run (ARCH-4: required, fails closed)
    "is_replay":        bool  — true when re-running a DLQ entry
    "replay_of_run_id": str   — original run_id (required when is_replay=true)
  }

Required Lambda environment variables:
  AWS_REGION               — injected automatically by Lambda runtime
  PLATFORM_ENVIRONMENT     — deployment environment (dev/staging/prod)
  RAW_S3_BUCKET            — name of the raw layer S3 bucket
  SCHEMA_SNAPSHOT_S3_BUCKET — name of the schema snapshot S3 bucket

Security (OWASP A03, A07, A09):
  - Input validated against stable identifier regex before use in any AWS call.
  - Credentials never in handler code; resolved from Secrets Manager by adapters.
  - Result returned to Step Functions contains only metadata — no field values.
  - Lambda execution role is the extraction_runtime IAM role (least privilege).
"""

from __future__ import annotations

import dataclasses
from typing import Any, Final

# Import adapter modules so their @connector_registry.register() decorators
# and register_builder() calls execute at Lambda cold-start time.
import connector_runtime.adapters.dialpad.dialpad_connector
import connector_runtime.adapters.google_ads.google_ads_connector
import connector_runtime.adapters.google_analytics.google_analytics_connector
import connector_runtime.adapters.housecall_pro.housecall_pro_connector
import connector_runtime.adapters.hubspot.hubspot_connector
import connector_runtime.adapters.maid_central.maid_central_connector
import connector_runtime.adapters.meta_ads.meta_ads_connector
import connector_runtime.adapters.mysql_rds.mysql_rds_connector
import connector_runtime.adapters.netsuite.netsuite_connector
import connector_runtime.adapters.sage.sage_connector
import connector_runtime.adapters.salesforce.salesforce_connector
import connector_runtime.adapters.seniorplace.seniorplace_connector
import connector_runtime.adapters.servman_pro.servman_pro_connector
import connector_runtime.adapters.wellsky.wellsky_connector  # noqa: F401
from connector_runtime.configuration_repository.configuration_repository import (
    ConfigurationRepositoryClient,
)
from connector_runtime.registry import connector_registry
from connector_runtime.run_lifecycle.run_lifecycle import RunCoordinator
from contracts.identifier_policy import STABLE_ID_PATTERN as _STABLE_ID_PATTERN
from contracts.identifier_policy import TENANT_CODE_PATTERN
from contracts.observability_contract import PipelineStage
from observability.lambda_utils import check_lambda_timeout, require_env
from observability.metrics_emitter import CloudWatchMetricsEmitter
from observability.stage_execution import (
    StageIdentity,
    derive_correlation_id,
    stage_execution,
)
from observability.structured_logger import get_platform_logger
from orchestration.step_functions.extraction_retry_policy import ExtractionRetryPolicy
from orchestration.step_functions.extraction_workflow import ExtractionWorkflow
from schema_management.drift_evaluation.drift_evaluator import SchemaDriftEvaluator
from schema_management.snapshot_repository.snapshot_repository import SchemaSnapshotRepository
from tenancy.connection_keys import resolve_connection_id
from watermark_management.watermark_repository.watermark_repository import WatermarkRepository

_logger = get_platform_logger(__name__)

_REQUIRED_EVENT_FIELDS: Final[frozenset[str]] = frozenset(
    {"source_id", "entity_id", "environment", "connector_params", "tenant_code"}
)
_KNOWN_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"dev", "staging", "prod"})

# ---------------------------------------------------------------------------
# Lambda-instance retry policy
# Lambda instances are reused across invocations, so a single ExtractionRetryPolicy
# instance accumulates circuit-breaker state across runs for the same source.
# This is intentional: consecutive failures within a Lambda instance's lifetime
# will open the circuit for that instance, preventing further extraction attempts
# until the instance recycles or the circuit is manually reset.
# ---------------------------------------------------------------------------
_retry_policy: ExtractionRetryPolicy = ExtractionRetryPolicy()


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    AWS Lambda entry point for the extraction pipeline Step Functions task.

    Args:
        event:   Step Functions execution input.
        context: Lambda runtime context (not used; typed Any to avoid aws_lambda
                 dependency in pyproject.toml).

    Returns:
        A dict representation of ExtractionWorkflowResult, serialised for
        Step Functions task output.

    Raises:
        ValueError:    Input validation failure (missing/invalid fields or env vars).
        KeyError:      source_id not registered in the connector registry.
        Exception:     Any pipeline stage failure; Step Functions records the
                       execution as failed and the DLQ entry is already enqueued
                       by ExtractionWorkflow before the exception propagates here.
    """
    _validate_event(event)

    # Abort early if insufficient Lambda time remains to run the full pipeline.
    # Lambda timeout is 900 s; 120 s margin prevents starting a run that cannot
    # complete, which would be killed without a DLQ entry or audit record.
    check_lambda_timeout(context, min_remaining_ms=120_000)

    source_id: str = event["source_id"]
    entity_id: str = event["entity_id"]
    environment: str = event["environment"]
    connector_params: dict[str, str] = event["connector_params"]
    is_replay: bool = bool(event.get("is_replay", False))
    replay_of_run_id: str | None = event.get("replay_of_run_id")
    # Tenant code for data-plane isolation (§1.1 / ARCH-4). Required — a
    # missing or malformed tenant_code must fail closed rather than silently
    # run as another tenant (OWASP A03); validated in _validate_event.
    tenant_code: str = str(event["tenant_code"])
    # DL-SCOPE-05: the connection is the identity dimension; for a single-connection source it
    # equals source_id, which keeps pre-migration payloads working unchanged.
    connection_id: str = resolve_connection_id(source_id, event.get("connection_id"))

    # ── Validate connector_params with per-connector Pydantic model (§2.2) ───
    _validate_connector_params(source_id, connector_params)

    region_name = require_env("AWS_REGION")
    raw_s3_bucket = require_env("RAW_S3_BUCKET")
    snapshot_s3_bucket = require_env("SCHEMA_SNAPSHOT_S3_BUCKET")

    _logger.info(
        "extraction_pipeline_handler_invoked",
        source_id=source_id,
        entity_id=entity_id,
        environment=environment,
        tenant_code=tenant_code,
        is_replay=is_replay,
        replay_of_run_id=replay_of_run_id,
        region_name=region_name,
    )

    # ── Wire dependencies ────────────────────────────────────────────────────

    coordinator = RunCoordinator(
        environment=environment,
        region_name=region_name,
        source_id=source_id,
        entity_id=entity_id,
        tenant_code=tenant_code,
    )

    config_client = ConfigurationRepositoryClient(
        environment=environment,
        region_name=region_name,
    )

    watermark_repo = WatermarkRepository(
        environment=environment,
        region_name=region_name,
    )

    snapshot_repo = SchemaSnapshotRepository(
        bucket_name=snapshot_s3_bucket,
        region_name=region_name,
    )

    drift_evaluator = SchemaDriftEvaluator()

    # Resolve connector + raw-layer writer from the registry builder.
    builder = connector_registry.resolve_builder(source_id)
    connector, raw_writer = builder(
        environment, region_name, connector_params, raw_s3_bucket, tenant_code
    )

    workflow = ExtractionWorkflow(
        run_coordinator=coordinator,
        configuration_client=config_client,
        watermark_repository=watermark_repo,
        snapshot_repository=snapshot_repo,
        drift_evaluator=drift_evaluator,
        connector=connector,
        raw_layer_writer=raw_writer,
        retry_policy=_retry_policy,
        # PERF-5: threaded through so the checkpoint path (opt-in via
        # EntityExtractionConfig.max_records_per_lambda_run) can check
        # remaining Lambda execution time via context.get_remaining_time_in_millis().
        lambda_context=context,
    )

    # ── Execute pipeline ─────────────────────────────────────────────────────
    # DL-OPS-05/07: this handler previously bound no contextvars at all and had no failure record
    # on a hard Lambda kill — the stage that most often hits the timeout was the least
    # instrumented. The run id comes from the coordinator, which owns it, so no workflow contract
    # changes.
    identity = StageIdentity(
        tenant_code=tenant_code,
        source_id=source_id,
        entity_id=entity_id,
        run_id=coordinator.run_id,
        environment=environment,
        stage=PipelineStage.EXTRACTION.value,
        correlation_id=derive_correlation_id(coordinator.run_id, replay_of_run_id),
        connection_id=connection_id,
    )

    with stage_execution(identity, region_name=region_name, lambda_context=context):
        result = workflow.execute(
            is_replay=is_replay,
            replay_of_run_id=replay_of_run_id,
        )

    _logger.info(
        "extraction_pipeline_handler_completed",
        run_id=result.run_id,
        source_id=result.source_id,
        entity_id=result.entity_id,
        record_count=result.record_count,
        drift_classification=result.drift_classification,
        transformation_blocked=result.transformation_blocked,
    )

    # ── Emit CloudWatch metrics for extraction stage ──────────────────────────
    try:
        _metrics = CloudWatchMetricsEmitter(region_name=region_name)
        _metrics.set_tenant_context(tenant_code)
        _metrics.emit_records_extracted(
            source_id=source_id,
            entity_id=entity_id,
            environment=environment,
            count=result.record_count,
            stage="extraction",
        )
        if result.drift_classification and result.drift_classification != "NONE":
            _metrics.emit_schema_drift_count(
                source_id=source_id,
                entity_id=entity_id,
                environment=environment,
                count=1,
                stage="extraction",
            )
        _metrics.flush()
    except Exception as _exc:
        # Metric emission must never fail an extraction run.
        _logger.warning("extraction_metrics_emission_failed", error=str(_exc))

    return dataclasses.asdict(result)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _validate_event(event: dict[str, Any]) -> None:
    """
    Validate the Step Functions execution input before any processing.

    Raises:
        ValueError: Missing required fields, invalid stable IDs, or unknown environment.
    """
    missing = _REQUIRED_EVENT_FIELDS - event.keys()
    if missing:
        raise ValueError(
            f"Step Functions execution input is missing required fields: {sorted(missing)}"
        )

    source_id = str(event["source_id"])
    entity_id = str(event["entity_id"])
    environment = str(event["environment"])

    if not _STABLE_ID_PATTERN.match(source_id):
        raise ValueError(
            f"source_id={source_id!r} does not conform to the stable identifier format."
        )
    if not _STABLE_ID_PATTERN.match(entity_id):
        raise ValueError(
            f"entity_id={entity_id!r} does not conform to the stable identifier format."
        )
    if environment not in _KNOWN_ENVIRONMENTS:
        raise ValueError(
            f"environment={environment!r} is not a known deployment environment. "
            f"Expected one of {sorted(_KNOWN_ENVIRONMENTS)}."
        )
    if not isinstance(event.get("connector_params", {}), dict):
        raise ValueError("connector_params must be a JSON object (dict).")

    # tenant_code is required (ARCH-4) and must always be well-formed
    # (OWASP A03 / SEC-5) — a missing or malformed tenant_code must fail
    # closed rather than silently default to another tenant's identity.
    tenant_code = str(event["tenant_code"])
    if not TENANT_CODE_PATTERN.match(tenant_code):
        raise ValueError(f"tenant_code={tenant_code!r} does not conform to the tenant code format.")


def _validate_connector_params(source_id: str, connector_params: dict[str, str]) -> None:
    """
    Validate connector_params using the per-connector Pydantic model (§2.2).

    Runs at handler entry before any AWS call.  Unknown connectors (no params
    model registered) are allowed through — not all connectors require strict
    param validation at this layer.

    Raises:
        ValueError: When connector_params fail the registered model's validation.
    """
    from pydantic import ValidationError

    params_model_cls = connector_registry.get_params_model(source_id)
    if params_model_cls is None:
        # No model registered — passthrough (OWASP: fail-open not fail-closed here is intentional).
        return
    try:
        params_model_cls.model_validate(connector_params)
    except ValidationError as exc:
        raise ValueError(
            f"connector_params validation failed for source_id={source_id!r}: {exc}"
        ) from exc
