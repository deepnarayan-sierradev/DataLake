"""
AWS Lambda handler for the BuildTwin Step Functions stage (FR-1.1 / FR-1.3).

Resolves the primary entity type from entity_id, loads its relationship rules,
and rebuilds the twin index for that type from the latest analytics-layer
golden records. Additive and idempotent: when no rules are configured the stage
returns skipped without touching state; related entity types not yet published
simply contribute no edges (eventual consistency across per-entity runs).

Step Functions input (Parameters block):
  {source_id, entity_id, environment, run_id, tenant_code,
   rule_set_version?, lifecycle_field?}

Required env vars: AWS_REGION (runtime-injected), ANALYTICS_S3_BUCKET,
RELATIONSHIP_RULES_S3_BUCKET.

Security (OWASP A03): event identifiers validated before use; bucket names come
only from env vars, never the event; SQL is engine-internal over allowlisted
rule fields.
"""

from __future__ import annotations

from typing import Any, Final

import boto3

import processing_engine.engines.duckdb_engine  # noqa: F401  (registers "duckdb")
from analytics_publisher.analytics_location import latest_partition_uri
from contracts.dlq_routing import DlqStage
from contracts.identifier_policy import (
    STABLE_ID_PATTERN,
    TENANT_CODE_PATTERN,
)
from contracts.observability_contract import PipelineStage
from entity_resolution.entity_type_registry import EntityTypeRegistryClient
from knowledge.relationship_resolver import RelationshipResolver
from knowledge.relationship_rules_registry import (
    RelationshipRulesNotFoundError,
    RelationshipRulesRegistry,
)
from knowledge.twin_pipeline import RelationshipInput, TwinPipeline
from knowledge.twin_repository import TwinRepository
from observability.lambda_runtime import check_lambda_timeout, require_env
from observability.stage_execution import (
    StageIdentity,
    derive_correlation_id,
    stage_execution,
)
from observability.structured_logger import get_platform_logger
from processing_engine.registry import set_based_engine_registry

_logger = get_platform_logger(__name__)

_REQUIRED_EVENT_FIELDS: Final[frozenset[str]] = frozenset(
    {"source_id", "entity_id", "environment", "run_id", "tenant_code"}
)
_KNOWN_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"dev", "uat", "prod"})


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entry point for the BuildTwin Step Functions task."""
    _validate_event(event)
    check_lambda_timeout(context, min_remaining_ms=60_000)

    source_id = str(event["source_id"])
    entity_id = str(event["entity_id"])
    environment = str(event["environment"])
    run_id = str(event["run_id"])
    tenant_code = str(event["tenant_code"])

    identity = StageIdentity(
        tenant_code=tenant_code,
        source_id=source_id,
        entity_id=entity_id,
        run_id=run_id,
        environment=environment,
        stage=PipelineStage.GOLDEN_RECORD_PUBLISH.value,
        dlq_stage=DlqStage.TWIN_BUILD,
        correlation_id=derive_correlation_id(run_id, event.get("replay_of_run_id")),
    )
    with stage_execution(identity, region_name=require_env("AWS_REGION"), lambda_context=context):
        return _run_twin_build(
            entity_id=entity_id,
            environment=environment,
            run_id=run_id,
            tenant_code=tenant_code,
            rule_set_version=str(event.get("rule_set_version") or "latest"),
            lifecycle_field=event.get("lifecycle_field"),
        )


def _run_twin_build(
    entity_id: str,
    environment: str,
    run_id: str,
    tenant_code: str,
    rule_set_version: str,
    lifecycle_field: str | None,
) -> dict[str, Any]:
    region_name = require_env("AWS_REGION")
    analytics_bucket = require_env("ANALYTICS_S3_BUCKET")
    rules_bucket = require_env("RELATIONSHIP_RULES_S3_BUCKET")

    registry = EntityTypeRegistryClient(environment=environment, region_name=region_name)
    entity_type = registry.get_entity_type(entity_id, tenant_code=tenant_code)
    if entity_type is None:
        raise ValueError(
            f"No entity type mapping for entity_id={entity_id!r}, tenant_code={tenant_code!r}."
        )

    rules_registry = RelationshipRulesRegistry(s3_bucket=rules_bucket, region_name=region_name)
    try:
        rule_set = rules_registry.load(tenant_code, entity_type, rule_set_version)
    except RelationshipRulesNotFoundError:
        _logger.info(
            "twin_build_skipped_no_rules", entity_type=entity_type, tenant_code=tenant_code
        )
        return {"skipped": True, "entity_type": entity_type, "twin_count": 0, "edge_count": 0}

    s3 = boto3.client("s3", region_name=region_name)
    golden_uri = latest_partition_uri(s3, analytics_bucket, tenant_code, entity_type)
    if golden_uri is None:
        raise ValueError(
            f"No analytics partition found for entity_type={entity_type!r}, tenant={tenant_code!r}."
        )

    relationships: list[RelationshipInput] = []
    for rule in rule_set.rules:
        if rule.from_entity_type != entity_type:
            continue
        to_uri = latest_partition_uri(s3, analytics_bucket, tenant_code, rule.to_entity_type)
        if to_uri is None:
            _logger.info(
                "twin_build_relationship_skipped_no_target",
                relationship_type=rule.relationship_type,
                to_entity_type=rule.to_entity_type,
            )
            continue
        relationships.append(
            RelationshipInput(
                rule=rule,
                to_uri=to_uri,
                edges_bucket=analytics_bucket,
                edges_prefix=f"{tenant_code}/relationships/{run_id}/{rule.relationship_type}",
            )
        )

    engine = set_based_engine_registry.build("duckdb", region_name=region_name)
    pipeline = TwinPipeline(
        engine=engine,
        resolver=RelationshipResolver(engine),
        repository=TwinRepository(region_name=region_name),
    )
    summary = pipeline.build_twins(
        tenant_code=tenant_code,
        entity_type=entity_type,
        golden_uri=golden_uri,
        relationships=relationships,
        lifecycle_field=lifecycle_field,
    )
    return {
        "skipped": False,
        "entity_type": summary.entity_type,
        "twin_count": summary.twin_count,
        "edge_count": summary.edge_count,
    }


def _validate_event(event: dict[str, Any]) -> None:
    """Validate the Step Functions event payload (OWASP A03)."""
    missing = _REQUIRED_EVENT_FIELDS - set(event.keys())
    if missing:
        raise ValueError(f"Missing required event fields: {sorted(missing)}")
    for field in ("source_id", "entity_id", "run_id"):
        value = str(event[field])
        if not STABLE_ID_PATTERN.match(value):
            raise ValueError(f"Event field {field}={value!r} contains disallowed characters.")
    if str(event["environment"]) not in _KNOWN_ENVIRONMENTS:
        raise ValueError(f"Unknown environment={event['environment']!r}.")
    tenant_code = str(event["tenant_code"])
    if not TENANT_CODE_PATTERN.match(tenant_code):
        raise ValueError(f"tenant_code={tenant_code!r} does not conform to the tenant code format.")
