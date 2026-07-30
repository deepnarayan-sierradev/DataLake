"""
Configuration capabilities and their reprocessing policies (DL-CFG-10, DL-CFG-12).

Policy as data: a capability declares whether a change is apply-forward or
reprocess-eligible, rather than each caller deciding. The declaration also carries the
minimum reprocessing window, which retention must respect at publish time.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class ConfigCapability(StrEnum):
    """Every configuration surface whose version the runtime pins and records."""

    ENTITY_SELECTION = "entity_selection"
    EXTRACTION_SCHEDULE = "extraction_schedule"
    CREDENTIALS = "credentials"
    SYNC_STRATEGY = "sync_strategy"
    FIELD_MAPPING = "field_mapping"
    QUALITY_POLICY = "quality_policy"
    ENTITY_RESOLUTION = "entity_resolution"
    SURVIVORSHIP = "survivorship"
    RELATIONSHIP_RULES = "relationship_rules"
    SEMANTIC_MODEL = "semantic_model"
    SERVING_STORE = "serving_store"
    WORKFLOW_DEFINITION = "workflow_definition"
    SCOPE_MODEL = "scope_model"


class ReprocessingPolicy(StrEnum):
    """Whether a change may be replayed over history."""

    APPLY_FORWARD = "apply_forward"
    REPROCESS_ELIGIBLE = "reprocess_eligible"


class ReprocessingSourceLayer(StrEnum):
    """The layer a reprocess replays from; determines which retention must cover it."""

    NONE = "none"
    RAW = "raw"
    CURATED = "curated"
    ANALYTICS = "analytics"


@dataclass(frozen=True)
class CapabilityPolicy:
    """The declared reprocessing behaviour of one capability."""

    capability: ConfigCapability
    policy: ReprocessingPolicy
    source_layer: ReprocessingSourceLayer = ReprocessingSourceLayer.NONE
    minimum_reprocessing_window_days: int | None = None
    report_only: bool = False
    restatement_flagged: bool = False
    full_reload_on_change: bool = False

    @property
    def is_reprocess_eligible(self) -> bool:
        return self.policy is ReprocessingPolicy.REPROCESS_ELIGIBLE


CAPABILITY_POLICIES: Final[dict[ConfigCapability, CapabilityPolicy]] = {
    ConfigCapability.ENTITY_SELECTION: CapabilityPolicy(
        ConfigCapability.ENTITY_SELECTION, ReprocessingPolicy.APPLY_FORWARD
    ),
    ConfigCapability.EXTRACTION_SCHEDULE: CapabilityPolicy(
        ConfigCapability.EXTRACTION_SCHEDULE, ReprocessingPolicy.APPLY_FORWARD
    ),
    ConfigCapability.CREDENTIALS: CapabilityPolicy(
        ConfigCapability.CREDENTIALS, ReprocessingPolicy.APPLY_FORWARD
    ),
    ConfigCapability.SYNC_STRATEGY: CapabilityPolicy(
        ConfigCapability.SYNC_STRATEGY, ReprocessingPolicy.APPLY_FORWARD
    ),
    ConfigCapability.FIELD_MAPPING: CapabilityPolicy(
        ConfigCapability.FIELD_MAPPING,
        ReprocessingPolicy.REPROCESS_ELIGIBLE,
        source_layer=ReprocessingSourceLayer.CURATED,
        minimum_reprocessing_window_days=395,
    ),
    ConfigCapability.QUALITY_POLICY: CapabilityPolicy(
        ConfigCapability.QUALITY_POLICY,
        ReprocessingPolicy.REPROCESS_ELIGIBLE,
        source_layer=ReprocessingSourceLayer.CURATED,
        minimum_reprocessing_window_days=395,
        report_only=True,
    ),
    ConfigCapability.ENTITY_RESOLUTION: CapabilityPolicy(
        ConfigCapability.ENTITY_RESOLUTION,
        ReprocessingPolicy.REPROCESS_ELIGIBLE,
        source_layer=ReprocessingSourceLayer.CURATED,
        minimum_reprocessing_window_days=395,
    ),
    ConfigCapability.SURVIVORSHIP: CapabilityPolicy(
        ConfigCapability.SURVIVORSHIP,
        ReprocessingPolicy.REPROCESS_ELIGIBLE,
        source_layer=ReprocessingSourceLayer.CURATED,
        minimum_reprocessing_window_days=395,
    ),
    ConfigCapability.RELATIONSHIP_RULES: CapabilityPolicy(
        ConfigCapability.RELATIONSHIP_RULES,
        ReprocessingPolicy.REPROCESS_ELIGIBLE,
        source_layer=ReprocessingSourceLayer.ANALYTICS,
        minimum_reprocessing_window_days=90,
    ),
    ConfigCapability.SEMANTIC_MODEL: CapabilityPolicy(
        ConfigCapability.SEMANTIC_MODEL,
        ReprocessingPolicy.APPLY_FORWARD,
        restatement_flagged=True,
    ),
    ConfigCapability.SERVING_STORE: CapabilityPolicy(
        ConfigCapability.SERVING_STORE,
        ReprocessingPolicy.APPLY_FORWARD,
        full_reload_on_change=True,
    ),
    ConfigCapability.WORKFLOW_DEFINITION: CapabilityPolicy(
        ConfigCapability.WORKFLOW_DEFINITION, ReprocessingPolicy.APPLY_FORWARD
    ),
    ConfigCapability.SCOPE_MODEL: CapabilityPolicy(
        ConfigCapability.SCOPE_MODEL,
        ReprocessingPolicy.REPROCESS_ELIGIBLE,
        source_layer=ReprocessingSourceLayer.CURATED,
        minimum_reprocessing_window_days=395,
    ),
}


class RetentionReprocessingConflictError(Exception):
    """Raised when a retention policy is shorter than a declared reprocessing window."""


def policy_for(capability: ConfigCapability) -> CapabilityPolicy:
    """The declared policy; a capability with no entry is a programming error."""
    try:
        return CAPABILITY_POLICIES[capability]
    except KeyError as exc:  # pragma: no cover — enum exhaustiveness is tested
        raise KeyError(
            f"Capability {capability!r} has no declared reprocessing policy. Every "
            "capability must declare one (DL-CFG-10)."
        ) from exc


def validate_retention_against_reprocessing(
    capability: ConfigCapability,
    retention_days: int | None,
) -> None:
    """
    Reject a retention policy shorter than the capability's reprocessing window.

    OWASP A05: turns a latent data-loss misconfiguration into an immediate validation
    error at publish rather than a surprise when a reprocess is attempted.
    """
    declared = policy_for(capability)
    window = declared.minimum_reprocessing_window_days
    if window is None or retention_days is None:
        return
    if retention_days < window:
        raise RetentionReprocessingConflictError(
            f"Capability {capability.value!r} is reprocess-eligible with a minimum window of "
            f"{window} days from the {declared.source_layer.value} layer, but the retention "
            f"policy for that layer is {retention_days} days. Reprocessing would find the "
            "input data already expired."
        )
