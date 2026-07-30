"""
Declarative REST/report source specification.

Twelve connectors composed from one substrate rather than twelve bespoke adapters — the reuse
threshold in this programme is two, not three. A new source is a spec plus a registration,
which is what makes `DL-CONN-16` (scaffolding parity) real rather than aspirational.

`DL-CONN-20` (specification fidelity) is why the fields below are as numerous as they are.
Every one of them exists because a *published* vendor document required it and the substrate
could not express it: `PaginationParameters` because MaidCentral pages on
`skipCount`/`maxResultCount` and WellSky on `_page`/`_count`; `record_unwrap_field` because a
FHIR bundle nests each row under `resource`; `read_method` and `watermark_body_field` because
WellSky filters through a POST body; `api_key_value_prefix` because SeniorPlace's scheme word
is part of the header value; `token_endpoint_path` because three sources issue tokens shorter
than a full sweep; `required_run_parameters` because two BePro endpoints cannot be scheduled
without a scope. A field with no document behind it does not belong here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final
from urllib.parse import urlparse

from connector_runtime.pagination import PaginationParameters
from connector_runtime.source_capabilities import (
    SourceCapability,
    SourceCapabilityDeclaration,
)

# Endpoint templates come from a server-side spec or, since DL-CONN-21, from validated
# entity configuration. This guards against a path becoming a traversal or SSRF vector
# (OWASP A03, A10).
_SAFE_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^/[A-Za-z0-9/_.\-{}]{0,255}$")

# Ceiling on any single read. The extraction Lambda has 900s and reserves 120s to start,
# so one request must never be able to consume enough of the remainder that the run cannot
# checkpoint. 300s leaves room for at least two attempts plus the checkpoint write.
MAX_REQUEST_TIMEOUT_SECONDS: Final[float] = 300.0


def _reject_unsafe_path(owner: str, label: str, path: str) -> None:
    """
    Validate an endpoint path, rejecting traversal outright.

    The character class above admits `..` because `.` and `/` are both legal in a real path
    segment. That was survivable while every path came from this repo; a config-declared
    entity makes it a widening — `urljoin` clamps traversal at the host root, so the host
    allowlist still holds, but a console user could otherwise aim a read at any path on an
    allowlisted host. Segment-level rejection closes it.
    """
    if not _SAFE_PATH_PATTERN.match(path) or path.startswith("//"):
        # A leading `//` is a protocol-relative URL. It is defanged today only because
        # `_resolve_url` strips leading slashes before `urljoin`; rejecting it here means the
        # guarantee does not depend on that incidental detail.
        raise ValueError(f"{owner}: {label} {path!r} is not a safe endpoint path.")
    if any(segment == ".." for segment in path.split("/")):
        raise ValueError(
            f"{owner}: {label} {path!r} contains a parent-directory segment. An endpoint "
            "path must address its endpoint directly."
        )


class AuthKind(StrEnum):
    """How a source authenticates an outbound request."""

    BEARER_TOKEN = "bearer_token"  # noqa: S105 — auth-kind name, not a credential  # nosec B105
    API_KEY_HEADER = "api_key_header"
    BASIC = "basic"
    OAUTH2_REFRESH = "oauth2_refresh"
    SESSION_KEY_QUERY = "session_key_query"


class TokenGrantKind(StrEnum):
    """How a short-lived access token is obtained from the source's token endpoint."""

    PASSWORD = "password"  # noqa: S105 — grant name, not a credential  # nosec B105
    REFRESH_TOKEN = "refresh_token"  # noqa: S105 — grant name  # nosec B105
    CLIENT_CREDENTIALS = "client_credentials"
    SESSION_LOGIN = "session_login"


class EntityShape(StrEnum):
    """Whether an entity is a row collection or a report request."""

    ROW_COLLECTION = "row_collection"
    REPORT = "report"


