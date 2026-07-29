"""
Declarative REST/report source specification.

Ten connectors composed from one substrate rather than ten bespoke adapters — the reuse
threshold in this programme is two, not three. A new source is a spec plus a registration,
which is what makes `DL-CONN-16` (scaffolding parity) real rather than aspirational.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final
from urllib.parse import urlparse

from connector_runtime.source_capabilities import (
    SourceCapability,
    SourceCapabilityDeclaration,
)

# Endpoint templates come from server-side specs only; this guards against a spec typo
# becoming a path-traversal or SSRF vector (OWASP A03, A10).
_SAFE_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(r"^/[A-Za-z0-9/_.\-{}]{0,255}$")


class AuthKind(StrEnum):
    """How a source authenticates an outbound request."""

    BEARER_TOKEN = "bearer_token"  # noqa: S105 — auth-kind name, not a credential  # nosec B105
    API_KEY_HEADER = "api_key_header"
    BASIC = "basic"
    OAUTH2_REFRESH = "oauth2_refresh"


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

    def __post_init__(self) -> None:
        if not _SAFE_PATH_PATTERN.match(self.path):
            raise ValueError(
                f"entity {self.entity_id!r}: path {self.path!r} is not a safe endpoint path."
            )
        if self.writeback_path and not _SAFE_PATH_PATTERN.match(self.writeback_path):
            raise ValueError(
                f"entity {self.entity_id!r}: writeback_path {self.writeback_path!r} is not a "
                "safe endpoint path."
            )
        if self.shape is EntityShape.REPORT and not self.report_metrics:
            raise ValueError(
                f"entity {self.entity_id!r}: a report-shaped entity must declare metrics."
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
    watermark_lower_parameter: str = "updated_after"
    watermark_upper_parameter: str = "updated_before"
    webhook_signature_algorithm: str | None = None
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

    @property
    def hostname(self) -> str:
        return urlparse(self.base_url).netloc.lower()

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
