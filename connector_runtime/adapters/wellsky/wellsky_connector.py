"""
WellSky Personal Care connector (DL-CONN-05) — Executive Home Care operations.

Rewritten on 2026-07-29 against the vendor's published Home Connect API specification
(`apidocs.clearcareonline.com`, Swagger 2.0). The previous spec declared fifty entities at
`/api/v2/{domain}/{sub}` with keyset paging and a `data.records` envelope. None of that
exists. The real API is a FHIR-flavoured surface at `connect.clearcareonline.com/v1/`, and
the "~50 tables" figure on the source list refers to a *different* product — WellSky
Insights, a warehouse with `CARE`, `Agencies` and `meta` schemas — which is a JDBC source,
not this API. Conflating the two is what produced the fictional entity list.

Four properties of the real API shape this connector, and each needed substrate support
that did not previously exist:

**Reads are `POST /{resource}/_search/`, not `GET`.** Filters travel in a JSON body; the
`GET` collection endpoint is the create surface's sibling, not a query. `read_method` and
`search_body` on the entity spec exist for this.

**Incremental filtering is a comparator-prefixed body field.** `{"updated": "ge2026-07-01"}`
— FHIR prefix notation, with no upper-bound form. The connector therefore binds only the
lower bound and leaves the window open at the top, which is correct: sending an upper bound
the API does not understand would silently return an unfiltered result set.

**Pagination is `_page` / `_count`, page-indexed from 0, capped at 100.** Not offsets. The
`page_number` strategy exists because reusing offset paging here would advance the page
index by the row count and skip 99 pages out of every 100.

**Rows are nested one level down.** The response is a FHIR Bundle: `{resourceType: "Bundle",
totalRecords: n, entry: [{resource: {...}}]}`. `record_unwrap_field` unwraps `resource` so
the raw layer stores the record and not the envelope.

Rate limiting: the vendor states it does not explicitly throttle, but asks for no more than
100 requests/second and explicitly advises against batch use. Both halves are honoured — the
bucket sits an order of magnitude below the stated ceiling, because "we do not throttle"
plus "do not use this for batch" is a request for restraint, not a licence to saturate.

**PHI-bearing.** Home care records are PHI, so this source stays gated by `DL-PORT-08` and
must not be onboarded before a BAA is recorded.
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

SOURCE_ID: Final[str] = "wellsky"

# Documented: `_count` default 20, minimum 1, maximum 100.
MAX_COUNT: Final[int] = 100

# `_page` is zero-based and counts pages, not rows.
_PAGINATION: Final[PaginationParameters] = PaginationParameters(
    page="_page", limit="_count", first_page_index=0
)

# FHIR search prefix for "greater than or equal to" on a date field.
_GE_PREFIX: Final[str] = "ge"


def _searchable(suffix: str, resource: str, *, watermarked: bool = True) -> RestEntitySpec:
    """A resource read through `POST /v1/{resource}/_search/`, returning a FHIR Bundle."""
    return RestEntitySpec(
        entity_id=f"{SOURCE_ID}-{suffix}",
        # The trailing slash is mandatory: the vendor's implementation rules state that
        # omitting it returns an error rather than the collection.
        path=f"/v1/{resource}/_search/",
        read_method="POST",
        records_json_path=("entry",),
        record_unwrap_field="resource",
        watermark_field="updated" if watermarked else None,
        watermark_body_field="updated" if watermarked else None,
        watermark_comparator_prefix=_GE_PREFIX,
        natural_key_field="id",
        pagination_strategy="page_number",
        page_size=MAX_COUNT,
        pagination_parameters=_PAGINATION,
    )


def _listable(suffix: str, resource: str) -> RestEntitySpec:
    """A resource the API exposes only as a `GET` collection, with no search sibling."""
    return RestEntitySpec(
        entity_id=f"{SOURCE_ID}-{suffix}",
        path=f"/v1/{resource}/",
        records_json_path=("entry",),
        record_unwrap_field="resource",
        watermark_field=None,
        natural_key_field="id",
        pagination_strategy="page_number",
        page_size=MAX_COUNT,
        pagination_parameters=_PAGINATION,
    )


WELLSKY_SPEC: Final[RestSourceSpec] = RestSourceSpec(
    source_id=SOURCE_ID,
    display_name="WellSky Personal Care",
    base_url="https://connect.clearcareonline.com",
    auth_kind=AuthKind.OAUTH2_REFRESH,
    token_endpoint_path="/oauth/accesstoken",  # noqa: S106  # nosec B106 — a path, not a secret
    token_grant_kind=TokenGrantKind.CLIENT_CREDENTIALS,
    entities=(
        # Clinical and demographic records — the PHI core of the source.
        _searchable("patient", "patients"),
        _searchable("practitioner", "practitioners"),
        _searchable("related-person", "relatedperson"),
        _searchable("encounter", "encounter"),
        _searchable("appointment", "appointment"),
        _searchable("admin-task", "adminTasks"),
        _searchable("activity", "activities"),
        _searchable("charge-item", "chargeitem"),
        _searchable("document-reference", "documentReferences"),
        _searchable("medication", "medication"),
        _searchable("subscription", "subscriptions"),
        # Reference and organisational data: searchable, but with no useful change stamp.
        _searchable("organization", "organizations", watermarked=False),
        _searchable("location", "locations", watermarked=False),
        _searchable("agency-admin", "admins", watermarked=False),
        # Collection-only resources — no `_search` sibling is published for these.
        _listable("referral-source", "referralsource"),
        _listable("profile-tag", "profileTags"),
    ),
    capabilities=frozenset(
        {
            SourceCapability.INCREMENTAL,
            SourceCapability.SCHEMA_DISCOVERY,
            SourceCapability.RECORD_COUNT,
        }
    ),
    default_pagination_strategy="page_number",
    default_rate_limit_policy="wellsky-conservative",
    pagination_parameters=_PAGINATION,
    default_records_json_path=("entry",),
    default_page_size=MAX_COUNT,
    required_credential_keys=frozenset({"client_id", "client_secret"}),
    # Retained for the GET-collection entities; the searchable ones bind the lower bound
    # into the request body instead (see `watermark_body_field`).
    watermark_lower_parameter="updated",
    watermark_upper_parameter="updated_before",
    notes=(
        "Executive Home Care, WellSky Personal Care Home Connect API. PHI-bearing — blocked "
        "by the DL-PORT-08 onboarding gate until a BAA is recorded. Reads are POST _search "
        "with a FHIR Bundle response; paging is _page/_count capped at 100. The vendor asks "
        "for <=100 req/s and advises against batch use, so the policy stays well below it. "
        "The '~50 tables' on the source list is WellSky Insights, a separate warehouse "
        "product, not this API."
    ),
)

register_rest_source(WELLSKY_SPEC)

# Consumed by the PHI onboarding gate; declared here so the fact lives with the adapter.
IS_PHI_BEARING: Final[bool] = True