@dataclass(frozen=True)
class RestEntitySpec:
    """One extractable entity of a REST source."""

    entity_id: str
    path: str
    records_json_path: tuple[str, ...] = ("results",)
    watermark_field: str | None = None
    natural_key_field: str = "id"
    shape: EntityShape = EntityShape.ROW_COLLECTION
    pagination_strategy: str | None = None
    page_size: int = 100
    keyset_field: str | None = None
    report_metrics: tuple[str, ...] = ()
    report_dimensions: tuple[str, ...] = ()
    writeback_path: str | None = None
    writeback_external_id_field: str | None = None
    static_query_parameters: Mapping[str, str] = field(default_factory=dict)
    # FHIR-style envelopes wrap each row: `entry: [{resource: {...}}]`. Naming the wrapper
    # keeps the unwrap declarative instead of a per-source branch in the connector.
    record_unwrap_field: str | None = None
    read_method: str = "GET"
    # Body filters for a POST-search read; the watermark is merged in at query-build time.
    search_body: Mapping[str, Any] = field(default_factory=dict)
    watermark_body_field: str | None = None
    watermark_comparator_prefix: str = ""
    # Parameters the provider requires and a scheduled run cannot infer (a match id, an
    # agency id). Declared so the guard fails closed with a configuration error rather than
    # letting the provider answer 422 and the retry policy treat it as transient.
    required_run_parameters: tuple[str, ...] = ()
    pagination_parameters: PaginationParameters | None = None

    def __post_init__(self) -> None:
        owner = f"entity {self.entity_id!r}"
        _reject_unsafe_path(owner, "path", self.path)
        if "{" in self.path or "}" in self.path:
            # The substrate does no path templating: a `{patient_id}` would be requested
            # literally and 404. Several WellSky and BePro endpoints are shaped this way,
            # so the trap is live rather than theoretical — such an entity needs a
            # parent-scoped fan-out, not a declaration.
            raise ValueError(
                f"{owner}: path {self.path!r} contains a path template. Nothing substitutes "
                "it, so the request would be issued literally. Declare a parent-scoped "
                "fan-out instead."
            )
        if self.writeback_path:
            _reject_unsafe_path(owner, "writeback_path", self.writeback_path)
        if self.shape is EntityShape.REPORT and not self.report_metrics:
            raise ValueError(
                f"entity {self.entity_id!r}: a report-shaped entity must declare metrics."
            )
        if self.read_method not in ("GET", "POST"):
            raise ValueError(
                f"entity {self.entity_id!r}: read_method {self.read_method!r} must be GET or "
                "POST — a read must never be issued as a mutating verb."
            )
        if self.watermark_body_field and self.read_method != "POST":
            raise ValueError(
                f"entity {self.entity_id!r}: watermark_body_field only applies to a POST-search "
                "read; a GET read carries its watermark in the query string."
            )

    @property
    def supports_writeback(self) -> bool:
        return bool(self.writeback_path and self.writeback_external_id_field)


