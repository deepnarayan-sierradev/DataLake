"""
Maid Central connector (DL-CONN-03) — Maid Brigade operations.

The source list records the API as available but buggy with a cooperative vendor, so the
rate-limit policy is `Retry-After`-driven rather than a fixed schedule: an intermittently
failing endpoint should back off adaptively instead of hammering a known-flaky service.
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

SOURCE_ID: Final[str] = "maid-central"


def _entity(suffix: str, path: str, watermark: str | None = "modifiedDate") -> RestEntitySpec:
    return RestEntitySpec(
        entity_id=f"{SOURCE_ID}-{suffix}",
        path=path,
        records_json_path=("data",),
        watermark_field=watermark,
        natural_key_field="id",
        pagination_strategy="offset_limit",
        page_size=200,
    )


MAID_CENTRAL_SPEC: Final[RestSourceSpec] = RestSourceSpec(
    source_id=SOURCE_ID,
    display_name="Maid Central",
    base_url="https://api.maidcentral.com",
    auth_kind=AuthKind.API_KEY_HEADER,
    api_key_header_name="X-Api-Key",
    entities=(
        _entity("customer", "/v1/customers"),
        _entity("job", "/v1/jobs"),
        _entity("appointment", "/v1/appointments"),
        _entity("invoice", "/v1/invoices"),
        _entity("employee", "/v1/employees"),
        _entity("service", "/v1/services", watermark=None),
        _entity("location", "/v1/locations", watermark=None),
    ),
    capabilities=frozenset(
        {
            SourceCapability.INCREMENTAL,
            SourceCapability.SCHEMA_DISCOVERY,
        }
    ),
    default_pagination_strategy="offset_limit",
    default_rate_limit_policy="maid-central-standard",
    required_credential_keys=frozenset({"api_key"}),
    watermark_lower_parameter="modifiedSince",
    watermark_upper_parameter="modifiedBefore",
    notes="Maid Brigade operations. Vendor API has known defects; back off adaptively.",
)

register_rest_source(MAID_CENTRAL_SPEC)
