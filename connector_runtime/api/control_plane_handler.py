"""
Control-plane API Lambda handler for the Enterprise Data Lake SaaS platform.

Single Lambda dispatches all control-plane routes based on (httpMethod, path)
from an API Gateway Lambda-proxy event:

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
  PLATFORM_ENVIRONMENT         — deployment environment (dev/uat/prod)
  PIPELINE_TRIGGER_QUEUE_URL   — URL of the datalake-pipeline-trigger-dev.fifo queue
  ENTITY_CONFIG_TABLE          — optional override; defaults to
                                  datalake-entity-extraction-config-dev
  ENTITY_TYPE_REGISTRY_TABLE   — optional override; defaults to
                                  datalake-entity-type-registry-dev
  AUDIT_LOG_TABLE              — optional override; defaults to
                                  datalake-run-audit-log-dev
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import boto3
import structlog
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from pydantic import ValidationError as PydanticValidationError

import processing_engine.engines.duckdb_engine  # noqa: F401  (registers "duckdb")
from analytics_publisher.analytics_location import latest_partition_uri
from connector_runtime.api.config_governance_routes import (
    ConfigRoute,
    build_config_routes,
    match_config_route,
)
from connector_runtime.api.errors import (
    ApiError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationFailedError,
)
from connector_runtime.api.models import (
    PipelineTriggerRequest,
    SavedQueryCreateBody,
    SemanticQueryBody,
)
from connector_runtime.api.request_context import (
    authenticated_user as _authenticated_user,
)
from connector_runtime.api.request_context import (
    authorize_path_tenant as _authorize_path_tenant,
)
from connector_runtime.api.request_context import (
    decode_page_token as _decode_page_token,
)
from connector_runtime.api.request_context import (
    encode_page_token as _encode_page_token,
)
from connector_runtime.api.request_context import (
    environment as _environment,
)
from connector_runtime.api.request_context import (
    error_response as _error_response,
)
from connector_runtime.api.request_context import (
    granted_access_tags as _granted_access_tags,
)
from connector_runtime.api.request_context import (
    json_response as _response,
)
from connector_runtime.api.request_context import (
    parse_json_body as _parse_json_body,
)
from connector_runtime.api.request_context import (
    region as _region,
)
from connector_runtime.api.request_context import (
    scope_predicate_for as _scope_predicate_for,
)
from connector_runtime.api.routes.governance import (
    handle_active_model as _handle_active_model,
)
from connector_runtime.api.routes.governance import (
    handle_config_reprocess as _handle_config_reprocess,
)
from connector_runtime.api.routes.governance import (
    handle_config_rollback as _handle_config_rollback,
)
from connector_runtime.api.routes.governance import (
    handle_get_effective_config as _handle_get_effective_config,
)
from connector_runtime.api.routes.governance import (
    handle_list_effective_config as _handle_list_effective_config,
)
from connector_runtime.api.routes.governance import (
    handle_list_model_versions as _handle_list_model_versions,
)
from connector_runtime.api.routes.governance import (
    handle_list_restatements as _handle_list_restatements,
)
from connector_runtime.api.routes.governance import (
    handle_metric_lineage as _handle_metric_lineage,
)
from connector_runtime.api.routes.governance import (
    load_active_model as _load_active_model,
)
from connector_runtime.api.routes.operations import (
    handle_list_quality_exceptions as _handle_list_quality_exceptions,
)
from connector_runtime.api.routes.operations import (
    handle_onboard_serving_entity as _handle_onboard_serving_entity,
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
)
from knowledge.twin_repository import TwinNotFoundError, TwinRepository
from observability.lambda_runtime import require_env
from observability.structured_logger import get_platform_logger
from persistence.dynamodb_paging import PagingError, fetch_page, index_available
from processing_engine.registry import set_based_engine_registry
from semantic.query_compiler import (
    AccessDeniedError,
    SemanticQueryError,
)
from semantic.saved_query import SavedQuery
from semantic.saved_query_repository import SavedQueryNotFoundError, SavedQueryRepository
from semantic.semantic_model import SemanticModel
from semantic.semantic_model_repository import (
    SemanticModelRepository,
)
from semantic.semantic_query_service import SemanticQueryService
from tenancy.scope_predicate import (
    ConsumptionSurface,
    ScopePredicate,
)

_logger = get_platform_logger(__name__)

_MAX_RUNS_LISTED: Final[int] = 50

_MAX_ENTITIES_LISTED: Final[int] = 100

_AUDIT_ROWS_PER_READ: Final[int] = 400

_AUDIT_TENANT_INDEX: Final[str] = "tenant-started-index"

_INDEX_PRESENCE: dict[str, bool] = {}


def _entity_type_registry_table() -> Any:
    dynamodb = boto3.resource("dynamodb", region_name=_region())
    table_name = require_env("ENTITY_TYPE_REGISTRY_TABLE")
    return dynamodb.Table(table_name)


def _run_audit_log_table() -> Any:
    dynamodb = boto3.resource("dynamodb", region_name=_region())
    table_name = require_env("AUDIT_LOG_TABLE")
    return dynamodb.Table(table_name)


def _configuration_repository() -> ConfigurationRepositoryClient:
    return ConfigurationRepositoryClient(
        environment=_environment(), region_name=_region(), backend=ConfigurationBackend.DYNAMODB
    )


def _handle_list_entities(event: dict[str, Any], path_tenant_code: str) -> dict[str, Any]:
    """
    GET /tenants/{tenant_code}/entities — one page of configured entities.

    Bounded and cursored since 2026-07-29. This drained every config for the tenant and, because
    the GSI projected `KEYS_ONLY`, issued one `GetItem` per entity to rehydrate each — so the
    platform's most-used endpoint made 100+ serial round trips at the target entity count, with no
    cap and no way for a console to page.
    """
    tenant_code = _authorize_path_tenant(event, path_tenant_code)
    repo = _configuration_repository()
    configs, next_key = repo.page_configs_for_tenant(
        tenant_code,
        limit=_MAX_ENTITIES_LISTED,
        start_key=_decode_page_token(event, tenant_code),
    )
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
        200,
        {
            "tenant_code": tenant_code,
            "entities": entities,
            "count": len(entities),
            "next_token": _encode_page_token(next_key, tenant_code),
        },
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
    GET /tenants/{tenant_code}/runs — one page of recent runs for a tenant.

    Queries `tenant-started-index` (hash `tenant_code`, range `started_at`, projection ALL), falling
    back to a Scan while an environment has not applied the index, so code can deploy before the
    Terraform does.

    **Pages by run, not by audit row.** A run writes one item per stage, so the previous cap of 50
    *items* returned roughly four runs — and reported that as `count` with no cursor to reach the
    rest. Rows are accumulated until `_MAX_RUNS_LISTED` distinct `run_id`s are complete, and the
    cursor points at the next unread row so a client can page without re-reading.

    `ScanIndexForward=False` keeps the *most recent* runs: the Scan fallback kept whatever came back
    first and sorted afterwards, so a capped response could omit runs newer than the ones it showed.
    """
    tenant_code = _authorize_path_tenant(event, path_tenant_code)

    table = _run_audit_log_table()
    use_index = index_available(table, _AUDIT_TENANT_INDEX, _INDEX_PRESENCE)
    read_kwargs: dict[str, Any] = (
        {
            "IndexName": _AUDIT_TENANT_INDEX,
            "KeyConditionExpression": Key("tenant_code").eq(tenant_code),
            "ScanIndexForward": False,
        }
        if use_index
        else {
            "FilterExpression": "tenant_code = :tc",
            "ExpressionAttributeValues": {":tc": tenant_code},
        }
    )

    runs_by_id: dict[str, dict[str, Any]] = {}
    next_key: dict[str, Any] | None = None
    try:
        start_key = _decode_page_token(event, tenant_code)
        stopped_early = False
        while not stopped_early:
            page = fetch_page(
                table,
                limit=_AUDIT_ROWS_PER_READ,
                start_key=start_key,
                use_query=use_index,
                **read_kwargs,
            )
            for item in page.items:
                run_id = str(item.get("run_id"))
                if run_id not in runs_by_id and len(runs_by_id) >= _MAX_RUNS_LISTED:
                    next_key = _audit_row_key(item, use_index)
                    stopped_early = True
                    break
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
            if stopped_early:
                break
            if page.next_key is None:
                next_key = None
                break
            start_key = page.next_key
    except PagingError as exc:
        _logger.error("list_runs_read_failed", tenant_code=tenant_code, used_index=use_index)
        raise ApiError("Failed to list runs due to an internal error.") from exc

    runs = sorted(runs_by_id.values(), key=lambda r: str(r.get("completed_at") or ""), reverse=True)
    return _response(
        200,
        {
            "tenant_code": tenant_code,
            "runs": runs,
            "count": len(runs),
            "next_token": _encode_page_token(next_key, tenant_code),
        },
    )


