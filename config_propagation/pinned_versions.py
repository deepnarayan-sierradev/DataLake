"""
Run-level configuration pinning (DL-CFG-01, DL-CFG-02, DL-CFG-03).

The pipeline trigger resolves every `latest` pointer once and threads the resolved set
through the Step Functions payload as `config_versions`. Downstream stages consume the
pinned versions and never resolve `latest` themselves, so a publish between two stages
cannot split one run across two configuration generations.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

from pydantic import BaseModel, Field, field_validator

from config_propagation.capability import ConfigCapability

# Pinned version strings come from config artefacts, never from request input (OWASP A03).
_SAFE_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}$")

# Sentinel that must never appear in a pinned set — its presence means pinning was bypassed.
UNPINNED_SENTINEL: Final[str] = "latest"


class ConfigVersionPinError(Exception):
    """Raised when a pinned version is absent or no longer resolves (DL-CFG-02)."""


class ConfigVersionMismatchError(Exception):
    """Raised when a stage observes a version different from the run's pinned one."""


class PinnedConfigVersions(BaseModel):
    """
    The resolved configuration set for one run.

    Serialised into the Step Functions payload alongside `tenant_code` and `entity_type`,
    extending the threading pattern already in `orchestration/main.tf` rather than
    inventing a second mechanism.
    """

    model_config = {"frozen": True, "extra": "forbid"}

    versions: dict[str, str] = Field(default_factory=dict)
    pinned_at: str = Field(description="ISO-8601 UTC timestamp the pointers were resolved.")
    config_schema_version: int = Field(default=1, ge=1)

    @field_validator("versions")
    @classmethod
    def _validate_versions(cls, value: dict[str, str]) -> dict[str, str]:
        known = {c.value for c in ConfigCapability}
        for capability, version in value.items():
            if capability not in known:
                raise ValueError(
                    f"Pinned capability {capability!r} is not a known ConfigCapability. "
                    f"Known: {sorted(known)}."
                )
            if version == UNPINNED_SENTINEL:
                raise ValueError(
                    f"Capability {capability!r} is pinned to {UNPINNED_SENTINEL!r}, which is "
                    "not a version. Resolve the pointer at the run boundary (DL-CFG-01)."
                )
            if not _SAFE_VERSION_PATTERN.match(version):
                raise ValueError(
                    f"Pinned version {version!r} for capability {capability!r} is not a safe "
                    "version identifier."
                )
        return value

    def require(self, capability: ConfigCapability) -> str:
        """
        Return the pinned version, failing closed when it is absent.

        A silent fallback to `latest` would defeat the pinning, so an unpinned capability
        raises rather than resolving (DL-CFG-02).
        """
        version = self.versions.get(capability.value)
        if not version:
            raise ConfigVersionPinError(
                f"No pinned version for capability {capability.value!r} in this run's "
                "config_versions. The stage must not fall back to 'latest' — re-trigger the "
                "run so the pointer is resolved at the run boundary."
            )
        return version

    def get(self, capability: ConfigCapability) -> str | None:
        """Pinned version when the run pinned this capability, else None."""
        return self.versions.get(capability.value)

    def assert_matches(self, capability: ConfigCapability, observed_version: str) -> None:
        """
        Raise when a stage observed a version other than the pinned one.

        Feeds `ConfigVersionMismatchWithinRun`, which must stay at zero once pinning is
        live — any non-zero value means a stage bypassed the pinned set.
        """
        pinned = self.versions.get(capability.value)
        if pinned is not None and pinned != observed_version:
            raise ConfigVersionMismatchError(
                f"Capability {capability.value!r} was pinned to {pinned!r} at the run boundary "
                f"but this stage observed {observed_version!r}. One run must span exactly one "
                "configuration generation."
            )

    def with_capability(self, capability: ConfigCapability, version: str) -> PinnedConfigVersions:
        return self.model_copy(update={"versions": {**self.versions, capability.value: version}})

    def to_payload(self) -> dict[str, Any]:
        """Step Functions `Parameters`-safe representation."""
        return self.model_dump(mode="json")

    def audit_fingerprint(self) -> str:
        """Stable, order-independent rendering for the run audit log and lineage records."""
        return json.dumps(self.versions, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> PinnedConfigVersions | None:
        """Parse the Step Functions payload field; None when the run predates pinning."""
        if not payload:
            return None
        return cls(**payload)
