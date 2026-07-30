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

## Second pass, 2026-07-30 — seven of the first sixteen entities were wrong

The first rewrite got the *shape* right and the *inventory* wrong: it assumed every resource
in the "Supported APIs" table had a `_search` sibling. Checking each declared path against
the operations the specification actually publishes found:

- `adminTasks`, `activities`, `documentReferences` — **no `_search` path exists.** They
  publish create plus fetch-by-id only. `documentReferences` has `_profile`, which requires
  a `reference` in its body and so is profile-scoped, not a bulk list.
- `referralsource`, `profileTags` — the collection endpoint is **POST (create) only**; there
  is no GET list. Reading them would have been a 405, not a list.
- `locations/_search` — published with **no trailing slash**, unlike every other `_search`.
- `organizations` — its endpoints declare **no `_page` / `_count`**. Driving it with
  `page_number` would re-request page 0 until the 10,000-page ceiling, duplicating rows.

Watermarks were also over-claimed: only `patients`, `practitioners` and `relatedperson`
document `created` / `updated` as searchable. The other eight were sending a filter their
endpoint does not define — which loads everything while still advancing the watermark.

`allergyintolerance/all-allergy` was missed entirely and is now included.

Not extractable in bulk, and therefore deliberately absent: `condition`, `goal`,
`medicationstatement`, `careplan`, `documentReferences`, `invoice`, `tasklog` — each is
scoped to a patient, encounter or id the schedule cannot supply. Several also carry a path
parameter, and the substrate does no path templating, so declaring them would issue a
literal `{patient_id}`. They need a parent-scoped fan-out first.
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

MAX_COUNT: Final[int] = 100

_PAGINATION: Final[PaginationParameters] = PaginationParameters(
    page="_page", limit="_count", first_page_index=0
)

_GE_PREFIX: Final[str] = "ge"


def _searchable(path: str, suffix: str, *, watermarked: bool = False) -> RestEntitySpec:
    """A `POST .../_search/` read returning a paginated FHIR Bundle."""
    return RestEntitySpec(
        entity_id=f"{SOURCE_ID}-{suffix}",
        path=path,
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


def _unpaginated(path: str, suffix: str, *, read_method: str = "GET") -> RestEntitySpec:
    """
    A bulk read the specification declares without `_page` / `_count`.

    Driving one of these with `page_number` is the trap this exists to avoid: the endpoint
    ignores `_page`, so every request returns the same full first page, the strategy never
    sees a short page, and the run duplicates that page up to the 10,000-page ceiling.
    """
    return RestEntitySpec(
        entity_id=f"{SOURCE_ID}-{suffix}",
        path=path,
        read_method=read_method,
        records_json_path=("entry",),
        record_unwrap_field="resource",
        watermark_field=None,
        natural_key_field="id",
        pagination_strategy="single_request",
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
        _searchable("/v1/patients/_search/", "patient", watermarked=True),
        _searchable("/v1/practitioners/_search/", "practitioner", watermarked=True),
        _searchable("/v1/relatedperson/_search/", "related-person", watermarked=True),
        _searchable("/v1/encounter/_search/", "encounter"),
        _searchable("/v1/appointment/_search/", "appointment"),
        _searchable("/v1/chargeitem/_search/", "charge-item"),
        _searchable("/v1/medication/_search/", "medication"),
        _searchable("/v1/subscriptions/_search/", "subscription"),
        _searchable("/v1/admins/_search/", "agency-admin"),
        _searchable("/v1/locations/_search", "location"),
        _unpaginated("/v1/organizations/", "organization"),
        _unpaginated("/v1/allergyintolerance/all-allergy/", "allergy-intolerance"),
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
    request_timeout_seconds=90.0,
    default_records_json_path=("entry",),
    default_page_size=MAX_COUNT,
    required_credential_keys=frozenset({"client_id", "client_secret"}),
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

IS_PHI_BEARING: Final[bool] = True
