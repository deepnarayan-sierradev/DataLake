"""
Control-plane API Lambda handler for the Enterprise Data Lake SaaS platform.

Single Lambda dispatches all control-plane routes based on (httpMethod, path)
from an API Gateway Lambda-proxy event:

  POST   /tenants                                 — provision a new tenant
  GET    /tenants/{tenant_code}/entities           — list configured entities
  POST   /tenants/{tenant_code}/entities           — register a new entity
  POST   /tenants/{tenant_code}/pipelines/trigger  — enqueue a pipeline run
  GET    /tenants/{tenant_code}/runs/{run_id}      — run status detail
  GET    /tenants/{tenant_code}/runs               — list recent runs

Design:
  - The pipeline trigger route enqueues to the SAME SQS FIFO queue that
    orchestration/pipeline_trigger/pipeline_trigger_handler.py consumes,
    building the identical TriggerMessage-shaped body — there is exactly one
    states:StartExecution code path in the platform, not two.
  - Entity registration reuses EntityExtractionConfig (contracts) directly
    for validation rather than re-implementing field checks.
  - All identifier validation (tenant_code, source_id, entity_id, run_id)
    goes through contracts.identifier_policy — never re-implemented here.

Security (OWASP A01, A03, A09):
  - Every request body is validated with a Pydantic model (extra="forbid")
    before any AWS API call is made.
  - The `{tenant_code}` path parameter is NEVER trusted on its own: the
    authenticated tenant identity is extracted from the API Gateway
    authorizer context (Cognito claims) and cross-checked against the path
    parameter on every tenant-scoped route. Absence of authorizer claims
    (e.g. the Cognito authorizer is not wired up in front of this Lambda, or
    a local/manual invocation) is a hard failure (401) — the path parameter
    is never trusted as a fallback.
  - Responses never include raw stored record values beyond the minimal
    metadata/status fields documented per endpoint — matching the
    platform-wide convention in every other handler module.
  - All error responses are structured JSON with a caller-safe message.
    Unexpected exceptions are logged server-side (structured logger) and
    returned to the caller as a generic 500 — never a raw stack trace or
    exception message.

Required Lambda environment variables:
  AWS_REGION                  — injected by the Lambda runtime
  PLATFORM_ENVIRONMENT         — deployment environment (dev/staging/prod)
  PIPELINE_TRIGGER_QUEUE_URL   — URL of the EdlPipelineTrigger.fifo queue
  ENTITY_CONFIG_TABLE          — optional override; defaults to
                                  EdlEntityExtractionConfig
  ENTITY_TYPE_REGISTRY_TABLE   — optional override; defaults to
                                  EdlEntityTypeRegistry
  AUDIT_LOG_TABLE              — optional override; defaults to
                                  EdlRunAuditLog
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from pydantic import ValidationError as PydanticValidationError

import processing_engine.engines.duckdb_engine  # noqa: F401  (registers "duckdb")
from analytics_publisher.analytics_location import latest_partition_uri
from connector_runtime.api.errors import (
    ApiError,
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationFailedError,
)
from connector_runtime.api.models import (
    PipelineTriggerRequest,
    SavedQueryCreateBody,
    SemanticQueryBody,
    TenantProvisionRequest,
)
from connector_runtime.configuration_repository.configuration_repository import (
    ConfigurationAlreadyExistsError,
    ConfigurationBackend,
    ConfigurationRepositoryClient,
)
from contracts.entity_configuration_contract import EntityExtractionConfig
from contracts.identifier_policy import (
    ENTITY_TYPE_PATTERN,
    RUN_ID_PATTERN,
    STABLE_ID_PATTERN,
    validate_run_id,
    validate_tenant_code,
)
from knowledge.twin_repository import TwinNotFoundError, TwinRepository
from observability.lambda_utils import require_env
from observability.structured_logger import get_platform_logger
from processing_engine.registry import set_based_engine_registry
from semantic.query_compiler import (
    AccessDeniedError,
    SemanticQueryError,
    SemanticQueryRequest,
)
from semantic.saved_query import SavedQuery
from semantic.saved_query_repository import SavedQueryNotFoundError, SavedQueryRepository
from semantic.semantic_model import SemanticModel
from semantic.semantic_model_repository import (
    SemanticModelNotFoundError,
    SemanticModelRepository,
)
from semantic.semantic_query_service import SemanticQueryService

_logger = get_platform_logger(__name__)

# Upper bound on rows returned/scanned by the list-runs endpoint per request —
# a defensive cap, not a pagination cursor (tracked as follow-up if a tenant
# genuinely needs to page through more than this many runs).
_MAX_RUNS_LISTED: Final[int] = 50

_ENTITY_TYPE_REGISTRY_TABLE_NAME: Final[str] = "EdlEntityTypeRegistry"
_AUDIT_LOG_TABLE_NAME: Final[str] = "EdlRunAuditLog"


# ---------------------------------------------------------------------------
# Environment / client helpers
# ---------------------------------------------------------------------------


def _region() -> str:
    return os.environ.get("AWS_REGION", "us-east-1")


def _environment() -> str:
    return require_env("PLATFORM_ENVIRONMENT")


def _entity_type_registry_table() -> Any:
    dynamodb = boto3.resource("dynamodb", region_name=_region())
    table_name = os.environ.get("ENTITY_TYPE_REGISTRY_TABLE") or _ENTITY_TYPE_REGISTRY_TABLE_NAME
    return dynamodb.Table(table_name)


def _run_audit_log_table() -> Any:
    dynamodb = boto3.resource("dynamodb", region_name=_region())
    table_name = os.environ.get("AUDIT_LOG_TABLE") or _AUDIT_LOG_TABLE_NAME
    return dynamodb.Table(table_name)


def _configuration_repository() -> ConfigurationRepositoryClient:
    return ConfigurationRepositoryClient(
        environment=_environment(), region_name=_region(), backend=ConfigurationBackend.DYNAMODB
    )


# ---------------------------------------------------------------------------
# Authentication / authorization (OWASP A01)
# ---------------------------------------------------------------------------


def _extract_claims(event: dict[str, Any]) -> dict[str, Any] | None:
    """
    Extract the authorizer claims dict from an API Gateway proxy event.

    Checks both plausible shapes so this works regardless of which API
    Gateway / authorizer combination fronts the Lambda:
      - REST API / HTTP API (payload format 1.0) + Cognito User Pools
        authorizer: requestContext.authorizer.claims
      - HTTP API (payload format 2.0) + JWT authorizer:
        requestContext.authorizer.jwt.claims
    """
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    claims = authorizer.get("claims")
    if isinstance(claims, dict):
        return claims
    jwt_claims = (authorizer.get("jwt") or {}).get("claims")
    if isinstance(jwt_claims, dict):
        return jwt_claims
    return None


def _authenticated_tenant_code(event: dict[str, Any]) -> str:
    """
    Extract and validate the authenticated tenant_code from the authorizer context.

    Fails closed: absence of authorizer claims (Cognito authorizer not wired
    up, or a local/manual invocation) is always rejected with 401 — the
    `{tenant_code}` path parameter is NEVER trusted as a fallback.
    """
    claims = _extract_claims(event)
    if not claims:
        raise AuthenticationError(
            "Request is missing authenticated identity context. This API requires "
            "a valid authenticated request."
        )
    tenant_claim = claims.get("custom:tenant_code") or claims.get("tenant_code")
    if not tenant_claim:
        raise AuthenticationError("Authenticated identity does not carry a tenant_code claim.")
    return validate_tenant_code(str(tenant_claim))


def _authorize_path_tenant(event: dict[str, Any], path_tenant_code: str) -> str:
    """Validate path_tenant_code's format and cross-check it against the authenticated tenant."""
    tenant_code = validate_tenant_code(path_tenant_code)
    authenticated = _authenticated_tenant_code(event)
    if authenticated != tenant_code:
        raise AuthorizationError(
            "Authenticated tenant is not permitted to access this tenant_code path."
        )
    return tenant_code


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _json_default(value: Any) -> Any:
    """
    json.dumps default= hook: DynamoDB numeric attributes deserialize as
    decimal.Decimal via the boto3 resource API, which the stdlib json module
    cannot serialize natively.
    """
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, separators=(",", ":"), default=_json_default),
    }


