"""
Google Analytics 4 connector (DL-CONN-06) — marketing performance for Evive.

Registered as its own source, sharing the OAuth credential client with Google Ads.
Report-style like Ads: a metric/dimension/date-range request, with the returned report rows
stored raw.
"""

from __future__ import annotations

from typing import Final

from connector_runtime.adapters.google_ads.google_oauth_credentials import (
    GOOGLE_REQUIRED_CREDENTIAL_KEYS,
)
from connector_runtime.adapters.rest_api.rest_adapter_registration import register_rest_source
from connector_runtime.adapters.rest_api.rest_source_spec import (
    AuthKind,
    EntityShape,
    RestEntitySpec,
    RestSourceSpec,
)
from connector_runtime.source_capabilities import SourceCapability

SOURCE_ID: Final[str] = "google-analytics"


def _report(suffix: str, metrics: tuple[str, ...], dimensions: tuple[str, ...]) -> RestEntitySpec:
    return RestEntitySpec(
        entity_id=f"{SOURCE_ID}-{suffix}",
        path="/v1beta/properties/runReport",
        records_json_path=("rows",),
        shape=EntityShape.REPORT,
        report_metrics=metrics,
        report_dimensions=dimensions,
        natural_key_field="dimensionValues",
        pagination_strategy="offset_limit",
        page_size=10_000,
    )


GOOGLE_ANALYTICS_SPEC: Final[RestSourceSpec] = RestSourceSpec(
    source_id=SOURCE_ID,
    display_name="Google Analytics 4",
    base_url="https://analyticsdata.googleapis.com",
    auth_kind=AuthKind.OAUTH2_REFRESH,
    entities=(
        _report(
            "traffic-acquisition",
            ("sessions", "totalUsers", "newUsers", "engagedSessions"),
            ("date", "sessionSource", "sessionMedium", "sessionCampaignName"),
        ),
        _report(
            "conversion-event",
            ("eventCount", "conversions", "totalRevenue"),
            ("date", "eventName"),
        ),
        _report(
            "landing-page",
            ("sessions", "bounceRate", "averageSessionDuration"),
            ("date", "landingPage"),
        ),
        _report(
            "geo-performance",
            ("sessions", "totalUsers", "conversions"),
            ("date", "country", "region", "city"),
        ),
    ),
    capabilities=frozenset(
        {
            SourceCapability.INCREMENTAL,
            SourceCapability.REPORT_STYLE,
        }
    ),
    default_pagination_strategy="offset_limit",
    default_rate_limit_policy="google-analytics-standard",
    request_timeout_seconds=180.0,
    default_records_json_path=("rows",),
    default_page_size=10_000,
    required_credential_keys=GOOGLE_REQUIRED_CREDENTIAL_KEYS | frozenset({"property_id"}),
    watermark_lower_parameter="startDate",
    watermark_upper_parameter="endDate",
    notes="Evive marketing. Shares the Google OAuth credential client with Google Ads.",
)

register_rest_source(GOOGLE_ANALYTICS_SPEC)
