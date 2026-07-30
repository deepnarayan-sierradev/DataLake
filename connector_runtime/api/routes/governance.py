"""
Config- and semantic-governance routes: effective config, restatements, rollback, model versions,
metric lineage, and reprocess.

Split out of `control_plane_handler.py` (F11). These are the routes an operator uses to inspect and
correct configuration, and they were the largest single block in a 1,361-line module. Grouping them
here means a new governance route widens this file rather than the dispatcher, and the dispatcher
stays readable enough that a route added without its isolation control is visible in review.

Security (OWASP A01, A09): every handler authorises the path tenant through the request kernel
before touching a repository, and rollback is maker-checker — the approver may not be the requester.
"""

from __future__ import annotations

import dataclasses
from datetime import date
from typing import Any

from config_propagation.capability import ConfigCapability
from config_propagation.config_rollback import (
    ConfigGovernanceService,
    MakerCheckerViolationError,
    RollbackRequest,
)
from config_propagation.effective_config_repository import EffectiveConfigRepository
from config_propagation.restatement_repository import RestatementRepository
from connector_runtime.api.config_governance_routes import (
    ConfigRouteError,
    ReprocessRequestParams,
    RollbackRequestParams,
    parse_capability,
    parse_entity_key,
)
from connector_runtime.api.errors import (
    NotFoundError,
    ValidationFailedError,
)
from connector_runtime.api.request_context import (
    authorize_path_tenant,
    environment,
    json_response,
    parse_json_body,
    region,
)
from contracts.platform_metrics import PlatformMetric
from entity_resolution.resolution_config.resolution_config_registry import (
    ResolutionConfigRegistry,
)
from observability.lambda_runtime import require_env
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger
from semantic.metric_lineage import metric_lineage
from semantic.model_governance import SemanticModelGovernance
from semantic.semantic_model_repository import (
    SemanticModelNotFoundError,
    SemanticModelRepository,
)

_logger = get_platform_logger(__name__)


def load_active_model(tenant_code: str) -> Any:
    """
    The tenant's published semantic model, or a 404.

    Lives here rather than with the intelligence routes because the published-model lifecycle is a
    governance concern — publish, version, roll back — and both route groups read through it.
    """
    try:
        return SemanticModelRepository(region_name=region()).load_active(tenant_code)
    except SemanticModelNotFoundError as exc:
        raise NotFoundError("No active semantic model is published for this tenant.") from exc


def effective_config_repository() -> EffectiveConfigRepository:
    return EffectiveConfigRepository(environment=environment(), region_name=region())


def handle_list_effective_config(event: dict[str, Any], path_tenant: str) -> dict[str, Any]:
    """GET /tenants/{t}/config/effective — every capability's in-effect version."""
    tenant_code = authorize_path_tenant(event, path_tenant)
    records = effective_config_repository().list_effective(tenant_code)
    return json_response(200, {"tenant_code": tenant_code, "effective": records})


def handle_get_effective_config(
    event: dict[str, Any], path_tenant: str, raw_capability: str, raw_entity: str
) -> dict[str, Any]:
    """GET /tenants/{t}/config/effective/{capability}/{entity_key} — one capability."""
    tenant_code = authorize_path_tenant(event, path_tenant)
    try:
        capability = parse_capability(raw_capability)
        entity_key = parse_entity_key(raw_entity)
    except ConfigRouteError as exc:
        raise ValidationFailedError(str(exc)) from exc
    repository = effective_config_repository()
    record = repository.get_effective(tenant_code, capability, entity_key)
    if record is None:
        raise NotFoundError(
            f"No version of {capability.value!r} has been consumed for {entity_key!r} yet."
        )
    return json_response(
        200,
        {
            "tenant_code": tenant_code,
            "effective": record,
            "propagation_lag_seconds": repository.propagation_lag_seconds(
                tenant_code, capability, entity_key
            ),
        },
    )


def handle_list_restatements(event: dict[str, Any], path_tenant: str) -> dict[str, Any]:
    """GET /tenants/{t}/config/restatements — why a historical figure changed (DL-CFG-13)."""
    tenant_code = authorize_path_tenant(event, path_tenant)
    events = RestatementRepository(
        environment=environment(), region_name=region()
    ).list_restatements(tenant_code)
    return json_response(200, {"tenant_code": tenant_code, "restatements": events})


def handle_config_rollback(
    event: dict[str, Any], path_tenant: str, raw_capability: str, raw_entity: str
) -> dict[str, Any]:
    """POST /tenants/{t}/config/{capability}/{entity_key}/rollback — audited, maker-checker."""
    tenant_code = authorize_path_tenant(event, path_tenant)
    body = parse_json_body(event)
    try:
        params = RollbackRequestParams(
            capability=parse_capability(raw_capability),
            entity_key=parse_entity_key(raw_entity),
            target_version=str(body.get("target_version") or ""),
            requested_by=str(body.get("requested_by") or ""),
            approved_by=str(body.get("approved_by") or ""),
        )
    except ConfigRouteError as exc:
        raise ValidationFailedError(str(exc)) from exc

    service = ConfigGovernanceService(
        environment=environment(),
        region_name=region(),
        pointer_store=resolution_pointer_store(),
    )
    try:
        result = service.rollback(
            RollbackRequest(
                tenant_code=tenant_code,
                capability=params.capability,
                entity_key=params.entity_key,
                target_version=params.target_version,
                requested_by=params.requested_by,
                approved_by=params.approved_by,
                correlation_id=str(event.get("requestContext", {}).get("requestId", "")),
            )
        )
    except MakerCheckerViolationError as exc:
        raise ValidationFailedError(str(exc)) from exc
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc
    return json_response(
        200,
        {
            "tenant_code": tenant_code,
            "rollback_id": result.rollback_id,
            "previous_version": result.previous_version,
            "target_version": result.target_version,
        },
    )