def _audit_row_key(item: dict[str, Any], use_index: bool) -> dict[str, Any]:
    """
    The exclusive-start key for one audit row: exactly the attributes DynamoDB accepts, no more.

    A GSI read's key is the index key *plus* the base-table key; a Scan's is the base key alone.
    This previously included `tenant_code` unconditionally so `decode_page_token` could verify
    ownership — but on the Scan fallback `tenant_code` is not part of `datalake-run-audit-log-dev`'s
    key schema,
    and DynamoDB validates `ExclusiveStartKey` against that schema. The suite could not see it
    because moto accepts non-key attributes there.

    Ownership is now verified by the token envelope instead, so the key carries only key attributes.
    """
    key: dict[str, Any] = {"run_id": item.get("run_id"), "stage": item.get("stage")}
    if use_index:
        key["tenant_code"] = item.get("tenant_code")
        key["started_at"] = item.get("started_at")
    return key


_SAFE_GOLDEN_ID: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,255}$")
_MAX_TWINS_LISTED: Final[int] = 200
_MAX_EXCEPTIONS_LISTED: Final[int] = 200


def _twin_repository() -> TwinRepository:
    return TwinRepository(region_name=_region())


def _saved_query_repository() -> SavedQueryRepository:
    return SavedQueryRepository(region_name=_region())


