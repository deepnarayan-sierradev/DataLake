"""
HubSpot connector (DL-CONN-01) with the bi-directional write path (DL-CONN-02).

Highest-priority source on the customer list: it serves rows 5, 10, and 11 (Evive,
Brothers Gutters, Grasons), all of which are Franchise Management System use cases, so it
is also the source that makes `connection_id` load-bearing — three brands on one connector
type under one tenant.

Custom objects are discovered at runtime through the schema endpoint rather than declared,
so onboarding a brand's custom object needs no code change.
"""

from __future__ import annotations

from typing import Final

from connector_runtime.adapters.rest_api.rest_adapter_registration import register_rest_source
from connector_runtime.adapters.rest_api.rest_http_session import RestHttpSession
from connector_runtime.adapters.rest_api.rest_source_spec import (
    AuthKind,
    RestEntitySpec,
    RestSourceSpec,
)
from connector_runtime.source_capabilities import SourceCapability

SOURCE_ID: Final[str] = "hubspot"

# CRM object endpoints. `properties` is supplied per run from the discovered field set, so
# adding a HubSpot property does not require touching this table.
_CRM_OBJECTS: Final[tuple[tuple[str, str], ...]] = (
    ("companies", "company"),
    ("contacts", "contact"),
    ("deals", "deal"),
    ("tickets", "ticket"),
    ("line_items", "line-item"),
)


def _crm_entity(object_path: str, entity_suffix: str) -> RestEntitySpec:
    return RestEntitySpec(
        entity_id=f"{SOURCE_ID}-{entity_suffix}",
        path=f"/crm/v3/objects/{object_path}",
        records_json_path=("results",),
        watermark_field="updatedAt",
        natural_key_field="id",
        pagination_strategy="cursor",
        page_size=100,
        writeback_path=f"/crm/v3/objects/{object_path}",
        writeback_external_id_field="id",
    )


HUBSPOT_SPEC: Final[RestSourceSpec] = RestSourceSpec(
    source_id=SOURCE_ID,
    display_name="HubSpot",
    base_url="https://api.hubapi.com",
    auth_kind=AuthKind.BEARER_TOKEN,
    entities=(
        *(_crm_entity(path, suffix) for path, suffix in _CRM_OBJECTS),
        RestEntitySpec(
            entity_id=f"{SOURCE_ID}-engagement",
            path="/crm/v3/objects/notes",
            watermark_field="updatedAt",
            pagination_strategy="cursor",
        ),
        RestEntitySpec(
            entity_id=f"{SOURCE_ID}-owner",
            path="/crm/v3/owners",
            watermark_field="updatedAt",
            pagination_strategy="cursor",
        ),
        RestEntitySpec(
            entity_id=f"{SOURCE_ID}-pipeline",
            path="/crm/v3/pipelines/deals",
            records_json_path=("results",),
            # Pipelines are small reference data with no watermark; a full load each run
            # is cheaper than tracking one.
            pagination_strategy="offset_limit",
        ),
        RestEntitySpec(
            entity_id=f"{SOURCE_ID}-custom-object-schema",
            path="/crm/v3/schemas",
            records_json_path=("results",),
            pagination_strategy="offset_limit",
        ),
    ),
    capabilities=frozenset(
        {
            SourceCapability.INCREMENTAL,
            SourceCapability.SOFT_DELETE,
            SourceCapability.WEBHOOKS,
            SourceCapability.WRITEBACK,
            SourceCapability.SCHEMA_DISCOVERY,
            SourceCapability.RECORD_COUNT,
        }
    ),
    default_pagination_strategy="cursor",
    # HubSpot CRM v3 documents `properties` as the field-projection parameter. It is
    # declared here rather than assumed by the substrate: no other source on the platform
    # documents one, and sending it to an API that validates its query string is a 400.
    field_projection_parameter="properties",
    default_rate_limit_policy="hubspot-standard",
    default_sync_strategy="webhook_ingest",
    required_credential_keys=frozenset({"access_token"}),
    watermark_lower_parameter="hs_lastmodifieddate__gte",
    watermark_upper_parameter="hs_lastmodifieddate__lt",
    webhook_signature_algorithm="hmac_sha256_base64",
    notes="Franchise Management System source; serves Evive, Brothers Gutters, and Grasons.",
)

register_rest_source(HUBSPOT_SPEC)


def discover_custom_object_entities(session: RestHttpSession) -> tuple[RestEntitySpec, ...]:
    """
    Read HubSpot's schema endpoint and derive an entity spec per custom object.

    Returned rather than registered so the caller decides whether a newly-discovered custom
    object is onboarded — auto-registering would start extracting data nobody configured.
    """
    response = session.get("/crm/v3/schemas")
    discovered: list[RestEntitySpec] = []
    for schema in response.records(("results",)):
        object_name = str(schema.get("name") or schema.get("objectTypeId") or "").strip().lower()
        if not object_name:
            continue
        slug = object_name.replace("_", "-")
        entity_id = f"{SOURCE_ID}-{slug}"
        if entity_id in HUBSPOT_SPEC.entity_ids():
            continue
        discovered.append(
            RestEntitySpec(
                entity_id=entity_id,
                path=f"/crm/v3/objects/{object_name}",
                records_json_path=("results",),
                watermark_field="updatedAt",
                pagination_strategy="cursor",
            )
        )
    return tuple(discovered)
