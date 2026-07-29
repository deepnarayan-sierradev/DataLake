"""
Pin-at-entry resolution of every `latest` pointer (DL-CFG-01).

Called once by the pipeline trigger. Each resolver is a small callable so the service has
no dependency on any registry's internals, and a resolver that cannot answer contributes
nothing rather than failing the whole pin — a capability the run does not consume must not
block the run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from config_propagation.capability import ConfigCapability
from config_propagation.pinned_versions import PinnedConfigVersions
from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

VersionResolver = Callable[[str, str], str | None]
"""(tenant_code, entity_key) -> resolved version, or None when not applicable."""


class ConfigPinningService:
    """Resolves the run's configuration set once, at the run boundary."""

    def __init__(self, resolvers: Mapping[ConfigCapability, VersionResolver]) -> None:
        self._resolvers = dict(resolvers)

    def pin(
        self,
        tenant_code: str,
        entity_key: str,
        *,
        capabilities: tuple[ConfigCapability, ...] | None = None,
    ) -> PinnedConfigVersions:
        """
        Resolve and freeze the configuration versions this run will use.

        A resolver raising is logged and skipped rather than propagated: the alternative is
        that one unrelated capability's outage prevents every pipeline run.
        """
        wanted = capabilities or tuple(self._resolvers)
        versions: dict[str, str] = {}
        for capability in wanted:
            resolver = self._resolvers.get(capability)
            if resolver is None:
                continue
            try:
                version = resolver(tenant_code, entity_key)
            except Exception as exc:
                record_platform_metric(
                    PlatformMetric.CONFIG_VERSION_PIN_FAILURES, 1.0, Capability=capability.value
                )
                _logger.warning(
                    "config_version_pin_resolver_failed",
                    tenant_code=tenant_code,
                    entity_key=entity_key,
                    capability=capability.value,
                    error=str(exc),
                )
                continue
            if version:
                versions[capability.value] = version

        pinned = PinnedConfigVersions(
            versions=versions,
            pinned_at=datetime.now(UTC).isoformat(),
        )
        _logger.info(
            "config_versions_pinned",
            tenant_code=tenant_code,
            entity_key=entity_key,
            pinned_capabilities=sorted(versions),
        )
        return pinned
