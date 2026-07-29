"""
One registration entry point for every spec-driven REST source.

Registering a source means: capability declaration (DL-CONN-17), params model, connector
class, and builder — the same four artefacts `/new-connector` scaffolds, produced from the
spec so a new source cannot land half-registered.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field, field_validator

from connector_runtime.adapters.rest_api.rest_api_connector import RestApiConnector
from connector_runtime.adapters.rest_api.rest_http_session import RestHttpSession
from connector_runtime.adapters.rest_api.rest_source_spec import (
    RestEntitySpec,
    RestSourceSpec,
    rest_source_spec_registry,
)
from connector_runtime.connection_credential_resolver import ConnectionCredentialPathResolver
from connector_runtime.credential_client import (
    SecretsManagerCredentialClient,
    SecretsManagerCredentialError,
)
from connector_runtime.interfaces.connector_interface import (
    ConnectorInterface,
    DeterministicConnectorError,
    ExtractionErrorClassification,
)
from connector_runtime.rate_limiting import (
    RateLimitPolicySpec,
    RateLimitStrategy,
    rate_limit_policy_registry,
)
from connector_runtime.raw_layer_writer import RawLayerWriter
from connector_runtime.registry import connector_registry
from connector_runtime.source_capabilities import source_capability_registry
from contracts.identifier_policy import DEFAULT_TENANT_CODE
from observability.structured_logger import get_platform_logger
from tenancy.connection_keys import raw_layer_path_segments, resolve_connection_id

_logger = get_platform_logger(__name__)

# Fallback policy for a source whose spec names none, so no adapter is ever unthrottled.
_FALLBACK_POLICY_NAME = "rest-source-default"
rate_limit_policy_registry.register(
    _FALLBACK_POLICY_NAME,
    RateLimitPolicySpec(RateLimitStrategy.RETRY_AFTER, base_backoff_seconds=1.0),
)


class RestSourceParams(BaseModel):
    """
    Connector params for a spec-driven REST source.

    `extra="forbid"` because this is the boundary where config-supplied params are
    validated; a catch-all escape hatch here is how injection reaches an adapter.

    The `entity_*` fields below let the configuration console declare an entity this repo
    has never heard of (DL-CONN-21). Salesforce (`object_name`), MySQL (`table_name`) and
    NetSuite (`record_type`) have always taken their entity from configuration; the REST
    substrate was the only family that required a code change, which contradicted the
    platform's own configuration-driven premise. Each field is named explicitly rather than
    collected in a free-form dict, so this stays a validated boundary.
    """

    model_config = {"extra": "forbid"}

    entity_id: str = Field(description="Entity this run extracts.")
    connection_id: str | None = Field(
        default=None, description="Connector instance; None means the source's default."
    )
    page_size: int | None = Field(default=None, ge=1, le=1_000)
    rate_limit_policy: str | None = None

    # ── Config-declared entity (DL-CONN-21) ──────────────────────────────────
    # Supplied only when `entity_id` is not one the spec declares. `entity_path` is the
    # trigger: without it an unknown entity is a configuration error, not a silent guess.
    entity_path: str | None = Field(
        default=None,
        description="Endpoint path for an entity the console declared, e.g. '/api/v2/quotes'.",
    )
    entity_records_json_path: str | None = Field(
        default=None,
        description="Dotted path to the record array, e.g. 'Result.Items'. Blank means the "
        "response body is itself the array. Omit to inherit the source's convention.",
    )
    entity_watermark_field: str | None = None
    entity_natural_key_field: str | None = None
    entity_pagination_strategy: str | None = None
    entity_record_unwrap_field: str | None = None
    entity_read_method: str | None = Field(
        default=None, description="GET or POST. A read is never issued as a mutating verb."
    )

    @field_validator("entity_id")
    @classmethod
    def _entity_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("entity_id must not be empty.")
        return value

    @field_validator("entity_read_method")
    @classmethod
    def _read_method_is_a_read(cls, value: str | None) -> str | None:
        if value is not None and value.upper() not in ("GET", "POST"):
            raise ValueError("entity_read_method must be GET or POST.")
        return None if value is None else value.upper()

    def declared_records_json_path(self) -> tuple[str, ...] | None:
        """The dotted path as a tuple; `None` means inherit, `()` means a bare array body."""
        if self.entity_records_json_path is None:
            return None
        stripped = self.entity_records_json_path.strip()
        return () if not stripped else tuple(stripped.split("."))


class RestSourceRawLayerWriter(RawLayerWriter):
    """Raw-layer writer for a spec-driven source; only the path segments differ."""

    error_cls = SecretsManagerCredentialError
    log_prefix = "rest-source"


class UndeclaredEntityError(DeterministicConnectorError):
    """An entity neither this repo nor the configuration describes well enough to call."""

    classification = ExtractionErrorClassification.DETERMINISTIC_INVALID_CONFIGURATION


def resolve_entity_spec(spec: RestSourceSpec, params: RestSourceParams) -> RestEntitySpec:
    """
    The entity this run extracts: the spec's if it declares one, otherwise the console's.

    A spec-declared entity always wins, so configuration cannot silently redirect a curated
    endpoint somewhere else. Anything else needs `entity_path`; without it the run fails
    with a message naming exactly what to supply, rather than a bare `KeyError`.
    """
    if spec.declares(params.entity_id):
        return spec.entity(params.entity_id)
    if not params.entity_path:
        raise UndeclaredEntityError(
            f"Source {spec.source_id!r} does not declare entity {params.entity_id!r}, and the "
            "entity configuration supplies no 'entity_path'. Either set entity_path (plus any "
            "of entity_records_json_path / entity_watermark_field / entity_natural_key_field / "
            "entity_pagination_strategy) in the entity's connector_params, or use one of the "
            f"declared entities: {sorted(spec.entity_ids())}."
        )
    return spec.entity_from_configuration(
        params.entity_id,
        params.entity_path,
        records_json_path=params.declared_records_json_path(),
        watermark_field=params.entity_watermark_field,
        natural_key_field=params.entity_natural_key_field,
        pagination_strategy=params.entity_pagination_strategy,
        page_size=params.page_size,
        record_unwrap_field=params.entity_record_unwrap_field,
        read_method=params.entity_read_method,
    )


def register_rest_source(spec: RestSourceSpec) -> RestSourceSpec:
    """Register a spec's capability declaration, connector, params model, and builder."""
    rest_source_spec_registry.register(spec)
    source_capability_registry.register(spec.to_capability_declaration())

    # The generated class is a RestApiConnector subclass, so the builder can construct it with
    # the spec-driven signature rather than the bare ConnectorInterface one.
    connector_cls: type[RestApiConnector] = type(
        f"{_class_prefix(spec.source_id)}Connector",
        (RestApiConnector,),
        {"__doc__": f"{spec.display_name} connector, driven by its registered spec."},
    )
    connector_registry.register(spec.source_id)(connector_cls)
    connector_registry.register_params_model(spec.source_id, RestSourceParams)
    connector_registry.register_builder(spec.source_id, _builder_for(spec, connector_cls))
    return spec


