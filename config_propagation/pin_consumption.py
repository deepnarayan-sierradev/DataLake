"""
Stage-side consumption of the run's pinned configuration (DL-CFG-01, DL-CFG-08).

The pin is taken once at the run boundary by the pipeline trigger. A stage's obligation is the
other half of the contract:

1. **Assert** the version it actually observed matches the pinned one, so a publish that landed
   mid-run surfaces as `ConfigVersionMismatchWithinRun` rather than as two stages of one run
   quietly disagreeing about a definition.
2. **Record** the version as effective, attributed to the first run that consumed it, which is
   what turns "published" into "in effect" for the console.

Both are one call, because a stage that does the first and forgets the second produces a run
nobody can explain afterwards.
"""

from __future__ import annotations

from typing import Final

from config_propagation.capability import ConfigCapability
from config_propagation.effective_config_repository import EffectiveConfigRepository
from config_propagation.pinned_versions import (
    ConfigVersionMismatchError,
    PinnedConfigVersions,
)
from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger
from tenancy.scope_contract import IMPLICIT_SCOPE_UNIT_ID

_logger = get_platform_logger(__name__)

# A mismatch is not fatal by default: the stage has already read the configuration, and failing
# the run would turn a consistency observation into an outage. It is recorded, alarmed, and the
# run continues on what it read — which is the pinned-consistency exit-gate metric.
DEFAULT_FAIL_ON_MISMATCH: Final[bool] = False


def consume_pinned_config(
    *,
    pinned: PinnedConfigVersions | None,
    capability: ConfigCapability,
    observed_version: str,
    tenant_code: str,
    entity_key: str,
    run_id: str,
    environment: str,
    region_name: str,
    scope_id: str = IMPLICIT_SCOPE_UNIT_ID,
    fail_on_mismatch: bool = DEFAULT_FAIL_ON_MISMATCH,
) -> bool:
    """
    Reconcile the observed configuration version against the run's pin, and record it as effective.

    Returns True when the observed version matched the pin (or no pin was carried). Never raises
    for a recording failure — telemetry must not decide whether a pipeline run succeeds — but will
    raise `ConfigVersionMismatchError` when `fail_on_mismatch` is set, which is the correct choice
    for a capability whose mid-run change would produce incorrect output rather than merely
    inconsistent provenance.
    """
    matched = True
    if pinned is not None:
        try:
            pinned.assert_matches(capability, observed_version)
        except ConfigVersionMismatchError:
            matched = False
            record_platform_metric(
                PlatformMetric.CONFIG_VERSION_MISMATCH_WITHIN_RUN,
                1.0,
                Capability=capability.value,
            )
            _logger.error(
                "config_version_mismatch_within_run",
                tenant_code=tenant_code,
                entity_key=entity_key,
                run_id=run_id,
                capability=capability.value,
                observed_version=observed_version,
                pinned_version=pinned.get(capability),
            )
            if fail_on_mismatch:
                raise

    try:
        EffectiveConfigRepository(
            environment=environment, region_name=region_name
        ).record_consumption(
            tenant_code,
            capability,
            entity_key,
            observed_version,
            run_id,
            scope_id=scope_id,
        )
    except Exception as exc:
        _logger.warning(
            "effective_config_record_failed",
            tenant_code=tenant_code,
            capability=capability.value,
            entity_key=entity_key,
            error=str(exc),
        )
    return matched
