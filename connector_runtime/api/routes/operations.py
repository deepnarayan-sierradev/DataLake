"""
Operational routes: the data-quality triage inbox and serving-store onboarding.

Split out of `control_plane_handler.py` (F11).

The quality inbox is the only reader of the exception store — before it existed, every exception the
pipeline recorded was invisible to the operator expected to act on it. It pages with a keyset cursor
rather than draining, because that table's TTL is deliberately disabled to preserve audit evidence,
so its history grows with data volume (see docs/SCALE_AND_DLQ_THRESHOLDS.md).
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError as PydanticValidationError

from connector_runtime.api.errors import ValidationFailedError
from connector_runtime.api.request_context import (
    authorize_path_tenant,
    decode_page_token,
    encode_page_token,
    environment,
    json_response,
    parse_json_body,
    region,
)
from contracts.serving_store_config_contract import ServingStoreLoadConfig
from data_quality.exception_repository import DataQualityExceptionRepository
from observability.structured_logger import get_platform_logger
from serving_store.serving_store_config_repository import ServingStoreConfigRepositoryClient

_logger = get_platform_logger(__name__)

# One page of findings; the cursor carries the rest.
MAX_EXCEPTIONS_LISTED = 200


def handle_list_quality_exceptions(event: dict[str, Any], path_tenant: str) -> dict[str, Any]:
    """
    GET /tenants/{t}/quality/exceptions — the triage inbox (DL-DQ-13).

    Open findings only by default. The store had no reader at all before this route existed, so
    every exception the pipeline recorded was invisible to the operator expected to act on it.

    Two different reads on purpose: a run-scoped request is bounded by the run and drains, while
    the open-findings inbox is bounded by a cursor — the exception table has its TTL deliberately
    disabled to preserve audit evidence, so its history grows with data volume and cannot be read
    whole.
    """
    tenant_code = authorize_path_tenant(event, path_tenant)
    repository = DataQualityExceptionRepository(environment=environment(), region_name=region())
    run_id = str((event.get("queryStringParameters") or {}).get("run_id") or "")

    if run_id:
        records = repository.list_for_run(tenant_code, run_id)
        next_token = None
    else:
        page = repository.list_open(
            tenant_code,
            limit=MAX_EXCEPTIONS_LISTED,
            start_key=decode_page_token(event, tenant_code),
        )
        records = page.items
        next_token = encode_page_token(page.next_key)

    return json_response(
        200,
        {
            "tenant_code": tenant_code,
            "scope": "run" if run_id else "open",
            "exceptions": records,
            "count": len(records),
            "next_token": next_token,
        },
    )


def handle_onboard_serving_entity(event: dict[str, Any], path_tenant: str) -> dict[str, Any]:
    """
    POST /tenants/{t}/serving-store/entities — onboard an entity into the serving store.

    DL-SERV-03: `scripts/seed_serving_store_config.py` covered the operator path; this is the API
    the enterprise-platform's console (EP-04) drives, so onboarding does not require shell access
    to this account.
    """
    tenant_code = authorize_path_tenant(event, path_tenant)
    body = parse_json_body(event)
    try:
        config = ServingStoreLoadConfig.model_validate({**body, "tenant_code": tenant_code})
    except PydanticValidationError as exc:
        raise ValidationFailedError(
            f"Serving-store config failed validation: {exc.error_count()} error(s)."
        ) from exc
    ServingStoreConfigRepositoryClient(environment=environment(), region_name=region()).save_config(
        config, overwrite=bool(body.get("overwrite", False))
    )
    _logger.info(
        "serving_store_entity_onboarded",
        tenant_code=tenant_code,
        entity_type=config.entity_type,
        target_engine=config.target_engine.value,
        enabled=config.enabled,
    )
    return json_response(
        201,
        {
            "tenant_code": tenant_code,
            "entity_type": config.entity_type,
            "target_engine": config.target_engine.value,
            "enabled": config.enabled,
        },
    )
