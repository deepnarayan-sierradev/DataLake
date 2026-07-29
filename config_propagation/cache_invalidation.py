"""
Declared cache-invalidation basis per config registry (DL-CFG-04).

Every registry with an in-process cache states its contract here, and the contract is
tested. A dead invalidation API is worse than none, because it implies a guarantee that
does not exist — so a registry declared `SIGNAL_DRIVEN` must expose a real invalidation
method that a real publish path calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from config_propagation.capability import ConfigCapability


class InvalidationBasis(StrEnum):
    """How a cached configuration stops being served."""

    VERSION_KEYED = "version_keyed"
    TTL_BOUNDED = "ttl_bounded"
    SIGNAL_DRIVEN = "signal_driven"
    UNCACHED = "uncached"


@dataclass(frozen=True)
class CacheContract:
    """The declared invalidation contract of one registry."""

    capability: ConfigCapability
    registry: str
    basis: InvalidationBasis
    ttl_seconds: int | None = None
    invalidation_method: str | None = None
    signal_source: str | None = None

    def __post_init__(self) -> None:
        if self.basis is InvalidationBasis.TTL_BOUNDED and not self.ttl_seconds:
            raise ValueError(
                f"{self.registry}: a TTL-bounded cache must declare its bound (DL-CFG-04)."
            )
        if self.basis is InvalidationBasis.SIGNAL_DRIVEN and not (
            self.invalidation_method and self.signal_source
        ):
            raise ValueError(
                f"{self.registry}: a signal-driven cache must name both its invalidation "
                "method and the real signal that calls it — otherwise the API is dead."
            )


# The declared contracts. Adding a cache without adding an entry fails the contract test.
CACHE_CONTRACTS: Final[tuple[CacheContract, ...]] = (
    CacheContract(
        capability=ConfigCapability.ENTITY_SELECTION,
        registry="connector_runtime.configuration_repository.configuration_repository.ConfigurationRepositoryClient",
        basis=InvalidationBasis.UNCACHED,
    ),
    CacheContract(
        capability=ConfigCapability.ENTITY_RESOLUTION,
        registry="entity_resolution.resolution_config.resolution_config_registry.ResolutionConfigRegistry",
        basis=InvalidationBasis.SIGNAL_DRIVEN,
        invalidation_method="invalidate_entity_type",
        signal_source="ResolutionConfigRegistry.publish",
    ),
    CacheContract(
        capability=ConfigCapability.SURVIVORSHIP,
        registry="entity_resolution.resolution_config.resolution_config_registry.ResolutionConfigRegistry",
        basis=InvalidationBasis.SIGNAL_DRIVEN,
        invalidation_method="invalidate_entity_type",
        signal_source="ResolutionConfigRegistry.publish",
    ),
    CacheContract(
        capability=ConfigCapability.RELATIONSHIP_RULES,
        registry="knowledge.relationship_rules_registry.RelationshipRulesRegistry",
        basis=InvalidationBasis.VERSION_KEYED,
    ),
    CacheContract(
        capability=ConfigCapability.CREDENTIALS,
        registry="connector_runtime.credential_client.SecretsManagerCredentialClient",
        basis=InvalidationBasis.TTL_BOUNDED,
        ttl_seconds=300,
    ),
    CacheContract(
        capability=ConfigCapability.SEMANTIC_MODEL,
        registry="semantic.semantic_model_repository.SemanticModelRepository",
        basis=InvalidationBasis.VERSION_KEYED,
    ),
    CacheContract(
        capability=ConfigCapability.FIELD_MAPPING,
        registry="transformation.field_mapping.field_mapping_registry.FieldMappingRegistryClient",
        basis=InvalidationBasis.VERSION_KEYED,
    ),
    CacheContract(
        capability=ConfigCapability.SERVING_STORE,
        registry="serving_store.serving_store_config_repository.ServingStoreConfigRepository",
        basis=InvalidationBasis.UNCACHED,
    ),
    CacheContract(
        capability=ConfigCapability.WORKFLOW_DEFINITION,
        registry="workflow_automation.definition_repository.WorkflowDefinitionRepository",
        basis=InvalidationBasis.VERSION_KEYED,
    ),
    CacheContract(
        capability=ConfigCapability.SCOPE_MODEL,
        registry="tenancy.scope_unit_repository.ScopeUnitRepository",
        basis=InvalidationBasis.UNCACHED,
    ),
    CacheContract(
        capability=ConfigCapability.EXTRACTION_SCHEDULE,
        registry="orchestration.event_bridge.extraction_schedule_client.ExtractionScheduleClient",
        basis=InvalidationBasis.UNCACHED,
    ),
    CacheContract(
        capability=ConfigCapability.SYNC_STRATEGY,
        registry="connector_runtime.sync_strategy.SyncStrategyRegistry",
        basis=InvalidationBasis.UNCACHED,
    ),
    CacheContract(
        capability=ConfigCapability.QUALITY_POLICY,
        registry="data_quality.quality_policy_repository.QualityPolicyRepository",
        basis=InvalidationBasis.VERSION_KEYED,
    ),
)


def contracts_for(capability: ConfigCapability) -> tuple[CacheContract, ...]:
    """Every declared contract touching one capability."""
    return tuple(c for c in CACHE_CONTRACTS if c.capability is capability)