@dataclass(frozen=True)
class RestSourceSpec:
    """Everything the substrate needs to extract one source system."""

    source_id: str
    display_name: str
    base_url: str
    auth_kind: AuthKind
    entities: tuple[RestEntitySpec, ...]
    capabilities: frozenset[SourceCapability]
    default_pagination_strategy: str = "offset_limit"
    default_rate_limit_policy: str | None = None
    default_sync_strategy: str = "watermark_polling"
    required_credential_keys: frozenset[str] = frozenset({"access_token"})
    api_key_header_name: str = "Authorization"
    # SeniorPlace sends `Authorization: ApiKey <key>`; the scheme word is part of the value,
    # not the header name, so it is declared rather than folded into the key itself.
    api_key_value_prefix: str = ""
    # The parameter this provider uses to project a field subset — HubSpot's `properties`.
    # Opt-in, because it used to be sent unconditionally: five of the sources audited on
    # 2026-07-29 document an exact parameter list that does not include it, and an API that
    # validates its query string rejects the request rather than ignoring the extra key.
    field_projection_parameter: str | None = None
    watermark_lower_parameter: str = "updated_after"
    watermark_upper_parameter: str = "updated_before"
    webhook_signature_algorithm: str | None = None
    pagination_parameters: PaginationParameters = field(default_factory=PaginationParameters)
    # Sources whose credential is a long-lived secret exchanged for a short-lived token.
    token_endpoint_path: str | None = None
    token_grant_kind: TokenGrantKind | None = None
    session_key_parameter: str = "sessionKey"
    # Conventions a config-declared entity inherits, so the console only has to supply the
    # endpoint path (DL-CONN-21). Every entity of a source shares its envelope shape and a
    # sensible page size; repeating them per entity is what made the declared list feel
    # like the only way to add one.
    default_records_json_path: tuple[str, ...] = ("results",)
    default_page_size: int = 100
    # Per-source read timeout. A single platform-wide 30s was fine for a row collection and
    # wrong for a report: an unpaginated per-frame or payroll read is slow by nature, and a
    # timeout shorter than the response makes the entity permanently unextractable while
    # looking like a transient network fault. Bounded below the Lambda budget so a slow
    # source still checkpoints rather than being killed mid-write.
    request_timeout_seconds: float = 30.0
    notes: str = ""

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            # OWASP A02: no source is contacted over plaintext, and a relative base_url
            # would make the allowlist unenforceable.
            raise ValueError(
                f"source {self.source_id!r}: base_url {self.base_url!r} must be an absolute "
                "https URL."
            )
        if not self.entities:
            raise ValueError(f"source {self.source_id!r} declares no entities.")
        duplicates = _duplicates(e.entity_id for e in self.entities)
        if duplicates:
            raise ValueError(
                f"source {self.source_id!r} declares duplicate entity ids: {sorted(duplicates)}."
            )
        if self.token_endpoint_path:
            _reject_unsafe_path(
                f"source {self.source_id!r}", "token_endpoint_path", self.token_endpoint_path
            )
        if not 1.0 <= self.request_timeout_seconds <= MAX_REQUEST_TIMEOUT_SECONDS:
            raise ValueError(
                f"source {self.source_id!r}: request_timeout_seconds "
                f"{self.request_timeout_seconds} must be between 1 and "
                f"{MAX_REQUEST_TIMEOUT_SECONDS}. A single read may not consume so much of "
                "the Lambda budget that the run cannot checkpoint and exit cleanly."
            )
        if bool(self.token_endpoint_path) != bool(self.token_grant_kind):
            raise ValueError(
                f"source {self.source_id!r}: a token endpoint and a grant kind are only "
                "meaningful together — declaring one without the other silently disables the "
                "token exchange."
            )

    @property
    def hostname(self) -> str:
        return urlparse(self.base_url).netloc.lower()

    def pagination_names_for(self, entity: RestEntitySpec) -> PaginationParameters:
        """Entity-level parameter naming wins; otherwise the source's."""
        return entity.pagination_parameters or self.pagination_parameters

    def entity(self, entity_id: str) -> RestEntitySpec:
        for candidate in self.entities:
            if candidate.entity_id == entity_id:
                return candidate
        raise KeyError(
            f"source {self.source_id!r} has no entity {entity_id!r}. "
            f"Declared: {sorted(e.entity_id for e in self.entities)}."
        )

    def entity_ids(self) -> tuple[str, ...]:
        return tuple(e.entity_id for e in self.entities)

    def declares(self, entity_id: str) -> bool:
        return any(candidate.entity_id == entity_id for candidate in self.entities)

    def entity_from_configuration(
        self,
        entity_id: str,
        path: str,
        *,
        records_json_path: tuple[str, ...] | None = None,
        watermark_field: str | None = None,
        natural_key_field: str | None = None,
        pagination_strategy: str | None = None,
        page_size: int | None = None,
        record_unwrap_field: str | None = None,
        read_method: str | None = None,
    ) -> RestEntitySpec:
        """
        Build an entity spec the console declared rather than this repo (DL-CONN-21).

        Everything the caller leaves unset is inherited from the source's own conventions,
        so onboarding an endpoint the vendor added yesterday means supplying a path — the
        same shape as adding a Salesforce object or a MySQL table, which have always been
        pure configuration.

        Deliberately *not* settable from configuration: `writeback_path` and
        `writeback_external_id_field`. Enabling a read must never be able to enable a source
        mutation, which is the same rule `writeback_enabled` enforces at the entity level.
        The path is still validated by `RestEntitySpec.__post_init__` (no traversal) and the
        resolved host is still checked against the allowlist at call time (OWASP A03, A10).
        """
        return RestEntitySpec(
            entity_id=entity_id,
            path=path,
            records_json_path=(
                self.default_records_json_path if records_json_path is None else records_json_path
            ),
            watermark_field=watermark_field,
            natural_key_field=natural_key_field or "id",
            pagination_strategy=pagination_strategy or self.default_pagination_strategy,
            page_size=page_size or self.default_page_size,
            record_unwrap_field=record_unwrap_field,
            read_method=read_method or "GET",
        )

    def to_capability_declaration(self) -> SourceCapabilityDeclaration:
        """The console-facing declaration derived from this spec (DL-CONN-17)."""
        return SourceCapabilityDeclaration(
            source_id=self.source_id,
            display_name=self.display_name,
            capabilities=self.capabilities,
            default_sync_strategy=self.default_sync_strategy,
            default_pagination_strategy=self.default_pagination_strategy,
            default_rate_limit_policy=self.default_rate_limit_policy,
            webhook_signature_algorithm=self.webhook_signature_algorithm,
            allowed_hostnames=(self.hostname,),
            notes=self.notes,
        )


def _duplicates(values: Any) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


class RestSourceSpecRegistry:
    """One spec per source_id, registered at import time."""

    def __init__(self) -> None:
        self._specs: dict[str, RestSourceSpec] = {}

    def register(self, spec: RestSourceSpec) -> RestSourceSpec:
        if spec.source_id in self._specs:
            raise ValueError(f"REST source spec for {spec.source_id!r} is already registered.")
        self._specs[spec.source_id] = spec
        return spec

    def get(self, source_id: str) -> RestSourceSpec:
        spec = self._specs.get(source_id)
        if spec is None:
            raise KeyError(
                f"No REST source spec registered for {source_id!r}. "
                f"Registered: {self.registered_source_ids()}."
            )
        return spec

    def registered_source_ids(self) -> list[str]:
        return sorted(self._specs)

    def reset(self) -> None:
        """Testing only."""
        self._specs.clear()


rest_source_spec_registry: Final[RestSourceSpecRegistry] = RestSourceSpecRegistry()


# Re-exported so a spec module imports its whole vocabulary from one place. Defined in
# `pagination` because that is where the strategies consume it; two copies of one dataclass
# would drift the moment a provider needed a knob only one of them knew about.
__all__ = [
    "AuthKind",
    "EntityShape",
    "PaginationParameters",
    "RestEntitySpec",
    "RestSourceSpec",
    "RestSourceSpecRegistry",
    "TokenGrantKind",
    "rest_source_spec_registry",
]
