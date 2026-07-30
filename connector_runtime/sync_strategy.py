"""
Sync strategies — Change Data Capture where available (DL-CONN-13, §3.4).

Three implementations behind one port: `WatermarkPolling` (today's behaviour),
`WebhookIngest`, and `LogBasedCdc`. Configuration selects the strategy, and the watermark
repository stays the resume point for all three, so a webhook gap can always be back-filled
by a polling run.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from contracts.entity_configuration_contract import EntityExtractionConfig, LoadType
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)


class SyncStrategyKind(StrEnum):
    """Registered sync strategy names, matching `EntityExtractionConfig.sync_strategy`."""

    WATERMARK_POLLING = "watermark_polling"
    WEBHOOK_INGEST = "webhook_ingest"
    LOG_BASED_CDC = "log_based_cdc"


class ExtractionMode(StrEnum):
    """What the planned extraction will actually do."""

    FULL = "full"
    INCREMENTAL_WINDOW = "incremental_window"
    WEBHOOK_DRAIN = "webhook_drain"
    CDC_STREAM = "cdc_stream"
    GAP_BACKFILL = "gap_backfill"


@dataclass(frozen=True)
class WatermarkState:
    """The resume point every strategy shares."""

    last_successful_watermark: datetime | None = None
    upper_watermark: datetime | None = None


@dataclass(frozen=True)
class ExtractionPlan:
    """What one run should extract, and from where."""

    mode: ExtractionMode
    watermark_lower: datetime | None = None
    watermark_upper: datetime | None = None
    cdc_start_position: str | None = None
    drain_queue_url: str | None = None
    reason: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)

    @property
    def window_days(self) -> float | None:
        if self.watermark_lower is None or self.watermark_upper is None:
            return None
        return (self.watermark_upper - self.watermark_lower).total_seconds() / 86_400


class SyncStrategy(abc.ABC):
    """Port turning (config, watermark) into an extraction plan."""

    kind: SyncStrategyKind

    @abc.abstractmethod
    def plan(
        self, config: EntityExtractionConfig, watermark: WatermarkState | None
    ) -> ExtractionPlan:
        raise NotImplementedError

    @staticmethod
    def _window(
        config: EntityExtractionConfig, watermark: WatermarkState | None, now: datetime
    ) -> tuple[datetime, datetime]:
        overlap = timedelta(hours=config.watermark_overlap_hours)
        if watermark is None or watermark.last_successful_watermark is None:
            lower = now - timedelta(days=config.extraction_window_days)
        else:
            lower = watermark.last_successful_watermark - overlap
        return lower, now


class WatermarkPollingSyncStrategy(SyncStrategy):
    """Today's behaviour: bounded incremental window, or a full load when declared."""

    kind = SyncStrategyKind.WATERMARK_POLLING

    def plan(
        self, config: EntityExtractionConfig, watermark: WatermarkState | None
    ) -> ExtractionPlan:
        if config.load_type is LoadType.FULL:
            return ExtractionPlan(mode=ExtractionMode.FULL, reason="load_type is full")
        now = datetime.now(UTC)
        lower, upper = self._window(config, watermark, now)
        return ExtractionPlan(
            mode=ExtractionMode.INCREMENTAL_WINDOW,
            watermark_lower=lower,
            watermark_upper=upper,
            reason="watermark polling window",
        )


DEFAULT_WEBHOOK_GAP_TOLERANCE_HOURS: Final[int] = 6


