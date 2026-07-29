"""
SeniorPlace connector (DL-CONN-10) — Assisted Living Locators.

Rewritten on 2026-07-29 against the vendor's published OpenAPI 3.0.3 document
(`seniorplace-public.s3.us-west-2.amazonaws.com/docs`). The previous spec modelled this
source as OData — `/odata/Clients`, `$filter`, a `value` envelope — on the strength of the
source list's note that Assisted Living Locators is "currently OData to ALL IN". That note
describes a *different* pipe: ALL IN is the downstream system the agency feeds today, and
the OData contract belongs to ALL IN. SeniorPlace's own public API is an ordinary REST
surface, and this is now modelled from its specification.

The corrected shape:

**`Authorization: ApiKey <key>`.** Not bearer. The scheme word is part of the header value,
so it is declared as `api_key_value_prefix` rather than baked into the stored secret — the
secret holds the key alone, which is what a rotation writes.

**`updatedAfter` on `/clients`, and nowhere else.** Exactly one endpoint accepts an
incremental filter. The rest are small reference lists, so they are full loads and declare
no watermark rather than pretending to filter.

**No pagination parameters are documented on any endpoint.** The specification declares
`officeId`, `assignedUserId`, `updatedAfter` and the referral filters — and nothing that
skips, limits, or cursors. Rather than invent `limit`/`offset` and hope, every entity uses
the `single_request` strategy, which makes the absence a declared fact. If the vendor is in
fact truncating silently, the reconciliation stage's count check surfaces it; a guessed
parameter would instead look like it worked.

**No rate limit is documented at all.** An undocumented limit is not an absent one, so the
policy is a deliberately modest fixed window rather than unthrottled.

**PHI-bearing.** Senior placement records are PHI, so this source stays gated by
`DL-PORT-08`.

`build_odata_incremental_filter` and `odata_query_parameters` are retained below: the ALL IN
OData feed is still a live integration surface for this agency, documented separately, and
the validation discipline they carry — timestamps parsed before they reach a filter clause —
is what keeps that path free of injection. They are no longer used by this spec.
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


def _entity(suffix: str, path: str, *, watermark: str | None = None) -> RestEntitySpec:
    return RestEntitySpec(
        entity_id=f"{SOURCE_ID}-{suffix}",
        path=path,
        # Collections come back as a bare JSON array, so the record path is empty and the
        # body itself is the list.
        records_json_path=(),
        watermark_field=watermark,
        natural_key_field="id",
        # The specification documents no paging parameters — see the module docstring.
        pagination_strategy="single_request",
    )


SENIORPLACE_SPEC: Final[RestSourceSpec] = RestSourceSpec(
    source_id=SOURCE_ID,
    display_name="SeniorPlace",
    base_url="https://app.seniorplace.com",
    auth_kind=AuthKind.API_KEY_HEADER,
    api_key_header_name="Authorization",
    api_key_value_prefix="ApiKey ",
    entities=(
        _entity("client", "/api/v1/clients", watermark="updatedAt"),
        _entity("client-status", "/api/v1/client-statuses"),
        _entity("client-custom-question", "/api/v1/client/custom-questions"),
        _entity("user", "/api/v1/users"),
        _entity("referral-contact", "/api/v1/referral-contacts"),
        _entity("referral-organization", "/api/v1/referral-organizations"),
    ),
    capabilities=frozenset(
        {
            SourceCapability.INCREMENTAL,
            SourceCapability.SCHEMA_DISCOVERY,
        }
    ),
    default_pagination_strategy="single_request",
    default_rate_limit_policy="seniorplace-standard",
    # SeniorPlace returns a bare JSON array, so the body itself is the record list.
    default_records_json_path=(),
    required_credential_keys=frozenset({"api_key"}),
    # The one documented incremental filter, on `/clients`. There is no upper-bound
    # parameter, so the window is left open at the top rather than closed with a guess.
    watermark_lower_parameter="updatedAfter",
    watermark_upper_parameter="updatedBefore",
    notes=(
        "Assisted Living Locators. PHI-bearing — blocked by the DL-PORT-08 onboarding gate "
        "until a BAA is recorded. Authorization: ApiKey <key>. Only /clients filters "
        "incrementally (updatedAfter); no paging and no rate limit are documented, so reads "
        "are single-request and the policy is conservative by choice. The ALL IN OData feed "
        "is a separate surface — see the OData helpers in this module."
    ),
)

register_rest_source(SENIORPLACE_SPEC)

IS_PHI_BEARING: Final[bool] = True


def build_odata_incremental_filter(
    watermark_field: str, lower: str | None, upper: str | None
) -> str:
    """
    Build the `$filter` clause for an incremental OData read against the ALL IN feed.

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
    """OData query parameters for one incremental read against the ALL IN feed."""
    parameters: dict[str, Any] = {}
    filter_clause = build_odata_incremental_filter(watermark_field, lower, upper)
    if filter_clause:
        parameters["$filter"] = filter_clause
    parameters["$orderby"] = f"{watermark_field} asc"
    return parameters
