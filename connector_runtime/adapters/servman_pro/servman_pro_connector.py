"""
ServMan Pro connector (DL-CONN-04) — Pacific Lawn & Sprinklers CRM and call centre.

The vendor API is still under construction, so entities the vendor has not shipped are
declared but marked unavailable: calling one raises `SourceCapabilityUnavailableError`
rather than a generic failure, which is what keeps a not-yet-built endpoint out of the
transient-retry path.
"""

from __future__ import annotations

from typing import Final

from connector_runtime.adapters.rest_api.rest_adapter_registration import register_rest_source
from connector_runtime.adapters.rest_api.rest_source_spec import (
    AuthKind,
    RestEntitySpec,
    RestSourceSpec,
)
from connector_runtime.source_capabilities import (
    SourceCapability,
    SourceCapabilityUnavailableError,
)

SOURCE_ID: Final[str] = "servman-pro"

# Entities the vendor has confirmed as not yet delivered. Declared rather than omitted so
# the console can show them as pending instead of the platform silently lacking them.
PENDING_VENDOR_ENTITIES: Final[frozenset[str]] = frozenset(
    {
        f"{SOURCE_ID}-call-recording",
        f"{SOURCE_ID}-technician-route",
    }
)


def _entity(suffix: str, path: str, watermark: str | None = "updated_at") -> RestEntitySpec:
    return RestEntitySpec(
        entity_id=f"{SOURCE_ID}-{suffix}",
        path=path,
        records_json_path=("items",),
        watermark_field=watermark,
        natural_key_field="id",
        pagination_strategy="offset_limit",
        page_size=100,
    )


SERVMAN_PRO_SPEC: Final[RestSourceSpec] = RestSourceSpec(
    source_id=SOURCE_ID,
    display_name="ServMan Pro",
    base_url="https://api.servmanpro.com",
    auth_kind=AuthKind.BEARER_TOKEN,
    entities=(
        _entity("customer", "/api/v1/customers"),
        _entity("work-order", "/api/v1/work-orders"),
        _entity("call", "/api/v1/calls"),
        _entity("invoice", "/api/v1/invoices"),
        _entity("service-agreement", "/api/v1/service-agreements"),
        _entity("call-recording", "/api/v1/call-recordings"),
        _entity("technician-route", "/api/v1/routes"),
    ),
    capabilities=frozenset({SourceCapability.INCREMENTAL}),
    default_pagination_strategy="offset_limit",
    default_rate_limit_policy="servman-pro-standard",
    # Inherited by a config-declared entity (DL-CONN-21); must match what this
    # source's own entities use, or a console-added entity silently reads zero rows.
    default_records_json_path=("items",),
    default_page_size=100,
    required_credential_keys=frozenset({"access_token"}),
    watermark_lower_parameter="updated_since",
    watermark_upper_parameter="updated_before",
    notes="Pacific Lawn & Sprinklers CRM + call centre. Vendor API partially delivered.",
)

register_rest_source(SERVMAN_PRO_SPEC)


def guard_vendor_availability(entity_id: str) -> None:
    """Raise a distinguishable error for an endpoint the vendor has not shipped."""
    if entity_id in PENDING_VENDOR_ENTITIES:
        raise SourceCapabilityUnavailableError(
            f"ServMan Pro entity {entity_id!r} is declared but the vendor endpoint is not yet "
            "available. This is a source-capability gap, not an extraction failure — do not "
            "retry it as transient."
        )
