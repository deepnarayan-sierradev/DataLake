"""
Google Ads connector (DL-CONN-06) — marketing performance for Evive.

Report-style, not row-style: the query builder emits a metric/dimension/date-range request
and the raw layer stores the returned report rows. Shares its OAuth credential client with
Google Analytics 4.
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

SOURCE_ID: Final[str] = "google-ads"


def _report(suffix: str, metrics: tuple[str, ...], dimensions: tuple[str, ...]) -> RestEntitySpec:
    return RestEntitySpec(
        entity_id=f"{SOURCE_ID}-{suffix}",
        path="/v17/customers/searchStream",
        records_json_path=("results",),
        shape=EntityShape.REPORT,
        report_metrics=metrics,
        report_dimensions=dimensions,
        natural_key_field="resourceName",
        pagination_strategy="cursor",
        page_size=1_000,
    )


GOOGLE_ADS_SPEC: Final[RestSourceSpec] = RestSourceSpec(
    source_id=SOURCE_ID,
    display_name="Google Ads",
    base_url="https://googleads.googleapis.com",
    auth_kind=AuthKind.OAUTH2_REFRESH,
    entities=(
        _report(
            "campaign-performance",
            ("metrics.impressions", "metrics.clicks", "metrics.cost_micros", "metrics.conversions"),
            ("campaign.id", "campaign.name", "segments.date"),
        ),
        _report(
            "ad-group-performance",
            ("metrics.impressions", "metrics.clicks", "metrics.cost_micros", "metrics.conversions"),
            ("ad_group.id", "ad_group.name", "campaign.id", "segments.date"),
        ),
        _report(
            "keyword-performance",
            ("metrics.impressions", "metrics.clicks", "metrics.cost_micros"),
            ("ad_group_criterion.criterion_id", "ad_group.id", "segments.date"),
        ),
        _report(
            "conversion-action",
            ("metrics.all_conversions", "metrics.all_conversions_value"),
            ("conversion_action.id", "conversion_action.name", "segments.date"),
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
    default_rate_limit_policy="google-ads-standard",
    # Inherited by a config-declared entity (DL-CONN-21); must match what this
    # source's own entities use, or a console-added entity silently reads zero rows.
    default_records_json_path=("results",),
    default_page_size=1_000,
    required_credential_keys=GOOGLE_REQUIRED_CREDENTIAL_KEYS | frozenset({"developer_token"}),
    watermark_lower_parameter="start_date",
    watermark_upper_parameter="end_date",
    notes="Evive marketing. Report-style API; date range is the incremental dimension.",
)

register_rest_source(GOOGLE_ADS_SPEC)