def _class_prefix(source_id: str) -> str:
    return "".join(part.capitalize() for part in source_id.split("-"))


def _builder_for(spec: RestSourceSpec, connector_cls: type[RestApiConnector]) -> Any:
    def build(
        environment: str,
        region_name: str,
        connector_params: Mapping[str, str],
        raw_s3_bucket: str,
        tenant_code: str = DEFAULT_TENANT_CODE,
    ) -> tuple[ConnectorInterface, RawLayerWriter]:
        params = RestSourceParams.model_validate(dict(connector_params))
        connection_id = resolve_connection_id(spec.source_id, params.connection_id)
        policy_name = (
            params.rate_limit_policy or spec.default_rate_limit_policy or _FALLBACK_POLICY_NAME
        )
        policy = rate_limit_policy_registry.resolve(policy_name, connection_id)

        import boto3

        resolver = ConnectionCredentialPathResolver(
            boto3.client("secretsmanager", region_name=region_name)
        )
        resolved_path = resolver.resolve(tenant_code, spec.source_id, connection_id)
        credentials = SecretsManagerCredentialClient(
            secret_id=resolved_path.secret_id,
            region_name=region_name,
            required_keys=spec.required_credential_keys,
            source_label=spec.display_name,
            log_event="rest_source_credentials_loaded",
            log_fields={"source_id": spec.source_id, "connection_id": connection_id},
        ).get_credentials()

        session = RestHttpSession(spec, credentials, policy)
        connector = connector_cls(
            spec=spec,
            entity_id=params.entity_id,
            session=session,
            rate_limit_policy=policy,
            connection_id=connection_id,
            entity=resolve_entity_spec(spec, params),
        )
        writer = RestSourceRawLayerWriter(
            s3_bucket=raw_s3_bucket,
            path_segments=raw_layer_path_segments(spec.source_id, connection_id),
            region_name=region_name,
            tenant_code=tenant_code,
        )
        return connector, writer

    return build