def _error_response(exc: ApiError) -> dict[str, Any]:
    return _response(exc.status_code, {"error": exc.message})


def _parse_json_body(event: dict[str, Any]) -> dict[str, Any]:
    body_str = event.get("body") or "{}"
    try:
        parsed = json.loads(body_str)
    except json.JSONDecodeError as exc:
        raise ValidationFailedError("Request body is not valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise ValidationFailedError("Request body must be a JSON object.")
    return parsed


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def _handle_create_tenant(event: dict[str, Any]) -> dict[str, Any]:
    """
    POST /tenants — provision a new tenant.

    There is no existing tenant to authorize against yet (this is the
    genesis operation), so this route only requires a valid authenticated
    caller (any authenticated identity) rather than a tenant_code match.
    Promoting this to a platform-admin-scoped claim is tracked as follow-up
    work once an admin authorizer scope exists.
    """
    if not _extract_claims(event):
        raise AuthenticationError("Request is missing authenticated identity context.")

    body_dict = _parse_json_body(event)
    try:
        request = TenantProvisionRequest.model_validate(body_dict)
    except PydanticValidationError as exc:
        raise ValidationFailedError(
            f"Request body failed validation: {exc.error_count()} error(s)."
        ) from exc

    table = _entity_type_registry_table()
    now_iso = datetime.now(UTC).isoformat()
    try:
        table.put_item(
            Item={
                "tenant_code": request.tenant_code,
                "sk": "tenant_registry#meta",
                "status": "active",
                "provisioned_at": now_iso,
            },
            ConditionExpression="attribute_not_exists(sk)",
        )
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        if error_code == "ConditionalCheckFailedException":
            raise ConflictError(f"Tenant {request.tenant_code!r} already exists.") from exc
        _logger.error(
            "tenant_provisioning_dynamodb_error",
            tenant_code=request.tenant_code,
            error_code=error_code,
        )
        raise ApiError("Tenant provisioning failed due to an internal error.") from exc

    _logger.info("tenant_provisioned", tenant_code=request.tenant_code)
    return _response(201, {"tenant_code": request.tenant_code, "status": "active"})


def _handle_list_entities(event: dict[str, Any], path_tenant_code: str) -> dict[str, Any]:
    """GET /tenants/{tenant_code}/entities — list configured entities for a tenant."""
    tenant_code = _authorize_path_tenant(event, path_tenant_code)
    repo = _configuration_repository()
    configs = repo.list_configs_for_tenant(tenant_code)
    entities = [
        {
            "source_id": config.source_id,
            "entity_id": config.entity_id,
            "active": config.active,
            "load_type": str(config.load_type),
            "config_version": config.config_version,
            "schedule_enabled": config.schedule_enabled,
        }
        for config in configs
    ]
    return _response(
        200, {"tenant_code": tenant_code, "entities": entities, "count": len(entities)}
    )


def _handle_create_entity(event: dict[str, Any], path_tenant_code: str) -> dict[str, Any]:
    """POST /tenants/{tenant_code}/entities — register a new entity for a tenant."""
    tenant_code = _authorize_path_tenant(event, path_tenant_code)
    body_dict = _parse_json_body(event)

    body_tenant_code = body_dict.get("tenant_code")
    if body_tenant_code is not None and body_tenant_code != tenant_code:
        raise ValidationFailedError(
            "tenant_code in the request body must match the {tenant_code} path parameter, "
            "or be omitted."
        )
    body_dict["tenant_code"] = tenant_code

    try:
        config = EntityExtractionConfig.model_validate(body_dict)
    except PydanticValidationError as exc:
        raise ValidationFailedError(
            f"Entity configuration failed validation: {exc.error_count()} error(s)."
        ) from exc

    repo = _configuration_repository()
    try:
        repo.save_config(config, overwrite=False)
    except ConfigurationAlreadyExistsError as exc:
        raise ConflictError(str(exc)) from exc

    _logger.info(
        "entity_registered",
        tenant_code=tenant_code,
        source_id=config.source_id,
        entity_id=config.entity_id,
    )
    return _response(
        201,
        {
            "tenant_code": tenant_code,
            "source_id": config.source_id,
            "entity_id": config.entity_id,
            "active": config.active,
        },
    )


def _handle_trigger_pipeline(event: dict[str, Any], path_tenant_code: str) -> dict[str, Any]:
    """
    POST /tenants/{tenant_code}/pipelines/trigger — enqueue an extraction run.

    Enqueues to the SAME SQS FIFO queue that pipeline_trigger_handler.py
    drains, building the identical message shape (TriggerMessage) it
    validates — this is the single trigger code path, not a parallel
    states:StartExecution call.
    """
    tenant_code = _authorize_path_tenant(event, path_tenant_code)
    body_dict = _parse_json_body(event)
    try:
        request = PipelineTriggerRequest.model_validate(body_dict)
    except PydanticValidationError as exc:
        raise ValidationFailedError(
            f"Request body failed validation: {exc.error_count()} error(s)."
        ) from exc

    queue_url = require_env("PIPELINE_TRIGGER_QUEUE_URL")
    # environment is sourced from this Lambda's own deployment configuration —
    # never from client input — so a caller hitting the dev control-plane API
    # can never trigger a prod pipeline execution by supplying environment in
    # the request body.
    environment = _environment()

    message_body = {
        "source_id": request.source_id,
        "entity_id": request.entity_id,
        "environment": environment,
        "connector_params": request.connector_params,
        "is_replay": request.is_replay,
        "tenant_code": tenant_code,
        "schedule_tick_iso": datetime.now(UTC).isoformat(),
    }
    sqs = boto3.client("sqs", region_name=_region())
    try:
        sqs.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(message_body, separators=(",", ":")),
            # FIFO queue: MessageGroupId matches the convention documented in
            # infrastructure/modules/orchestration/main.tf. ContentBasedDeduplication
            # is enabled on the queue, so no explicit MessageDeduplicationId is needed.
            # tenant_code is included (ARCH-1) so two tenants triggering the same
            # source/entity don't share a FIFO message group — without it, one
            # tenant's burst of triggers would head-of-line-block another tenant's.
            MessageGroupId=f"{tenant_code}--{request.source_id}--{request.entity_id}",
        )
    except ClientError as exc:
        _logger.error(
            "pipeline_trigger_enqueue_failed",
            tenant_code=tenant_code,
            source_id=request.source_id,
            entity_id=request.entity_id,
            error_code=exc.response["Error"]["Code"],
        )
        raise ApiError("Failed to enqueue the pipeline trigger request.") from exc

    _logger.info(
        "pipeline_trigger_enqueued",
        tenant_code=tenant_code,
        source_id=request.source_id,
        entity_id=request.entity_id,
    )
    return _response(
        202,
        {
            "tenant_code": tenant_code,
            "source_id": request.source_id,
            "entity_id": request.entity_id,
            "status": "enqueued",
        },
    )


