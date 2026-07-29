"""
Connection-aware key construction (DL-SCOPE-04) — one place per layer form.

`connection_id` becomes the identity component; `source_id` is retained as an attribute
for browsing, adapter routing, and catalog display. Existing single-connection sources
carry `connection_id == source_id`, which makes every DynamoDB key and schedule name
byte-identical to the pre-DL-12 form and the migration non-destructive.
"""

from __future__ import annotations

from contracts.identifier_policy import (
    tenant_scoped_key,
    validate_stable_id,
    validate_tenant_code,
)


def resolve_connection_id(source_id: str, connection_id: str | None) -> str:
    """The migration identity: an absent connection_id is the source's default connection."""
    return connection_id if connection_id else source_id


def connection_scoped_key(tenant_code: str, source_id: str, connection_id: str | None) -> str:
    """Composite partition key for entity config and watermarks."""
    validate_tenant_code(tenant_code)
    resolved = resolve_connection_id(source_id, connection_id)
    validate_stable_id(resolved, "connection_id")
    return tenant_scoped_key(tenant_code, resolved)


def raw_layer_path_segments(source_id: str, connection_id: str | None) -> list[str]:
    """
    Raw S3 segments between the tenant prefix and the entity id.

    `{tenant_code}/{source_id}/{connection_id}/{entity_id}/...`, collapsing to
    `{tenant_code}/{source_id}/{entity_id}/...` for the default connection — emitting
    `salesforce/salesforce` there would reintroduce the doubled-source-segment defect
    RAW-1 fixed, and it would force a re-extraction of already-landed data for no
    isolation gain, since a default connection is the only one under its source.
    """
    validate_stable_id(source_id, "source_id")
    resolved = resolve_connection_id(source_id, connection_id)
    validate_stable_id(resolved, "connection_id")
    if resolved == source_id:
        return [source_id]
    return [source_id, resolved]


def curated_glue_table_name(
    tenant_code: str, connection_id: str, entity_id: str, domain: str
) -> str:
    """
    Curated Glue table name: `{tenant}_{connection}_{entity}_{domain}_curated`.

    Glue and Athena permit only `[a-z0-9_]`, so hyphens normalise to underscores.
    """
    validate_tenant_code(tenant_code)
    validate_stable_id(connection_id, "connection_id")
    parts = (tenant_code, connection_id, entity_id, domain, "curated")
    return "_".join(part.replace("-", "_") for part in parts)


def schedule_name_parts(
    tenant_code: str, source_id: str, connection_id: str | None
) -> tuple[str, str]:
    """`(tenant_code, connection_id)` for `{tenant}--{connection_id}--{entity}` names."""
    validate_tenant_code(tenant_code)
    resolved = resolve_connection_id(source_id, connection_id)
    validate_stable_id(resolved, "connection_id")
    return tenant_code, resolved
