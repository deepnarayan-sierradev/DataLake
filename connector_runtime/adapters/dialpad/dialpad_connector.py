"""
DialPad connector (DL-CONN-08) — Brothers Gutters call records.

The source list notes a vendor switch in progress and that the source is not yet
established, so the connection lifecycle (`pending` → `active`) carries the onboarding
state rather than the adapter guessing whether the source exists yet.
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

SOURCE_ID: Final[str] = "dialpad"


def _entity(suffix: str, path: str, watermark: str | None = "date_modified") -> RestEntitySpec:
    return RestEntitySpec(
        entity_id=f"{SOURCE_ID}-{suffix}",
        path=path,
        records_json_path=("items",),
        watermark_field=watermark,
        natural_key_field="id",
        pagination_strategy="cursor",
        page_size=100,
    )


DIALPAD_SPEC: Final[RestSourceSpec] = RestSourceSpec(
    source_id=SOURCE_ID,
    display_name="DialPad",
    base_url="https://dialpad.com",
    auth_kind=AuthKind.BEARER_TOKEN,
    entities=(
        _entity("call", "/api/v2/call"),
        _entity("call-log", "/api/v2/calls"),
        _entity("user", "/api/v2/users"),
        _entity("call-centre", "/api/v2/callcenters"),
        _entity("contact", "/api/v2/contacts"),
        _entity("department", "/api/v2/departments", watermark=None),
    ),
    capabilities=frozenset(
        {
            SourceCapability.INCREMENTAL,
            SourceCapability.WEBHOOKS,
            SourceCapability.SCHEMA_DISCOVERY,
        }
    ),
    default_pagination_strategy="cursor",
    default_rate_limit_policy="dialpad-standard",
    default_sync_strategy="webhook_ingest",
    required_credential_keys=frozenset({"access_token"}),
    watermark_lower_parameter="started_after",
    watermark_upper_parameter="started_before",
    webhook_signature_algorithm="hmac_sha256_hex",
    notes="Brothers Gutters. Vendor switch in progress; source not yet established.",
)

register_rest_source(DIALPAD_SPEC)