def _summarize_run_status(stages: list[dict[str, Any]]) -> str:
    statuses = {stage.get("status") for stage in stages}
    if "failed" in statuses:
        return "failed"
    if statuses and statuses <= {"success"}:
        return "success"
    return "in_progress"


def _handle_get_run(event: dict[str, Any], path_tenant_code: str, run_id: str) -> dict[str, Any]:
    """
    GET /tenants/{tenant_code}/runs/{run_id} — run status summary.

    Verifies the stored record's own tenant_code matches the requested
    tenant_code and returns 404 (not a permission error) on mismatch so a
    caller cannot distinguish "wrong tenant" from "does not exist" — no
    cross-tenant existence leakage.
    """
    tenant_code = _authorize_path_tenant(event, path_tenant_code)

    if not RUN_ID_PATTERN.match(run_id):
        raise ValidationFailedError(f"run_id {run_id!r} does not conform to the expected format.")
    try:
        validate_run_id(run_id)
    except ValueError as exc:
        raise ValidationFailedError(str(exc)) from exc

    table = _run_audit_log_table()
    try:
        response = table.query(KeyConditionExpression=Key("run_id").eq(run_id))
    except ClientError as exc:
        _logger.error(
            "run_status_query_failed", run_id=run_id, error_code=exc.response["Error"]["Code"]
        )
        raise ApiError("Failed to query run status due to an internal error.") from exc

    items = response.get("Items", [])
    if not items:
        raise NotFoundError(f"No run found for run_id={run_id!r}.")

    # Tenant isolation (security-critical): a run belonging to a different
    # tenant is reported as not-found, never as a permission error.
    if items[0].get("tenant_code") != tenant_code:
        raise NotFoundError(f"No run found for run_id={run_id!r}.")

    stages = [
        {
            "stage": item.get("stage"),
            "status": item.get("status"),
            "completed_at": item.get("completed_at"),
            "duration_ms": item.get("duration_ms"),
            "error_code": item.get("error_code"),
        }
        for item in items
    ]
    return _response(
        200,
        {
            "tenant_code": tenant_code,
            "run_id": run_id,
            "status": _summarize_run_status(stages),
            "stages": stages,
        },
    )


