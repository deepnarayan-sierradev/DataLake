"""
SeniorPlace connector (DL-CONN-10) — Assisted Living Locators, currently OData to ALL IN.

Where only OData is available, the shared OData engine already proven in
`adapters/sage/products/x3/x3_query_engine.py` is reused rather than a second OData
implementation — the reuse clause in DL-01 names this source specifically.

**PHI-bearing.** Senior placement records are PHI, so this source is gated by `DL-PORT-08`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from connector_runtime.adapters.rest_api.rest_adapter_registration import register_rest_source
from connector_runtime.adapters.rest_api.rest_source_spec import (
    AuthKind,
    RestEntitySpec,
    RestSourceSpec,
)
from connector_runtime.source_capabilities import SourceCapability

SOURCE_ID: Final[str] = "seniorplace"


def _entity(suffix: str, odata_set: str, watermark: str | None = "ModifiedOn") -> RestEntitySpec:
    return RestEntitySpec(
        entity_id=f"{SOURCE_ID}-{suffix}",
        path=f"/odata/{odata_set}",
        # OData wraps the collection in `value`.
        records_json_path=("value",),
        watermark_field=watermark,
        natural_key_field="Id",
        pagination_strategy="offset_limit",
        page_size=100,
    )


SENIORPLACE_SPEC: Final[RestSourceSpec] = RestSourceSpec(
    source_id=SOURCE_ID,
    display_name="SeniorPlace",
    base_url="https://api.seniorplace.com",
    auth_kind=AuthKind.BEARER_TOKEN,
    entities=(
        _entity("client", "Clients"),
        _entity("referral", "Referrals"),
        _entity("community", "Communities"),
        _entity("placement", "Placements"),
        _entity("assessment", "Assessments"),
        _entity("contact", "Contacts"),
        _entity("invoice", "Invoices"),
        _entity("user", "Users", watermark=None),
    ),
    capabilities=frozenset(
        {
            SourceCapability.INCREMENTAL,
            SourceCapability.SCHEMA_DISCOVERY,
            SourceCapability.RECORD_COUNT,
        }
    ),
    default_pagination_strategy="offset_limit",
    default_rate_limit_policy="seniorplace-standard",
    required_credential_keys=frozenset({"access_token"}),
    watermark_lower_parameter="$filter",
    watermark_upper_parameter="$filter_upper",
    notes=(
        "Assisted Living Locators, OData to ALL IN. PHI-bearing — blocked by the DL-PORT-08 "
        "onboarding gate until a BAA is recorded."
    ),
)

register_rest_source(SENIORPLACE_SPEC)

IS_PHI_BEARING: Final[bool] = True


def build_odata_incremental_filter(
    watermark_field: str, lower: str | None, upper: str | None
) -> str:
    """
    Build the `$filter` clause for an incremental OData read.

    OData has no separate parameter-binding channel, so the values are validated as
    ISO-8601 timestamps before they reach the clause — the same discipline the X3 engine
    applies (OWASP A03).
    """
    clauses: list[str] = []
    if lower:
        clauses.append(f"{watermark_field} ge {_odata_datetime(lower)}")
    if upper:
        clauses.append(f"{watermark_field} lt {_odata_datetime(upper)}")
    return " and ".join(clauses)


def _odata_datetime(value: str) -> str:
    from datetime import datetime

    # Raises on anything that is not a timestamp, so no arbitrary text reaches the clause.
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.isoformat()


def odata_query_parameters(
    watermark_field: str, lower: str | None, upper: str | None
) -> Mapping[str, Any]:
    """OData query parameters for one incremental page request."""
    parameters: dict[str, Any] = {}
    filter_clause = build_odata_incremental_filter(watermark_field, lower, upper)
    if filter_clause:
        parameters["$filter"] = filter_clause
    parameters["$orderby"] = f"{watermark_field} asc"
    return parameters