class WebhookIngestSyncStrategy(SyncStrategy):
    """
    Drains the webhook queue, falling back to a polling back-fill when a gap is detected.

    The fallback is the point of keeping the watermark authoritative: a provider that drops
    or delays a webhook must not produce a silent data gap.
    """

    kind = SyncStrategyKind.WEBHOOK_INGEST

    def __init__(
        self,
        drain_queue_url: str | None = None,
        gap_tolerance_hours: int = DEFAULT_WEBHOOK_GAP_TOLERANCE_HOURS,
    ) -> None:
        self._drain_queue_url = drain_queue_url
        self._gap_tolerance = timedelta(hours=gap_tolerance_hours)

    def plan(
        self, config: EntityExtractionConfig, watermark: WatermarkState | None
    ) -> ExtractionPlan:
        now = datetime.now(UTC)
        last = watermark.last_successful_watermark if watermark else None
        if last is None or now - last > self._gap_tolerance:
            lower, upper = self._window(config, watermark, now)
            _logger.warning(
                "webhook_ingest_gap_detected_backfilling",
                entity_id=config.entity_id,
                connection_id=config.effective_connection_id,
                last_successful_watermark=last.isoformat() if last else None,
            )
            return ExtractionPlan(
                mode=ExtractionMode.GAP_BACKFILL,
                watermark_lower=lower,
                watermark_upper=upper,
                reason="webhook stream fell behind the gap tolerance",
            )
        return ExtractionPlan(
            mode=ExtractionMode.WEBHOOK_DRAIN,
            watermark_lower=last,
            watermark_upper=now,
            drain_queue_url=self._drain_queue_url,
            reason="webhook queue drain",
        )


class LogBasedCdcSyncStrategy(SyncStrategy):
    """
    MySQL binlog CDC via DMS.

    The watermark still bounds the run so an absent or reset CDC position degrades to a
    polling window rather than silently re-reading from the start of the log.
    """

    kind = SyncStrategyKind.LOG_BASED_CDC

    def __init__(self, cdc_position: str | None = None) -> None:
        self._cdc_position = cdc_position

    def plan(
        self, config: EntityExtractionConfig, watermark: WatermarkState | None
    ) -> ExtractionPlan:
        if not self._cdc_position:
            now = datetime.now(UTC)
            lower, upper = self._window(config, watermark, now)
            return ExtractionPlan(
                mode=ExtractionMode.GAP_BACKFILL,
                watermark_lower=lower,
                watermark_upper=upper,
                reason="no CDC position available; falling back to a polling window",
            )
        return ExtractionPlan(
            mode=ExtractionMode.CDC_STREAM,
            cdc_start_position=self._cdc_position,
            reason="log-based CDC from the recorded position",
        )


class SyncStrategyRegistry:
    """Named strategy factories, resolved from the entity config."""

    def __init__(self) -> None:
        self._factories: dict[str, Any] = {}

    def register(self, name: str, factory: Any) -> None:
        if name in self._factories:
            raise ValueError(f"Sync strategy {name!r} is already registered.")
        self._factories[name] = factory

    def registered_names(self) -> list[str]:
        return sorted(self._factories)

    def resolve(self, name: str, **kwargs: Any) -> SyncStrategy:
        factory = self._factories.get(name)
        if factory is None:
            raise KeyError(
                f"No sync strategy registered under {name!r}. "
                f"Registered: {self.registered_names()}."
            )
        strategy: SyncStrategy = factory(**kwargs)
        return strategy

    def reset(self) -> None:
        """Testing only."""
        self._factories.clear()


sync_strategy_registry: Final[SyncStrategyRegistry] = SyncStrategyRegistry()
sync_strategy_registry.register(
    SyncStrategyKind.WATERMARK_POLLING.value, lambda **_: WatermarkPollingSyncStrategy()
)
sync_strategy_registry.register(
    SyncStrategyKind.WEBHOOK_INGEST.value,
    lambda drain_queue_url=None, **_: WebhookIngestSyncStrategy(drain_queue_url=drain_queue_url),
)
sync_strategy_registry.register(
    SyncStrategyKind.LOG_BASED_CDC.value,
    lambda cdc_position=None, **_: LogBasedCdcSyncStrategy(cdc_position=cdc_position),
)


def plan_for_config(
    config: EntityExtractionConfig,
    watermark: WatermarkState | None,
    **strategy_kwargs: Any,
) -> ExtractionPlan:
    """Resolve the configured strategy and plan one run."""
    strategy = sync_strategy_registry.resolve(config.sync_strategy, **strategy_kwargs)
    return strategy.plan(config, watermark)