def _handle_list_runs(event: dict[str, Any], path_tenant_code: str) -> dict[str, Any]:
    """
    GET /tenants/{tenant_code}/runs — list recent runs for a tenant.

    Implemented as a full table Scan with a FilterExpression on tenant_code:
    the run-audit-log table's only GSI today is `source-entity-time-index`
    (hash key source_entity_key, tenant-scoped as
    `{tenant_code}#{source_id}#{entity_id}` — see
    infrastructure/modules/metadata_persistence/main.tf), which serves
    per-entity lookups, not tenant-wide listing. A tenant-code GSI would make
    this an efficient Query at scale and is tracked as follow-up infra work,
    not built speculatively here.
    """
    tenant_code = _authorize_path_tenant(event, path_tenant_code)

    table = _run_audit_log_table()
    scan_kwargs: dict[str, Any] = {
        "FilterExpression": "tenant_code = :tc",
        "ExpressionAttributeValues": {":tc": tenant_code},
    }
    items: list[dict[str, Any]] = []
    try:
        while True:
            page = table.scan(**scan_kwargs)
            items.extend(page.get("Items", []))
            last_key = page.get("LastEvaluatedKey")
            if not last_key or len(items) >= _MAX_RUNS_LISTED:
                break
            scan_kwargs["ExclusiveStartKey"] = last_key
    except ClientError as exc:
        _logger.error(
            "list_runs_scan_failed",
            tenant_code=tenant_code,
            error_code=exc.response["Error"]["Code"],
        )
        raise ApiError("Failed to list runs due to an internal error.") from exc

    # Collapse to one summary row per run_id (a run has one item per stage);
    # keep the item with the most recent completed_at as the run's latest state.
    runs_by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        run_id = str(item.get("run_id"))
        existing = runs_by_id.get(run_id)
        if existing is None or str(item.get("completed_at") or "") > str(
            existing.get("completed_at") or ""
        ):
            runs_by_id[run_id] = {
                "run_id": run_id,
                "source_id": item.get("source_id"),
                "entity_id": item.get("entity_id"),
                "latest_stage": item.get("stage"),
                "status": item.get("status"),
                "completed_at": item.get("completed_at"),
            }
    runs = sorted(
        runs_by_id.values(), key=lambda r: str(r.get("completed_at") or ""), reverse=True
    )[:_MAX_RUNS_LISTED]
    return _response(200, {"tenant_code": tenant_code, "runs": runs, "count": len(runs)})


