"""
BePro Data API connector (DL-CONN-19) — sports performance events, stats and tracking.

Built from the vendor's published OpenAPI 3.1 document, so the entity table below is the
API's actual surface rather than an inference: three families (`meta` reference data,
`data` observations, `external` id mappings) over one `offset`/`limit` envelope of the shape
`{count, next, prev, data: [...]}`.

Three properties drive the design:

**There is no modification timestamp anywhere in the API.** Not one endpoint accepts an
`updated_since` filter and no response schema carries a modified field. Incremental
extraction is therefore not available, and the connector declares that rather than
pretending: every entity is a full load, and the source's capability set omits
`INCREMENTAL`. Claiming it and quietly reloading everything is the failure mode this avoids
— the watermark repository would advance against data it never actually filtered on.

**The quota is two-tier: 1000 requests/minute sustained with a 100 requests/second burst.**
A single fixed window cannot express that; a token bucket can, and does — capacity is the
burst, refill is the sustained rate. Both are set below the documented figures because the
budget is per API token and a token is shared by whatever else the customer runs against it.

**Two endpoints are match-scoped and cannot be scheduled standalone**, for two different
reasons worth keeping distinct. `video/timings` declares `match_id` **required** — the
vendor's own constraint. `data/tracking` does not: `match_id` is optional there, but the
endpoint is unpaginated and returns per-frame positional data, so an unscoped call asks for
every frame of every match in one response. That second constraint is **ours**, chosen
because the alternative is a request that cannot complete.

Both are declared with `required_run_parameters`, so calling them without a scope fails
closed as a *configuration* error rather than reaching the provider and returning 422, which
the retry policy would otherwise treat as worth retrying. They become schedulable when a
match-scoped fan-out exists; the declaration is what makes that gap visible in the console
instead of invisible.
"""

from __future__ import annotations

from typing import Final

from connector_runtime.adapters.rest_api.rest_adapter_registration import register_rest_source
from connector_runtime.adapters.rest_api.rest_source_spec import (
    AuthKind,
    RestEntitySpec,
    RestSourceSpec,
)
from connector_runtime.rate_limiting import (
    DocumentedRateLimit,
    rate_limit_policy_registry,
    token_bucket_within,
)
from connector_runtime.source_capabilities import SourceCapability

SOURCE_ID: Final[str] = "bepro"

DOCUMENTED_REQUESTS_PER_MINUTE: Final[int] = 1_000
DOCUMENTED_BURST_PER_SECOND: Final[int] = 100
DOCUMENTED_LIMITS: Final[tuple[DocumentedRateLimit, ...]] = (
    DocumentedRateLimit(DOCUMENTED_BURST_PER_SECOND, 1),
    DocumentedRateLimit(DOCUMENTED_REQUESTS_PER_MINUTE, 60),
)

RATE_LIMIT_POLICY_NAME: Final[str] = "bepro-standard"
rate_limit_policy_registry.register(RATE_LIMIT_POLICY_NAME, token_bucket_within(DOCUMENTED_LIMITS))

_PAGE_SIZE: Final[int] = 200

SCHEMA_SPORT_TYPE: Final[str] = "football"


def _collection(suffix: str, path: str, natural_key: str = "id") -> RestEntitySpec:
    return RestEntitySpec(
        entity_id=f"{SOURCE_ID}-{suffix}",
        path=path,
        records_json_path=("data",),
        watermark_field=None,
        natural_key_field=natural_key,
        pagination_strategy="offset_limit",
        page_size=_PAGE_SIZE,
        static_query_parameters={"sort_direction": "asc"},
    )


def _match_scoped(suffix: str, path: str) -> RestEntitySpec:
    """A per-match detail endpoint: neither of them declares offset/limit."""
    return RestEntitySpec(
        entity_id=f"{SOURCE_ID}-{suffix}",
        path=path,
        records_json_path=("data",),
        watermark_field=None,
        natural_key_field="id",
        pagination_strategy="single_request",
        page_size=_PAGE_SIZE,
        required_run_parameters=("match_id",),
    )


_META_ENTITIES: Final[tuple[RestEntitySpec, ...]] = (
    _collection("club", "/data-api/meta/clubs"),
    _collection("league", "/data-api/meta/leagues"),
    _collection("season", "/data-api/meta/seasons"),
    _collection("team", "/data-api/meta/teams"),
    _collection("player", "/data-api/meta/players"),
    _collection("match", "/data-api/meta/matches"),
    _collection("lineup", "/data-api/meta/lineups"),
)

_DATA_ENTITIES: Final[tuple[RestEntitySpec, ...]] = (
    _collection("event", "/data-api/data/events"),
    _collection("sequence", "/data-api/data/sequences"),
    _collection("player-stat", "/data-api/data/stats/players"),
    _collection("team-stat", "/data-api/data/stats/teams"),
)

_EXTERNAL_ENTITIES: Final[tuple[RestEntitySpec, ...]] = (
    _collection("external-club", "/data-api/external/clubs"),
    _collection("external-league", "/data-api/external/leagues"),
    _collection("external-season", "/data-api/external/seasons"),
    _collection("external-team", "/data-api/external/teams"),
    _collection("external-player", "/data-api/external/players"),
    _collection("external-match", "/data-api/external/matches"),
)

_SCOPED_ENTITIES: Final[tuple[RestEntitySpec, ...]] = (
    _match_scoped("tracking", "/data-api/data/tracking"),
    _match_scoped("video-timing", "/data-api/video/timings"),
)

_EVENT_SCHEMA_ENTITY: Final[RestEntitySpec] = RestEntitySpec(
    entity_id=f"{SOURCE_ID}-event-schema",
    path="/data-api/data/schemas",
    records_json_path=("data",),
    watermark_field=None,
    natural_key_field="id",
    pagination_strategy="single_request",
    page_size=_PAGE_SIZE,
    static_query_parameters={"sport_type": SCHEMA_SPORT_TYPE},
)

MATCH_SCOPED_ENTITY_IDS: Final[frozenset[str]] = frozenset(e.entity_id for e in _SCOPED_ENTITIES)

BEPRO_SPEC: Final[RestSourceSpec] = RestSourceSpec(
    source_id=SOURCE_ID,
    display_name="BePro Data API",
    base_url="https://ds.bepro.ai",
    auth_kind=AuthKind.BEARER_TOKEN,
    entities=(
        *_META_ENTITIES,
        *_DATA_ENTITIES,
        *_EXTERNAL_ENTITIES,
        _EVENT_SCHEMA_ENTITY,
        *_SCOPED_ENTITIES,
    ),
    capabilities=frozenset(
        {
            SourceCapability.SCHEMA_DISCOVERY,
            SourceCapability.RECORD_COUNT,
        }
    ),
    default_pagination_strategy="offset_limit",
    default_rate_limit_policy=RATE_LIMIT_POLICY_NAME,
    request_timeout_seconds=180.0,
    default_records_json_path=("data",),
    default_page_size=_PAGE_SIZE,
    default_sync_strategy="watermark_polling",
    required_credential_keys=frozenset({"access_token"}),
    notes=(
        "Sports performance data. Full load only — the API exposes no modification "
        "timestamp on any endpoint. 1000 req/min sustained with a 100 req/s burst, per API "
        "token. Two match-scoped entities are declared but need a fan-out before they can "
        "be scheduled."
    ),
)

register_rest_source(BEPRO_SPEC)