def _semantic_model_repository() -> SemanticModelRepository:
    return SemanticModelRepository(region_name=_region())


def _twin_to_dict(twin: Any, predicate: ScopePredicate) -> dict[str, Any]:
    """
    One twin, with its edges filtered to the caller's units.

    DL-SCOPE-13: an edge names the target, so fan-out is filtered too — a node the caller may
    see can still point at another unit's entity, and listing that edge discloses it exists.

    The suppressed *count* is deliberately not returned. `edges_hidden_by_scope` and the listing's
    `hidden_by_scope` let a franchisee enumerate how many peer entities and relationships exist,
    which is a weaker form of the same disclosure this filter exists to prevent — and the same
    reasoning that removed `total_visible`. Both counts are logged and metered instead, where an
    operator needs them and a tenant cannot read them.
    """
    visible_edges = [edge for edge in twin.edges if predicate.matches(edge.scope_unit_id)]
    hidden = len(twin.edges) - len(visible_edges)
    if hidden:
        _logger.info(
            "twin_edges_hidden_by_scope",
            entity_type=twin.entity_type,
            golden_id=twin.golden_id,
            hidden_edges=hidden,
        )
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
            for edge in visible_edges
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
        "filters": [
            {
                "dimension": f.dimension,
                "operator": f.operator,
                "value": f.value,
                "values": list(f.values),
            }
            for f in saved_query.filters
        ],
        "joined_dimensions": [list(pair) for pair in saved_query.joined_dimensions],
        "time_dimension": saved_query.time_dimension,
        "time_grain": saved_query.time_grain.value if saved_query.time_grain else None,
        "time_comparison": saved_query.time_comparison.value,
        "row_limit": saved_query.row_limit,
    }


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
        scope_predicate=_scope_predicate_for(event, tenant_code, ConsumptionSurface.SEMANTIC_QUERY),
    )


def _run_query(service: SemanticQueryService, request: Any) -> dict[str, Any]:
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
        predicate = _scope_predicate_for(event, tenant_code, ConsumptionSurface.TWIN_TRAVERSAL)
        if not predicate.matches(twin.scope_unit_id):
            raise NotFoundError(f"No twin for {entity_type}/{golden_id}.")
    except TwinNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc
    return _response(200, {"tenant_code": tenant_code, **_twin_to_dict(twin, predicate)})


