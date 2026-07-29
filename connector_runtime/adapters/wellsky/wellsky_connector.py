"""
WellSky connector (DL-CONN-05) — Executive Home Care operations.

Two properties of this source drive its shape: ~50 tables, so every entity is an
independently-scheduled extraction rather than one run; and documented rate limits with
undocumented numbers, so the policy is adaptive rather than a guessed fixed schedule.

**PHI-bearing.** Home care records are PHI, so this source is gated by `DL-PORT-08` and
must not be onboarded before a BAA is recorded.
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

SOURCE_ID: Final[str] = "wellsky"

# The ~50-table model, extracted as independent scheduled entities. Grouped by domain so an
# operator can enable a domain at a time rather than all fifty at once.
_CLIENT_ENTITIES: Final[tuple[str, ...]] = (
    "client",
    "client-address",
    "client-contact",
    "client-diagnosis",
    "client-authorization",
    "client-service-plan",
    "client-note",
    "client-document",
    "client-payer",
    "client-referral",
)
_CAREGIVER_ENTITIES: Final[tuple[str, ...]] = (
    "caregiver",
    "caregiver-address",
    "caregiver-availability",
    "caregiver-certification",
    "caregiver-skill",
    "caregiver-compliance",
    "caregiver-note",
    "caregiver-document",
    "caregiver-pay-rate",
    "caregiver-training",
)
_SCHEDULING_ENTITIES: Final[tuple[str, ...]] = (
    "shift",
    "shift-task",
    "visit",
    "visit-verification",
    "schedule-template",
    "schedule-exception",
    "time-entry",
    "mileage-entry",
    "shift-offer",
    "shift-cancellation",
)
_BILLING_ENTITIES: Final[tuple[str, ...]] = (
    "invoice",
    "invoice-line",
    "payment",
    "payer",
    "payer-rate",
    "authorization",
    "claim",
    "claim-line",
    "adjustment",
    "write-off",
)
_REFERENCE_ENTITIES: Final[tuple[str, ...]] = (
    "office",
    "service-code",
    "task-code",
    "diagnosis-code",
    "discipline",
    "user",
    "role",
    "region",
    "franchise",
    "holiday-calendar",
)

_WATERMARKED_DOMAINS: Final[tuple[tuple[str, ...], ...]] = (
    _CLIENT_ENTITIES,
    _CAREGIVER_ENTITIES,
    _SCHEDULING_ENTITIES,
    _BILLING_ENTITIES,
)


def _entity(suffix: str, watermark: str | None) -> RestEntitySpec:
    return RestEntitySpec(
        entity_id=f"{SOURCE_ID}-{suffix}",
        path=f"/api/v2/{suffix.replace('-', '/')}",
        records_json_path=("data", "records"),
        watermark_field=watermark,
        natural_key_field="id",
        pagination_strategy="keyset",
        keyset_field="id",
        page_size=200,
    )


def _all_entities() -> tuple[RestEntitySpec, ...]:
    entities = [
        _entity(suffix, "lastModifiedUtc") for domain in _WATERMARKED_DOMAINS for suffix in domain
    ]
    # Reference data has no reliable modification stamp; a full reload is cheaper than
    # incorrect incremental state.
    entities.extend(_entity(suffix, None) for suffix in _REFERENCE_ENTITIES)
    return tuple(entities)


WELLSKY_SPEC: Final[RestSourceSpec] = RestSourceSpec(
    source_id=SOURCE_ID,
    display_name="WellSky",
    base_url="https://api.wellsky.com",
    auth_kind=AuthKind.OAUTH2_REFRESH,
    entities=_all_entities(),
    capabilities=frozenset(
        {
            SourceCapability.INCREMENTAL,
            SourceCapability.SCHEMA_DISCOVERY,
            SourceCapability.RECORD_COUNT,
        }
    ),
    default_pagination_strategy="keyset",
    default_rate_limit_policy="wellsky-conservative",
    required_credential_keys=frozenset({"access_token", "refresh_token", "client_id"}),
    watermark_lower_parameter="modifiedSinceUtc",
    watermark_upper_parameter="modifiedBeforeUtc",
    notes=(
        "Executive Home Care. PHI-bearing — blocked by the DL-PORT-08 onboarding gate until "
        "a BAA is recorded. ~50 tables, each independently scheduled."
    ),
)

register_rest_source(WELLSKY_SPEC)

# Consumed by the PHI onboarding gate; declared here so the fact lives with the adapter.
IS_PHI_BEARING: Final[bool] = True
