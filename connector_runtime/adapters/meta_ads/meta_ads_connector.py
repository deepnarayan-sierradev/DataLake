"""
Meta Ads connector (DL-CONN-07) — Marketing Insights API for Evive.

Same report-style shape as the Google sources, plus async job polling for large date
ranges: Insights returns a report run id, which is polled with exponential backoff rather
than holding a Lambda open.

Meta's budget is per app rather than per connection, so the rate-limit policy is registered
as shared across a tenant's connections (DL-SCOPE-07's shared-quota case).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from connector_runtime.adapters.rest_api.rest_adapter_registration import register_rest_source
from connector_runtime.adapters.rest_api.rest_http_session import (
    RestHttpSession,
    RestSourceRequestError,
    RestSourceTransientError,
)
from connector_runtime.adapters.rest_api.rest_source_spec import (
    AuthKind,
    EntityShape,
    RestEntitySpec,
    RestSourceSpec,
)
from connector_runtime.source_capabilities import SourceCapability
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

SOURCE_ID: Final[str] = "meta-ads"

# Date span above which the connector submits an async report run instead of a sync read.
ASYNC_JOB_THRESHOLD_DAYS: Final[int] = 30

# Poll ceiling — a report that has not completed by here is reported, not waited on.
MAX_JOB_POLLS: Final[int] = 8


def _report(suffix: str, metrics: tuple[str, ...], dimensions: tuple[str, ...]) -> RestEntitySpec:
    return RestEntitySpec(
        entity_id=f"{SOURCE_ID}-{suffix}",
        path="/v20.0/act/insights",
        records_json_path=("data",),
        shape=EntityShape.REPORT,
        report_metrics=metrics,
        report_dimensions=dimensions,
        natural_key_field="id",
        pagination_strategy="cursor",
        page_size=500,
    )


META_ADS_SPEC: Final[RestSourceSpec] = RestSourceSpec(
    source_id=SOURCE_ID,
    display_name="Meta Ads",
    base_url="https://graph.facebook.com",
    auth_kind=AuthKind.BEARER_TOKEN,
    entities=(
        _report(
            "campaign-insights",
            ("impressions", "clicks", "spend", "actions", "action_values"),
            ("campaign_id", "campaign_name", "date_start"),
        ),
        _report(
            "adset-insights",
            ("impressions", "clicks", "spend", "actions"),
            ("adset_id", "adset_name", "campaign_id", "date_start"),
        ),
        _report(
            "ad-insights",
            ("impressions", "clicks", "spend", "actions"),
            ("ad_id", "ad_name", "adset_id", "date_start"),
        ),
    ),
    capabilities=frozenset(
        {
            SourceCapability.INCREMENTAL,
            SourceCapability.REPORT_STYLE,
            SourceCapability.ASYNC_JOB,
        }
    ),
    default_pagination_strategy="cursor",
    default_rate_limit_policy="meta-ads-standard",
    required_credential_keys=frozenset({"access_token", "ad_account_id"}),
    watermark_lower_parameter="time_range_since",
    watermark_upper_parameter="time_range_until",
    notes="Evive marketing. App-level shared quota; async report runs for long date ranges.",
)

register_rest_source(META_ADS_SPEC)


class AsyncReportStatus(StrEnum):
    """Meta's async insights job states."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass(frozen=True)
class AsyncReportRun:
    """A submitted async insights report."""

    report_run_id: str
    status: AsyncReportStatus
    percent_complete: int = 0


class AsyncReportNotReadyError(RestSourceTransientError):
    """Raised when a report run is still executing after the poll ceiling."""


def should_use_async_job(window_days: float | None) -> bool:
    """Long date ranges go async; short ones read synchronously and finish in one call."""
    return window_days is not None and window_days > ASYNC_JOB_THRESHOLD_DAYS


def submit_async_report(
    session: RestHttpSession, account_id: str, parameters: dict[str, Any]
) -> AsyncReportRun:
    """Submit an insights report run and return its identifier."""
    response = session.post(
        f"/v20.0/act_{account_id}/insights", payload={**parameters, "async": True}
    )
    body = response.body if isinstance(response.body, dict) else {}
    report_run_id = str(body.get("report_run_id") or "")
    if not report_run_id:
        raise RestSourceRequestError(
            "Meta Ads accepted the async report request but returned no report_run_id."
        )
    return AsyncReportRun(report_run_id=report_run_id, status=AsyncReportStatus.PENDING)


def poll_async_report(session: RestHttpSession, run: AsyncReportRun) -> AsyncReportRun:
    """One poll of a submitted report run; the caller owns the backoff schedule."""
    response = session.get(f"/v20.0/{run.report_run_id}")
    body = response.body if isinstance(response.body, dict) else {}
    raw_status = str(body.get("async_status", "")).strip().lower()
    status = {
        "job completed": AsyncReportStatus.COMPLETE,
        "job failed": AsyncReportStatus.FAILED,
        "job running": AsyncReportStatus.RUNNING,
        "job started": AsyncReportStatus.RUNNING,
        "job not started": AsyncReportStatus.PENDING,
    }.get(raw_status, AsyncReportStatus.RUNNING)
    return AsyncReportRun(
        report_run_id=run.report_run_id,
        status=status,
        percent_complete=int(body.get("async_percent_completion") or 0),
    )


def backoff_seconds_for_poll(attempt: int) -> float:
    """Exponential backoff, capped, so a slow report does not hold a Lambda open."""
    if attempt < 0:
        raise ValueError("attempt must be non-negative.")
    return min(2.0 ** min(attempt, MAX_JOB_POLLS), 60.0)