# ---------------------------------------------------------------------------
# Intelligence layer — twins, semantic queries, saved queries
# ---------------------------------------------------------------------------

_SAFE_GOLDEN_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,255}$")
_MAX_TWINS_LISTED: Final[int] = 200


def _twin_repository() -> TwinRepository:
    return TwinRepository(region_name=_region())


def _saved_query_repository() -> SavedQueryRepository:
    return SavedQueryRepository(region_name=_region())


def _semantic_model_repository() -> SemanticModelRepository:
    return SemanticModelRepository(region_name=_region())


def _authenticated_user(event: dict[str, Any]) -> str:
    claims = _extract_claims(event) or {}
    return str(
        claims.get("sub") or claims.get("email") or claims.get("cognito:username") or "unknown"
    )


def _granted_access_tags(event: dict[str, Any]) -> frozenset[str]:
    # OWASP A01: data-level access tags come from verified authorizer claims, never the body.
    claims = _extract_claims(event) or {}
    raw = str(claims.get("custom:access_tags") or claims.get("access_tags") or "")
    return frozenset(tag.strip() for tag in raw.split(",") if tag.strip())


def _twin_to_dict(twin: Any) -> dict[str, Any]:
    return {
        "entity_type": twin.entity_type,
        "golden_id": twin.golden_id,
        "lifecycle_stage": twin.lifecycle_stage,
        "rollups": twin.rollups,
        "edges": [
            {
                "relationship_type": edge.relationship_type,
                "to_entity_type": edge.to_entity_type,
                "to_golden_id": edge.to_golden_id,
            }
            for edge in twin.edges
        ],
    }


