"""
HouseCall Pro connector (DL-CONN-09) — Shine operations, via the BI Pro API.

Webhook-capable, so its default sync strategy is `webhook_ingest` with the polling
back-fill the strategy provides when the stream falls behind.
"""

from __future__ import annotations

from typing import Final

from connector_runtime.adapters.rest_api.rest_adapter_registration import register_rest_source
from connector_runtime.adapters.rest_api.rest_source_spec import (
    AuthKind,
    RestEntitySpec,
    RestSourceSpec,
)
from connector_runtime.source_capabilities import SourceCapability

SOURCE_ID: Final[str] = "housecall-pro"


def _entity(suffix: str, path: str, watermark: str | None = "updated_at") -> RestEntitySpec:
    return RestEntitySpec(
        entity_id=f"{SOURCE_ID}-{suffix}",
        path=path,
        records_json_path=("data",),
        watermark_field=watermark,
        natural_key_field="id",
        pagination_strategy="link_header",
        page_size=100,
    )


HOUSECALL_PRO_SPEC: Final[RestSourceSpec] = RestSourceSpec(
    source_id=SOURCE_ID,
    display_name="HouseCall Pro",
    base_url="https://api.housecallpro.com",
    auth_kind=AuthKind.API_KEY_HEADER,
    api_key_header_name="Authorization",
    entities=(
        _entity("customer", "/customers"),
        _entity("job", "/jobs"),
        _entity("estimate", "/estimates"),
        _entity("invoice", "/invoices"),
        _entity("employee", "/employees"),
        _entity("job-line-item", "/jobs/line_items"),
        _entity("schedule", "/schedules"),
        _entity("price-book-item", "/price_book/items", watermark=None),
    ),
    capabilities=frozenset(
        {
            SourceCapability.INCREMENTAL,
            SourceCapability.WEBHOOKS,
            SourceCapability.SCHEMA_DISCOVERY,
        }
    ),
    default_pagination_strategy="link_header",
    default_rate_limit_policy="housecall-pro-standard",
    default_sync_strategy="webhook_ingest",
    required_credential_keys=frozenset({"api_key"}),
    watermark_lower_parameter="updated_after",
    watermark_upper_parameter="updated_before",
    webhook_signature_algorithm="hmac_sha256_hex",
    notes="Shine operations via the BI Pro API connection.",
)

register_rest_source(HOUSECALL_PRO_SPEC)
