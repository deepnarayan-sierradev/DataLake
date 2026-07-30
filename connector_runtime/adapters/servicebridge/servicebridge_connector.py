"""
ServiceBridge connector (DL-CONN-18) — Brothers Gutters' outgoing field-service system.

Row 10 of the source list records Brothers Gutters as *migrating from Service Bridge* to
HubSpot. That makes this a history source, not a steady-state one: the value is the
pre-migration operational record, and the connector must be able to sweep it once,
completely, and then keep pace with a shrinking tail until the migration finishes.

Two documented facts shape the design, and both are unusual enough to be worth stating:

**The quota is per IP address, not per token.** ServiceBridge documents "50 requests per
second" and "60000 requests per hour" *per IP*. Every Lambda in a VPC egresses through the
same NAT address, so every connection — every franchise location, every tenant — spends one
shared budget. The policy is therefore registered with `shared_across_connections=True`.
Binding it per connection, which is right for HubSpot's per-token quota, would let N
concurrent extractions each believe they own 50 rps and collectively issue 50N.

**Authentication puts a credential in the query string.** The API-user flow issues a
`sessionKey` passed as a GET parameter with a 30-minute *sliding* expiry. Two consequences
are handled rather than tolerated: the session is re-acquired through the shared token
exchange when it lapses (`AuthKind.SESSION_KEY_QUERY` + `TokenGrantKind.SESSION_LOGIN`), and
the HTTP session never logs a query string, so the key cannot reach CloudWatch (OWASP A09).
OAuth is the alternative the vendor offers; a connection may use it instead by storing an
`access_token`, but the session-key flow is the default because it needs no user consent
screen for a server-to-server sweep.

The endpoint catalogue below is the set the vendor's public documentation names directly
(the full method reference at `cloud.servicebridge.com/developer/index` is not publicly
reachable — it answers 403 to unauthenticated clients). Entities are declared from the
documented resources; adding one the vendor exposes later is a spec line, not code.
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
from connector_runtime.rate_limiting import (
    DocumentedRateLimit,
    rate_limit_policy_registry,
    token_bucket_within,
)
from connector_runtime.source_capabilities import SourceCapability

SOURCE_ID: Final[str] = "servicebridge"

# Documented: 50 requests/second and 60000 requests/hour, per IP address.
# 60000/hour is 16.67/second sustained, so the hourly ceiling binds long before the
# per-second one; the bucket is sized to the hourly rate with the per-second figure as
# burst headroom. Deliberately below both so a co-tenant sharing the NAT address does not
# push the pair over.
DOCUMENTED_REQUESTS_PER_SECOND: Final[int] = 50
DOCUMENTED_REQUESTS_PER_HOUR: Final[int] = 60_000
DOCUMENTED_LIMITS: Final[tuple[DocumentedRateLimit, ...]] = (
    DocumentedRateLimit(DOCUMENTED_REQUESTS_PER_SECOND, 1),
    DocumentedRateLimit(DOCUMENTED_REQUESTS_PER_HOUR, 3_600),
)

RATE_LIMIT_POLICY_NAME: Final[str] = "servicebridge-shared-ip"
rate_limit_policy_registry.register(
    RATE_LIMIT_POLICY_NAME,
    # Derived rather than hand-sized: a capacity chosen as a fraction of the per-second
    # figure still breached it once the refill over that same second was counted.
    # The quota is per IP and every Lambda shares one NAT address (see module docstring).
    token_bucket_within(DOCUMENTED_LIMITS, shared_across_connections=True),
)

# ServiceBridge names resources in the plural and versions them in the path. v2 is the
# current shape for customers, locations and contacts — v1 kept only where the vendor has
# not published a v2 equivalent, which is what the 2.0 upgrade note describes.
_PAGINATION: Final[PaginationParameters] = PaginationParameters(
    page="page", limit="pageSize", first_page_index=1
)


def _entity(
    suffix: str,
    path: str,
    *,
    watermark: str | None = "ModifiedOn",
    natural_key: str = "Id",
) -> RestEntitySpec:
    return RestEntitySpec(
        entity_id=f"{SOURCE_ID}-{suffix}",
        path=path,
        records_json_path=("Results",),
        watermark_field=watermark,
        natural_key_field=natural_key,
        pagination_strategy="page_number",
        # Kept well under the documented per-second ceiling: a large page is one request,
        # but a page this size is also what keeps a 60k/hour budget covering ~50 entities.
        page_size=200,
        pagination_parameters=_PAGINATION,
    )


SERVICEBRIDGE_SPEC: Final[RestSourceSpec] = RestSourceSpec(
    source_id=SOURCE_ID,
    display_name="ServiceBridge",
    base_url="https://cloud.servicebridge.com",
    auth_kind=AuthKind.SESSION_KEY_QUERY,
    session_key_parameter="sessionKey",
    token_endpoint_path="/api/v1/login",  # noqa: S106  # nosec B106 — a path, not a secret
    token_grant_kind=TokenGrantKind.SESSION_LOGIN,
    entities=(
        _entity("customer", "/api/v2/customers"),
        _entity("location", "/api/v2/locations"),
        _entity("contact", "/api/v2/contacts"),
        _entity("work-order", "/api/v2/workOrders"),
        _entity("estimate", "/api/v2/estimates"),
        _entity("invoice", "/api/v1/invoices"),
        _entity("appointment", "/api/v1/appointments"),
        _entity("employee", "/api/v1/employees"),
        _entity("service", "/api/v1/services", watermark=None),
        _entity("marketing-category", "/api/v1/marketingCategories", watermark=None),
    ),
    capabilities=frozenset(
        {
            SourceCapability.INCREMENTAL,
            SourceCapability.SCHEMA_DISCOVERY,
            SourceCapability.RECORD_COUNT,
        }
    ),
    default_pagination_strategy="page_number",
    default_rate_limit_policy=RATE_LIMIT_POLICY_NAME,
    pagination_parameters=_PAGINATION,
    # The session-login grant needs a user id and password; a connection using the OAuth
    # flow instead stores an access_token and overrides auth at the connection level.
    required_credential_keys=frozenset({"user_id", "password"}),
    # 200-row pages from a field-service system carrying work-order history.
    request_timeout_seconds=60.0,
    default_records_json_path=("Results",),
    default_page_size=200,
    watermark_lower_parameter="modifiedOnOrAfter",
    watermark_upper_parameter="modifiedOnOrBefore",
    notes=(
        "Brothers Gutters, migrating away to HubSpot — extract for history and keep pace "
        "with the tail. Quota is 50 req/s and 60000 req/hour PER IP, so the policy is "
        "shared across every connection behind the same NAT address. Session key is a "
        "query-string credential and is never logged."
    ),
)

register_rest_source(SERVICEBRIDGE_SPEC)

# Consumed by the migration runbook: this source is expected to go read-only and then dark
# once Brothers Gutters completes the HubSpot cutover. Declared here so the fact lives with
# the adapter rather than only in a document.
IS_MIGRATION_SOURCE: Final[bool] = True