def _saved_query_to_dict(saved_query: Any) -> dict[str, Any]:
    return {
        "query_id": saved_query.query_id,
        "name": saved_query.name,
        "entity": saved_query.entity,
        "metrics": list(saved_query.metrics),
        "dimensions": list(saved_query.dimensions),
        "created_by": saved_query.created_by,
    }


def _load_active_model(tenant_code: str) -> SemanticModel:
    try:
        return _semantic_model_repository().load_active(tenant_code)
    except SemanticModelNotFoundError as exc:
        raise NotFoundError("No active semantic model is published for this tenant.") from exc


def _semantic_query_service(
    event: dict[str, Any], tenant_code: str, model: SemanticModel
) -> SemanticQueryService:
    region = _region()
    analytics_bucket = require_env("ANALYTICS_S3_BUCKET")
    s3 = boto3.client("s3", region_name=region)
    engine = set_based_engine_registry.build("duckdb", region_name=region)

    def _resolve_entity_uri(entity_name: str) -> str:
        entity = model.entity(entity_name)
        uri = latest_partition_uri(s3, analytics_bucket, tenant_code, entity.entity_type)
        if uri is None:
            raise NotFoundError(f"No analytics data is available for entity {entity_name!r}.")
        return uri

    return SemanticQueryService(
        model=model,
        engine=engine,
        entity_uri_resolver=_resolve_entity_uri,
        granted_access_tags=_granted_access_tags(event),
    )


def _run_query(service: SemanticQueryService, request: SemanticQueryRequest) -> dict[str, Any]:
    try:
        result = service.run(request)
    except AccessDeniedError as exc:
        raise AuthorizationError(str(exc)) from exc
    except SemanticQueryError as exc:
        raise ValidationFailedError(str(exc)) from exc
    return {"sql": result.sql, "rows": result.rows, "row_count": len(result.rows)}


def _handle_get_twin(
    event: dict[str, Any], path_tenant_code: str, entity_type: str, golden_id: str
) -> dict[str, Any]:
    """GET /tenants/{tenant_code}/twins/{entity_type}/{golden_id} — one twin."""
    tenant_code = _authorize_path_tenant(event, path_tenant_code)
    if not ENTITY_TYPE_PATTERN.match(entity_type):
        raise ValidationFailedError(f"entity_type {entity_type!r} is not valid.")
    if not _SAFE_GOLDEN_ID.match(golden_id):
        raise ValidationFailedError(f"golden_id {golden_id!r} is not valid.")
    try:
        twin = _twin_repository().get_twin(tenant_code, entity_type, golden_id)
    except TwinNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    return _response(200, {"tenant_code": tenant_code, **_twin_to_dict(twin)})


