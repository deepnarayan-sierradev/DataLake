"""
Source-capability declaration exposed through the registry (DL-CONN-17).

The configuration console renders only what a source actually supports instead of
hardcoding source names, so onboarding a new connector needs no console change.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class SourceCapability(StrEnum):
    """The capability vocabulary the console reads."""

    INCREMENTAL = "incremental"
    SOFT_DELETE = "soft_delete"
    WEBHOOKS = "webhooks"
    BULK_EXPORT = "bulk_export"
    WRITEBACK = "writeback"
    SCHEMA_DISCOVERY = "schema_discovery"
    RECORD_COUNT = "record_count"
    REPORT_STYLE = "report_style"
    ASYNC_JOB = "async_job"


class SourceCapabilityUnavailableError(Exception):
    """
    Raised when a declared-but-absent source endpoint is called.

    Distinguishable from a generic failure so a vendor API still under construction
    (ServMan Pro) surfaces as a capability gap rather than an outage (DL-CONN-04).
    """


@dataclass(frozen=True)
class SourceCapabilityDeclaration:
    """What one connector can do, and the strategies it defaults to."""

    source_id: str
    display_name: str
    capabilities: frozenset[SourceCapability]
    default_sync_strategy: str = "watermark_polling"
    default_pagination_strategy: str = "offset_limit"
    default_rate_limit_policy: str | None = None
    webhook_signature_algorithm: str | None = None
    allowed_hostnames: tuple[str, ...] = ()
    notes: str = ""

    def supports(self, capability: SourceCapability) -> bool:
        return capability in self.capabilities

    def require(self, capability: SourceCapability) -> None:
        """Raise a distinguishable error when a caller needs an unsupported capability."""
        if not self.supports(capability):
            raise SourceCapabilityUnavailableError(
                f"Source {self.source_id!r} does not support {capability.value!r}. "
                f"Declared capabilities: {sorted(c.value for c in self.capabilities)}."
            )


class SourceCapabilityRegistry:
    """One declaration per source_id, registered at import time."""

    def __init__(self) -> None:
        self._declarations: dict[str, SourceCapabilityDeclaration] = {}

    def register(self, declaration: SourceCapabilityDeclaration) -> None:
        if declaration.source_id in self._declarations:
            raise ValueError(
                f"Capability declaration for source_id {declaration.source_id!r} already exists."
            )
        self._declarations[declaration.source_id] = declaration

    def get(self, source_id: str) -> SourceCapabilityDeclaration:
        declaration = self._declarations.get(source_id)
        if declaration is None:
            raise KeyError(
                f"No capability declaration for source_id {source_id!r}. "
                f"Declared: {self.registered_source_ids()}."
            )
        return declaration

    def registered_source_ids(self) -> list[str]:
        return sorted(self._declarations)

    def all_declarations(self) -> tuple[SourceCapabilityDeclaration, ...]:
        return tuple(self._declarations[k] for k in sorted(self._declarations))

    def sources_supporting(self, capability: SourceCapability) -> list[str]:
        return sorted(
            source_id
            for source_id, declaration in self._declarations.items()
            if declaration.supports(capability)
        )

    def allowed_hostnames(self) -> frozenset[str]:
        """
        Union of every declared source hostname.

        OWASP A10: outbound HTTP is restricted to this allowlist, so a tampered `base_url`
        in connector params cannot turn an extraction into an SSRF probe.
        """
        hosts: set[str] = set()
        for declaration in self._declarations.values():
            hosts.update(declaration.allowed_hostnames)
        return frozenset(hosts)

    def reset(self) -> None:
        """Testing only."""
        self._declarations.clear()


source_capability_registry: Final[SourceCapabilityRegistry] = SourceCapabilityRegistry()


class OutboundHostNotAllowedError(Exception):
    """Raised when an adapter attempts an outbound call to a non-allowlisted host."""


def enforce_allowed_host(source_id: str, hostname: str) -> None:
    """
    Reject an outbound host the source did not declare (OWASP A10 — SSRF).

    A source declaring no hostnames is unrestricted by declaration, not by omission: the
    registry records that explicitly so the gap is visible rather than implied.
    """
    declaration = source_capability_registry.get(source_id)
    if not declaration.allowed_hostnames:
        return
    normalised = hostname.strip().lower()
    if normalised not in {h.lower() for h in declaration.allowed_hostnames}:
        raise OutboundHostNotAllowedError(
            f"Source {source_id!r} may only call {sorted(declaration.allowed_hostnames)}; "
            f"{hostname!r} is not allowlisted."
        )