def handle_metric_lineage(
    event: dict[str, Any], path_tenant: str, metric_name: str
) -> dict[str, Any]:
    """GET /tenants/{t}/semantic/metrics/{metric}/lineage — columns, joins, filters (DL-SEM-10)."""
    tenant_code = authorize_path_tenant(event, path_tenant)
    model = load_active_model(tenant_code)
    for entity in model.entities:
        for metric in entity.metrics:
            if metric.name == metric_name:
                return json_response(
                    200,
                    {
                        "tenant_code": tenant_code,
                        "lineage": dataclasses.asdict(
                            metric_lineage(model, entity.name, metric_name)
                        ),
                    },
                )
    raise NotFoundError(f"Metric {metric_name!r} is not in the active semantic model.")


def handle_list_model_versions(event: dict[str, Any], path_tenant: str) -> dict[str, Any]:
    """GET /tenants/{t}/semantic/model/versions — publish history and status."""
    tenant_code = authorize_path_tenant(event, path_tenant)
    governance = SemanticModelGovernance(
        environment=environment(), region_name=region(), s3_bucket=curated_bucket()
    )
    return json_response(
        200, {"tenant_code": tenant_code, "versions": governance.list_versions(tenant_code)}
    )


def curated_bucket() -> str:
    return require_env("CURATED_S3_BUCKET")


def resolution_pointer_store() -> Any:
    """
    Adapt the resolution-config registry to the `PointerStore` protocol a rollback needs.

    Only entity-resolution pointers are rollback-able today, because it is the only capability
    whose versions this system stores. A rollback naming another capability fails on
    `version_exists`, which is the correct answer rather than a silent no-op.
    """
    registry = ResolutionConfigRegistry(s3_bucket=curated_bucket(), region_name=region())

    class _RegistryPointerStore:
        def read_pointer(self, tenant_code: str, capability: ConfigCapability, key: str) -> str:
            return registry.resolved_version(tenant_code, key)

        def write_pointer(
            self, tenant_code: str, capability: ConfigCapability, key: str, version: str
        ) -> None:
            registry.repoint_latest(tenant_code, key, version)

        def version_exists(
            self, tenant_code: str, capability: ConfigCapability, key: str, version: str
        ) -> bool:
            return registry.version_exists(tenant_code, key, version)

    return _RegistryPointerStore()


def handle_active_model(event: dict[str, Any], path_tenant: str) -> dict[str, Any]:
    """GET /tenants/{t}/semantic/model — the active model's shape, for the console."""
    tenant_code = authorize_path_tenant(event, path_tenant)
    model = load_active_model(tenant_code)
    return json_response(
        200,
        {
            "tenant_code": tenant_code,
            "model_version": model.model_version,
            "entities": [
                {
                    "name": entity.name,
                    "entity_type": entity.entity_type,
                    "metrics": [metric.name for metric in entity.metrics],
                    "dimensions": [dimension.name for dimension in entity.dimensions],
                }
                for entity in model.entities
            ],
        },
    )


def handle_config_reprocess(
    event: dict[str, Any], path_tenant: str, raw_capability: str, raw_entity: str
) -> dict[str, Any]:
    """
    POST /tenants/{t}/config/{capability}/{entity_key}/reprocess — bounded historical replay.

    Validated and recorded here; the replay itself is a pipeline run, so the response returns the
    accepted window rather than pretending the recompute finished synchronously.
    """
    tenant_code = authorize_path_tenant(event, path_tenant)
    body = parse_json_body(event)
    try:
        params = ReprocessRequestParams(
            capability=parse_capability(raw_capability),
            entity_key=parse_entity_key(raw_entity),
            window_start=date.fromisoformat(str(body.get("window_start") or "")),
            window_end=date.fromisoformat(str(body.get("window_end") or "")),
            reason=str(body.get("reason") or ""),
            pinned_config_version=str(body.get("pinned_config_version") or ""),
        )
    except (ConfigRouteError, ValueError) as exc:
        raise ValidationFailedError(str(exc)) from exc

    retention_days = body.get("retention_days")
    try:
        params.guard_retention(int(retention_days) if retention_days is not None else None)
    except Exception as exc:
        raise ValidationFailedError(str(exc)) from exc

    _logger.warning(
        "config_reprocess_accepted",
        tenant_code=tenant_code,
        capability=params.capability.value,
        entity_key=params.entity_key,
        window_days=params.window_days,
        pinned_config_version=params.pinned_config_version,
        reason=params.reason,
    )
    record_platform_metric(
        PlatformMetric.ADMIN_ACTIONS, 1.0, Capability=f"reprocess_{params.capability.value}"
    )
    return json_response(
        202,
        {
            "tenant_code": tenant_code,
            "capability": params.capability.value,
            "entity_key": params.entity_key,
            "window_days": params.window_days,
            "pinned_config_version": params.pinned_config_version,
            "status": "accepted",
        },
    )