def _handle_list_twins(
    event: dict[str, Any], path_tenant_code: str, entity_type: str
) -> dict[str, Any]:
    """GET /tenants/{tenant_code}/twins/{entity_type} — twins for an entity type (capped)."""
    tenant_code = _authorize_path_tenant(event, path_tenant_code)
    if not ENTITY_TYPE_PATTERN.match(entity_type):
        raise ValidationFailedError(f"entity_type {entity_type!r} is not valid.")
    twins = _twin_repository().list_twins(tenant_code, entity_type)[:_MAX_TWINS_LISTED]
    return _response(
        200,
        {
            "tenant_code": tenant_code,
            "entity_type": entity_type,
            "twins": [_twin_to_dict(twin) for twin in twins],
            "count": len(twins),
        },
    )


def _handle_run_semantic_query(event: dict[str, Any], path_tenant_code: str) -> dict[str, Any]:
    """POST /tenants/{tenant_code}/semantic/query — run a structured semantic query."""
    tenant_code = _authorize_path_tenant(event, path_tenant_code)
    body_dict = _parse_json_body(event)
    try:
        body = SemanticQueryBody.model_validate(body_dict)
    except PydanticValidationError as exc:
        raise ValidationFailedError(
            f"Request body failed validation: {exc.error_count()} error(s)."
        ) from exc
    model = _load_active_model(tenant_code)
    service = _semantic_query_service(event, tenant_code, model)
    payload = _run_query(
        service,
        SemanticQueryRequest(
            entity=body.entity, metrics=tuple(body.metrics), dimensions=tuple(body.dimensions)
        ),
    )
    return _response(200, {"tenant_code": tenant_code, **payload})


def _handle_create_saved_query(event: dict[str, Any], path_tenant_code: str) -> dict[str, Any]:
    """POST /tenants/{tenant_code}/saved-queries — create a reusable saved query."""
    tenant_code = _authorize_path_tenant(event, path_tenant_code)
    body_dict = _parse_json_body(event)
    try:
        body = SavedQueryCreateBody.model_validate(body_dict)
        saved_query = SavedQuery(
            query_id=body.query_id,
            name=body.name,
            entity=body.entity,
            metrics=tuple(body.metrics),
            dimensions=tuple(body.dimensions),
            created_by=_authenticated_user(event),
        )
    except PydanticValidationError as exc:
        raise ValidationFailedError(
            f"Saved query failed validation: {exc.error_count()} error(s)."
        ) from exc
    _saved_query_repository().save(tenant_code, saved_query)
    _logger.info("saved_query_created", tenant_code=tenant_code, query_id=saved_query.query_id)
    return _response(
        201,
        {"tenant_code": tenant_code, "query_id": saved_query.query_id, "name": saved_query.name},
    )


def _handle_list_saved_queries(event: dict[str, Any], path_tenant_code: str) -> dict[str, Any]:
    """GET /tenants/{tenant_code}/saved-queries — list saved queries."""
    tenant_code = _authorize_path_tenant(event, path_tenant_code)
    saved = _saved_query_repository().list_for_tenant(tenant_code)
    return _response(
        200,
        {
            "tenant_code": tenant_code,
            "saved_queries": [_saved_query_to_dict(query) for query in saved],
            "count": len(saved),
        },
    )


def _handle_get_saved_query(
    event: dict[str, Any], path_tenant_code: str, query_id: str
) -> dict[str, Any]:
    """GET /tenants/{tenant_code}/saved-queries/{query_id} — one saved query."""
    tenant_code = _authorize_path_tenant(event, path_tenant_code)
    if not STABLE_ID_PATTERN.match(query_id):
        raise ValidationFailedError(f"query_id {query_id!r} is not valid.")
    try:
        saved_query = _saved_query_repository().get(tenant_code, query_id)
    except SavedQueryNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    return _response(200, {"tenant_code": tenant_code, **_saved_query_to_dict(saved_query)})


