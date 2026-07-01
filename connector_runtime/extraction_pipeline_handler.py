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
import os
from typing import Any, Final

# Import adapter modules so their @connector_registry.register() decorators
# and register_builder() calls execute at Lambda cold-start time.
import connector_runtime.adapters.mysql_rds.mysql_rds_connector
import connector_runtime.adapters.netsuite.netsuite_connector
import connector_runtime.adapters.sage.sage_connector  # noqa: F401
import connector_runtime.adapters.salesforce.salesforce_connector  # noqa: F401
from connector_runtime.configuration_repository.configuration_repository import (
    ConfigurationRepositoryClient,
)
from connector_runtime.registry import connector_registry
from connector_runtime.run_lifecycle.run_lifecycle import RunCoordinator
from contracts.identifier_policy import STABLE_ID_PATTERN as _STABLE_ID_PATTERN
from observability.structured_logger import get_platform_logger
from orchestration.step_functions.extraction_retry_policy import ExtractionRetryPolicy
from orchestration.step_functions.extraction_workflow import ExtractionWorkflow
from schema_management.drift_evaluation.drift_evaluator import SchemaDriftEvaluator
from schema_management.snapshot_repository.snapshot_repository import SchemaSnapshotRepository
from watermark_management.watermark_repository.watermark_repository import WatermarkRepository

_logger = get_platform_logger(__name__)

_REQUIRED_EVENT_FIELDS: Final[frozenset[str]] = frozenset(
    {"source_id", "entity_id", "environment", "connector_params"}
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

    source_id: str = event["source_id"]
    entity_id: str = event["entity_id"]
    environment: str = event["environment"]
    connector_params: dict[str, str] = event["connector_params"]
    is_replay: bool = bool(event.get("is_replay", False))
    replay_of_run_id: str | None = event.get("replay_of_run_id")

    region_name = _require_env("AWS_REGION")
    raw_s3_bucket = _require_env("RAW_S3_BUCKET")
    snapshot_s3_bucket = _require_env("SCHEMA_SNAPSHOT_S3_BUCKET")

    _logger.info(
        "extraction_pipeline_handler_invoked",
        source_id=source_id,
        entity_id=entity_id,
        environment=environment,
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
    connector, raw_writer = builder(environment, region_name, connector_params, raw_s3_bucket)

    workflow = ExtractionWorkflow(
        run_coordinator=coordinator,
        configuration_client=config_client,
        watermark_repository=watermark_repo,
        snapshot_repository=snapshot_repo,
        drift_evaluator=drift_evaluator,
        connector=connector,
        raw_layer_writer=raw_writer,
        retry_policy=_retry_policy,
    )

    # ── Execute pipeline ─────────────────────────────────────────────────────

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


def _require_env(name: str) -> str:
    """
    Return the value of a required environment variable.

    Raises:
        RuntimeError: When the variable is absent or empty.
    """
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(
            f"Required Lambda environment variable '{name}' is not set. "
            "Ensure the extraction pipeline Lambda is deployed with this variable configured."
        )
    return value