def _handle_list_twins(
    event: dict[str, Any], path_tenant_code: str, entity_type: str
) -> dict[str, Any]:
    """GET /tenants/{tenant_code}/twins/{entity_type} — one page of twins for an entity type."""
    tenant_code = _authorize_path_tenant(event, path_tenant_code)
    if not ENTITY_TYPE_PATTERN.match(entity_type):
        raise ValidationFailedError(f"entity_type {entity_type!r} is not valid.")
    predicate = _scope_predicate_for(event, tenant_code, ConsumptionSurface.TWIN_TRAVERSAL)
    twins, next_key = _twin_repository().page_twins(
        tenant_code,
        entity_type,
        limit=_MAX_TWINS_LISTED,
        start_key=_decode_page_token(event, tenant_code),
    )
    visible = [twin for twin in twins if predicate.matches(twin.scope_unit_id)]
    if len(visible) != len(twins):
        _logger.info(
            "twins_hidden_by_scope",
            entity_type=entity_type,
            hidden_twins=len(twins) - len(visible),
        )
    return _response(
        200,
        {
            "tenant_code": tenant_code,
            "entity_type": entity_type,
            "twins": [_twin_to_dict(twin, predicate) for twin in visible],
            "count": len(visible),
            "next_token": _encode_page_token(next_key, tenant_code),
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
    try:
        request = body.to_request()
    except SemanticQueryError as exc:
        raise ValidationFailedError(str(exc)) from exc
    payload = _run_query(service, request)
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
            filters=tuple(filter_body.to_filter() for filter_body in body.filters),
            joined_dimensions=tuple(
                (joined.entity, joined.dimension) for joined in body.joined_dimensions
            ),
            time_dimension=body.time_dimension,
            time_grain=body.time_grain,
            time_comparison=body.time_comparison,
            time_range=body.time_range.to_filter() if body.time_range else None,
            row_limit=body.row_limit,
        )
    except PydanticValidationError as exc:
        raise ValidationFailedError(
            f"Saved query failed validation: {exc.error_count()} error(s)."
        ) from exc
    except SemanticQueryError as exc:
        raise ValidationFailedError(str(exc)) from exc
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


_INTELLIGENCE_ROUTES: tuple[_Route, ...] = (
    _Route("GET", 5, "twins", None, lambda e, s: _handle_get_twin(e, s[1], s[3], s[4])),
    _Route("GET", 4, "twins", None, lambda e, s: _handle_list_twins(e, s[1], s[3])),
    _Route("POST", 4, "semantic", "query", lambda e, s: _handle_run_semantic_query(e, s[1])),
    _Route("GET", 3, "saved-queries", None, lambda e, s: _handle_list_saved_queries(e, s[1])),
    _Route("POST", 3, "saved-queries", None, lambda e, s: _handle_create_saved_query(e, s[1])),
    _Route("GET", 4, "saved-queries", None, lambda e, s: _handle_get_saved_query(e, s[1], s[3])),
    _Route("POST", 5, "saved-queries", "run", lambda e, s: _handle_run_saved_query(e, s[1], s[3])),
    _Route(
        "GET",
        4,
        "quality",
        "exceptions",
        lambda e, s: _handle_list_quality_exceptions(e, s[1]),
    ),
    _Route(
        "POST",
        4,
        "serving-store",
        "entities",
        lambda e, s: _handle_onboard_serving_entity(e, s[1]),
    ),
)


_GOVERNANCE_ROUTES: tuple[ConfigRoute, ...] = build_config_routes(
    effective_config=lambda e, tenant: _handle_list_effective_config(e, tenant),
    effective_config_one=lambda e, tenant, capability, entity: _handle_get_effective_config(
        e, tenant, capability, entity
    ),
    rollback=lambda e, tenant, capability, entity: _handle_config_rollback(
        e, tenant, capability, entity
    ),
    reprocess=lambda e, tenant, capability, entity: _handle_config_reprocess(
        e, tenant, capability, entity
    ),
    restatements=lambda e, tenant: _handle_list_restatements(e, tenant),
    metric_lineage=lambda e, tenant, metric: _handle_metric_lineage(e, tenant, metric),
    model_versions=lambda e, tenant: _handle_list_model_versions(e, tenant),
    active_model=lambda e, tenant: _handle_active_model(e, tenant),
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

    governance_response = match_config_route(_GOVERNANCE_ROUTES, event, method, segments)
    if governance_response is not None:
        return governance_response

    raise NotFoundError(f"No route matches {method} {path!r}.")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """AWS Lambda entry point — API Gateway Lambda-proxy integration."""
    request_context = event.get("requestContext") or {}
    structlog.contextvars.bind_contextvars(
        request_id=str(request_context.get("requestId") or ""),
        path=str(event.get("path") or ""),
        method=str(event.get("httpMethod") or ""),
    )
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
    finally:
        structlog.contextvars.clear_contextvars()