def _handle_run_saved_query(
    event: dict[str, Any], path_tenant_code: str, query_id: str
) -> dict[str, Any]:
    """POST /tenants/{tenant_code}/saved-queries/{query_id}/run — run a saved query."""
    tenant_code = _authorize_path_tenant(event, path_tenant_code)
    if not STABLE_ID_PATTERN.match(query_id):
        raise ValidationFailedError(f"query_id {query_id!r} is not valid.")
    try:
        saved_query = _saved_query_repository().get(tenant_code, query_id)
    except SavedQueryNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    model = _load_active_model(tenant_code)
    service = _semantic_query_service(event, tenant_code, model)
    payload = _run_query(service, saved_query.to_request())
    return _response(200, {"tenant_code": tenant_code, "query_id": query_id, **payload})


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Route:
    method: str
    length: int
    resource: str
    tail: str | None
    handler: Callable[[dict[str, Any], list[str]], dict[str, Any]]

    def matches(self, method: str, segments: list[str]) -> bool:
        return (
            method == self.method
            and len(segments) == self.length
            and segments[0] == "tenants"
            and segments[2] == self.resource
            and (self.tail is None or segments[-1] == self.tail)
        )


# Intelligence-layer routes kept in a table so _route stays within the complexity gate.
_INTELLIGENCE_ROUTES: tuple[_Route, ...] = (
    _Route("GET", 5, "twins", None, lambda e, s: _handle_get_twin(e, s[1], s[3], s[4])),
    _Route("GET", 4, "twins", None, lambda e, s: _handle_list_twins(e, s[1], s[3])),
    _Route("POST", 4, "semantic", "query", lambda e, s: _handle_run_semantic_query(e, s[1])),
    _Route("GET", 3, "saved-queries", None, lambda e, s: _handle_list_saved_queries(e, s[1])),
    _Route("POST", 3, "saved-queries", None, lambda e, s: _handle_create_saved_query(e, s[1])),
    _Route("GET", 4, "saved-queries", None, lambda e, s: _handle_get_saved_query(e, s[1], s[3])),
    _Route("POST", 5, "saved-queries", "run", lambda e, s: _handle_run_saved_query(e, s[1], s[3])),
)


def _route_intelligence_layer(
    event: dict[str, Any], method: str, segments: list[str]
) -> dict[str, Any] | None:
    for route in _INTELLIGENCE_ROUTES:
        if route.matches(method, segments):
            return route.handler(event, segments)
    return None


def _route(event: dict[str, Any]) -> dict[str, Any]:
    method = str(event.get("httpMethod", "")).upper()
    path = str(event.get("path") or event.get("rawPath") or "")
    segments = [segment for segment in path.split("/") if segment]

    if method == "POST" and segments == ["tenants"]:
        return _handle_create_tenant(event)

    if len(segments) == 3 and segments[0] == "tenants" and segments[2] == "entities":
        if method == "GET":
            return _handle_list_entities(event, segments[1])
        if method == "POST":
            return _handle_create_entity(event, segments[1])

    if (
        len(segments) == 4
        and segments[0] == "tenants"
        and segments[2] == "pipelines"
        and segments[3] == "trigger"
        and method == "POST"
    ):
        return _handle_trigger_pipeline(event, segments[1])

    if (
        len(segments) == 4
        and segments[0] == "tenants"
        and segments[2] == "runs"
        and method == "GET"
    ):
        return _handle_get_run(event, segments[1], segments[3])

    if (
        len(segments) == 3
        and segments[0] == "tenants"
        and segments[2] == "runs"
        and method == "GET"
    ):
        return _handle_list_runs(event, segments[1])

    intelligence_response = _route_intelligence_layer(event, method, segments)
    if intelligence_response is not None:
        return intelligence_response

    raise NotFoundError(f"No route matches {method} {path!r}.")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entry point — API Gateway Lambda-proxy integration."""
    try:
        return _route(event)
    except ApiError as exc:
        return _error_response(exc)
    except Exception:
        _logger.error(
            "control_plane_unhandled_exception",
            path=event.get("path"),
            method=event.get("httpMethod"),
        )
        return _response(500, {"error": "An internal error occurred."})
