"""
Maid Central connector (DL-CONN-03) — Maid Brigade operations.

Rewritten on 2026-07-29 against the vendor's *MaidCentral Reporting API Guide*. The previous
spec predated the guide and was wrong in every dimension that matters: it assumed an
`X-Api-Key` header (the API is OAuth 2.0), `/v1/{entity}` paths (they are
`/api/v1/reporting/{entity}`), a `data` envelope (it is `Result.Items`), `offset`/`limit`
paging (it is `skipCount`/`maxResultCount`), and seven entities that mostly do not exist. It
would have failed on its first request.

The corrected shape:

**OAuth 2.0 with a one-hour token.** `POST /token`, form-encoded, password grant for the
first exchange and refresh-token grant thereafter. An hour is shorter than a full sweep of
thirteen entities, so the token exchange re-issues mid-run rather than letting the second
half of the extraction 401.

**`skipCount` / `maxResultCount`, max 1000.** Page size is the documented maximum: the
hourly budget is the binding constraint, so fewer, larger pages is the cheaper shape. The
envelope is `{IsSuccess, Message, Result: {Items, TotalCount}, StatusCode}`.

**1000 requests/hour with a 100/minute burst.** That is 0.28 requests/second sustained — by
a wide margin the tightest budget of any source on the platform. At 1000 rows per page the
whole thirteen-entity model is a few hundred requests, which fits; at the previous spec's
200 it would not have. The policy is a token bucket sized to the hourly rate, so a burst
drains and then throttles to the sustained rate instead of exhausting the hour in the first
minute and stalling every remaining entity.

`IsSuccess` is a body-level success flag that can be `false` under HTTP 200. It is
deliberately not read here: the raw layer stores what the source returned, and a `false`
yields zero records, which the reconciliation stage already surfaces as a count mismatch.
Reading it in the connector would put the same check in a second place.
"""

from __future__ import annotations

from typing import Final

from connector_runtime.adapters.rest_api.rest_adapter_registration import register_rest_source
from connector_runtime.adapters.rest_api.rest_source_spec import (
    AuthKind,
    PaginationParameters,
    RestEntitySpec,
    RestSourceSpec,
    TokenGrantKind,
)
from connector_runtime.source_capabilities import SourceCapability

SOURCE_ID: Final[str] = "maid-central"

# Documented: default 50, maximum 1000.
MAX_RESULT_COUNT: Final[int] = 1_000

# `skipCount` skips rows and `maxResultCount` caps them — offset/limit under other names.
_PAGINATION: Final[PaginationParameters] = PaginationParameters(
    offset="skipCount", limit="maxResultCount"
)

# Present on every reporting DTO; the incremental key.
WATERMARK_FIELD: Final[str] = "DateLastModified"


def _entity(
    suffix: str, path: str, natural_key: str, *, watermarked: bool = True
) -> RestEntitySpec:
    return RestEntitySpec(
        entity_id=f"{SOURCE_ID}-{suffix}",
        path=f"/api/v1/reporting/{path}",
        # `{"IsSuccess": true, "Result": {"Items": [...], "TotalCount": n}}`
        records_json_path=("Result", "Items"),
        watermark_field=WATERMARK_FIELD if watermarked else None,
        natural_key_field=natural_key,
        pagination_strategy="offset_limit",
        page_size=MAX_RESULT_COUNT,
        pagination_parameters=_PAGINATION,
        # Offset paging over a table that is being written to skips and repeats rows
        # unless the server orders deterministically. The guide documents `sorting`, so
        # every page is ordered by the entity's own key — the one column guaranteed
        # unique and stable for the life of the sweep.
        static_query_parameters={"sorting": f"{natural_key} ASC"},
    )


# The thirteen documented entities, with the identifier each DTO actually uses — not one of
# them is called `id`, so assuming one would have made every natural key null.
MAID_CENTRAL_SPEC: Final[RestSourceSpec] = RestSourceSpec(
    source_id=SOURCE_ID,
    display_name="Maid Central",
    base_url="https://api.maidcentral.com",
    auth_kind=AuthKind.OAUTH2_REFRESH,
    token_endpoint_path="/token",  # noqa: S106  # nosec B106 — a path, not a secret
    token_grant_kind=TokenGrantKind.PASSWORD,
    entities=(
        _entity("company", "companies", "ServiceCompanyId"),
        _entity("customer", "customers", "CustomerInformationId"),
        _entity("home", "homes", "HomeInformationId"),
        _entity("service-set", "servicesets", "ServiceSetInformationId"),
        _entity("job", "jobs", "JobInformationId"),
        _entity("employee", "employees", "EmployeeInformationId"),
        _entity("time-clock", "timeclocks", "EmployeeTimeClockId"),
        _entity("time-sheet", "timesheets", "TimeSheetsId"),
        _entity("payroll-summary", "payroll/summary", "EmployeeInformationId"),
        _entity("payroll-detail", "payroll/detail", "EmployeeInformationId"),
        _entity("lead", "leads", "CustomerInformationId"),
        _entity("quote", "quotes", "CustomerQuoteId"),
        # Zones are a small zip-code reference table with no modification stamp.
        _entity("zone", "zones", "ZonesId", watermarked=False),
    ),
    capabilities=frozenset(
        {
            SourceCapability.INCREMENTAL,
            SourceCapability.SCHEMA_DISCOVERY,
            SourceCapability.RECORD_COUNT,
        }
    ),
    default_pagination_strategy="offset_limit",
    default_rate_limit_policy="maid-central-hourly",
    pagination_parameters=_PAGINATION,
    # 1000-row reporting pages over payroll and job history; the guide advises processing
    # large datasets in chunks, which implies these are not fast responses.
    request_timeout_seconds=120.0,
    default_records_json_path=("Result", "Items"),
    default_page_size=MAX_RESULT_COUNT,
    # Password grant on first exchange; the response's refresh_token is written back into
    # the secret by the rotation runbook, after which the refresh grant is used.
    required_credential_keys=frozenset({"username", "password"}),
    watermark_lower_parameter="modifiedOnOrAfter",
    watermark_upper_parameter="modifiedBefore",
    notes=(
        "Maid Brigade operations, from the MaidCentral Reporting API Guide. 1000 req/hour "
        "with a 100/min burst — the tightest budget on the platform. Thirteen entities, "
        "OAuth 2.0 with a 1-hour token, skipCount/maxResultCount paging capped at 1000."
    ),
)

register_rest_source(MAID_CENTRAL_SPEC)
