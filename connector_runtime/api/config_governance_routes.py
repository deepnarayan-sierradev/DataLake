"""
Configuration-governance and semantic-governance control-plane routes (DL-11, DL-03, DL-10).

These are the read/act endpoints the enterprise-platform console consumes to answer questions
this system alone can answer — *is my published change live yet*, *which run first consumed it*,
*what was restated*, *what does this metric derive from* — plus the audited rollback, reprocess,
and export operations.

Kept in its own module so the route table stays readable and `_route`'s complexity gate is not
breached by a second batch of routes.

Ownership: nothing here creates or administers a tenant, user, role, or permission. Every route
authorizes against the verified tenant claim the caller already holds; identity is consumed, not
owned (see `requirements/CROSS_REPO_INTERFACE_CONTRACT.md`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from config_propagation.capability import (
    CAPABILITY_POLICIES,
    ConfigCapability,
    validate_retention_against_reprocessing,
)
from contracts.identifier_policy import ENTITY_TYPE_PATTERN

_KNOWN_CAPABILITIES: Final[frozenset[str]] = frozenset(c.value for c in ConfigCapability)


class ConfigRouteError(Exception):
    """Raised when a config-governance path parameter is malformed."""


def parse_capability(raw: str) -> ConfigCapability:
    """Resolve a path segment to a declared capability, failing closed on anything else."""
    if raw not in _KNOWN_CAPABILITIES:
        raise ConfigRouteError(
            f"capability {raw!r} is not a declared configuration capability. "
            f"Known: {sorted(_KNOWN_CAPABILITIES)}."
        )
    return ConfigCapability(raw)


def parse_entity_key(raw: str) -> str:
    """Entity keys share the entity-type charset; underscores are permitted (`ar_invoice`)."""
    if not ENTITY_TYPE_PATTERN.match(raw):
        raise ConfigRouteError(f"entity key {raw!r} is not a valid identifier.")
    return raw


@dataclass(frozen=True)
class ReprocessRequestParams:
    """Validated parameters for a bounded historical replay (DL-CFG-11)."""

    capability: ConfigCapability
    entity_key: str
    window_start: date
    window_end: date
    reason: str
    pinned_config_version: str

    def __post_init__(self) -> None:
        if self.window_end < self.window_start:
            raise ConfigRouteError("window_end must not precede window_start.")
        if not self.reason.strip():
            raise ConfigRouteError(
                "A reprocess must state its reason — the reason is what makes the recomputed "
                "output explainable afterwards."
            )
        if not self.pinned_config_version.strip():
            raise ConfigRouteError(
                "A reprocess must name the configuration version to pin for the whole job "
                "(DL-CFG-11); an unpinned replay produces output nobody can attribute."
            )
        declared = CAPABILITY_POLICIES.get(self.capability)
        if declared is None or not declared.is_reprocess_eligible:
            raise ConfigRouteError(
                f"capability {self.capability.value!r} is apply-forward, so history cannot be "
                "recomputed under a new configuration (DL-CFG-10)."
            )

    @property
    def window_days(self) -> int:
        return (self.window_end - self.window_start).days + 1

    def guard_retention(self, retention_days: int | None) -> None:
        """A window longer than retention would find its input already expired (DL-CFG-12)."""
        validate_retention_against_reprocessing(self.capability, retention_days)


@dataclass(frozen=True)
class RollbackRequestParams:
    """Validated parameters for an audited pointer rollback (DL-CFG-09)."""

    capability: ConfigCapability
    entity_key: str
    target_version: str
    requested_by: str
    approved_by: str

    def __post_init__(self) -> None:
        if not self.target_version.strip():
            raise ConfigRouteError("A rollback must name the target version.")
        if not self.requested_by.strip():
            raise ConfigRouteError("A rollback must name its requester.")
        if not self.approved_by.strip() or self.approved_by == self.requested_by:
            raise ConfigRouteError("A rollback requires an approver distinct from the requester.")


@dataclass(frozen=True)
class ConfigRoute:
    """One config-governance route; matched positionally like the intelligence routes."""

    method: str
    length: int
    resource: str
    tail: str | None
    handler: Callable[[dict[str, Any], list[str]], dict[str, Any]]

    def matches(self, method: str, segments: list[str]) -> bool:
        return (
            method == self.method
            and len(segments) == self.length
            and segments[0] == "tenants"
            and segments[2] == self.resource
            and (self.tail is None or segments[-1] == self.tail)
        )


def build_config_routes(
    *,
    effective_config: Callable[[dict[str, Any], str], dict[str, Any]],
    effective_config_one: Callable[[dict[str, Any], str, str, str], dict[str, Any]],
    rollback: Callable[[dict[str, Any], str, str, str], dict[str, Any]],
    reprocess: Callable[[dict[str, Any], str, str, str], dict[str, Any]],
    restatements: Callable[[dict[str, Any], str], dict[str, Any]],
    metric_lineage: Callable[[dict[str, Any], str, str], dict[str, Any]],
    model_versions: Callable[[dict[str, Any], str], dict[str, Any]],
    active_model: Callable[[dict[str, Any], str], dict[str, Any]],
) -> tuple[ConfigRoute, ...]:
    """
    Build the route table from injected handlers.

    Injected rather than imported so this module carries no AWS dependency and the route shapes
    can be asserted without standing up a control plane.
    """
    return (
        ConfigRoute("GET", 4, "config", "effective", lambda e, s: effective_config(e, s[1])),
        ConfigRoute(
            "GET",
            6,
            "config",
            None,
            lambda e, s: effective_config_one(e, s[1], s[4], s[5]),
        ),
        ConfigRoute(
            "POST",
            6,
            "config",
            "rollback",
            lambda e, s: rollback(e, s[1], s[3], s[4]),
        ),
        ConfigRoute(
            "POST",
            6,
            "config",
            "reprocess",
            lambda e, s: reprocess(e, s[1], s[3], s[4]),
        ),
        ConfigRoute("GET", 4, "config", "restatements", lambda e, s: restatements(e, s[1])),
        ConfigRoute("GET", 6, "semantic", "lineage", lambda e, s: metric_lineage(e, s[1], s[4])),
        ConfigRoute("GET", 5, "semantic", "versions", lambda e, s: model_versions(e, s[1])),
        ConfigRoute("GET", 4, "semantic", "model", lambda e, s: active_model(e, s[1])),
    )


def match_config_route(
    routes: tuple[ConfigRoute, ...],
    event: dict[str, Any],
    method: str,
    segments: list[str],
) -> dict[str, Any] | None:
    """Return the handler's response, or None when no config route matches."""
    for route in routes:
        if route.matches(method, segments):
            return route.handler(event, segments)
    return None
