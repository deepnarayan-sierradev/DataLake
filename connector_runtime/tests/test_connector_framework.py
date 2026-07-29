"""Rate limiting, pagination, sync strategies, capabilities, webhooks (DL-CONN-11 … 17)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from connector_runtime.pagination import (
    CursorPagination,
    KeysetPagination,
    LinkHeaderPagination,
    OffsetLimitPagination,
    PaginationExhaustionError,
    PaginationKind,
    SourcePage,
    SourceRequest,
    pagination_strategy_registry,
    parse_next_link,
    stream_records,
)
from connector_runtime.rate_limiting import (
    FixedWindowRateLimitPolicy,
    RateLimitPolicySpec,
    RateLimitStrategy,
    ResumeAfterBackoffRequired,
    RetryAfterRateLimitPolicy,
    SustainedThrottleError,
    TokenBucketRateLimitPolicy,
    parse_rate_limit_headers,
    rate_limit_policy_registry,
    telemetry_for,
)
from connector_runtime.source_capabilities import (
    OutboundHostNotAllowedError,
    SourceCapability,
    SourceCapabilityDeclaration,
    SourceCapabilityUnavailableError,
    enforce_allowed_host,
    source_capability_registry,
)
from connector_runtime.sync_strategy import (
    ExtractionMode,
    LogBasedCdcSyncStrategy,
    SyncStrategyKind,
    WatermarkPollingSyncStrategy,
    WatermarkState,
    WebhookIngestSyncStrategy,
    plan_for_config,
    sync_strategy_registry,
)
from connector_runtime.webhook_signature import (
    SignatureAlgorithm,
    SignatureSpec,
    WebhookSignatureError,
    compute_signature,
    sha256_hex,
    spec_for_source,
    verify_webhook_signature,
)
from contracts.entity_configuration_contract import EntityExtractionConfig, LoadType


def _config(**overrides) -> EntityExtractionConfig:
    base = {
        "source_id": "hubspot",
        "entity_id": "hubspot-company",
        "config_version": "1.0.0",
        "tenant_code": "evive",
        "target_raw_s3_prefix": "s3://raw/hubspot/company/",
        "schema_snapshot_s3_prefix": "s3://snap/hubspot/company/",
        "watermark_field": "updatedAt",
        "extraction_window_days": 7,
        "watermark_overlap_hours": 2,
    }
    return EntityExtractionConfig(**{**base, **overrides})


class TestRateLimitHeaderParsing:
    def test_retry_after_marks_a_throttle(self):
        observation = parse_rate_limit_headers({"Retry-After": "30"})
        assert observation.throttled is True
        assert observation.retry_after_seconds == 30.0

    def test_zero_remaining_marks_a_throttle(self):
        assert parse_rate_limit_headers({"X-RateLimit-Remaining": "0"}).throttled is True

    def test_status_429_marks_a_throttle(self):
        assert parse_rate_limit_headers({"x-edl-response-status": "429"}).throttled is True

    def test_healthy_headers_are_not_a_throttle(self):
        observation = parse_rate_limit_headers(
            {"X-RateLimit-Remaining": "95", "X-RateLimit-Limit": "100"}
        )
        assert observation.throttled is False
        assert observation.remaining_requests == 95
        assert observation.limit_requests == 100

    def test_non_numeric_headers_are_ignored(self):
        assert parse_rate_limit_headers({"Retry-After": "soon"}).retry_after_seconds is None

    def test_parsing_is_case_insensitive(self):
        assert parse_rate_limit_headers({"retry-after": "5"}).retry_after_seconds == 5.0


class TestFixedWindowPolicy:
    def test_requests_within_the_window_do_not_wait(self):
        slept: list[float] = []
        clock = [0.0]
        policy = FixedWindowRateLimitPolicy(
            connection_id="hubspot-grasons",
            max_requests=2,
            window_seconds=1.0,
            sleep=slept.append,
            monotonic=lambda: clock[0],
        )
        policy.acquire()
        policy.acquire()
        assert slept == []

    def test_exceeding_the_window_waits(self):
        slept: list[float] = []
        clock = [0.0]
        policy = FixedWindowRateLimitPolicy(
            connection_id="hubspot-grasons",
            max_requests=1,
            window_seconds=1.0,
            sleep=slept.append,
            monotonic=lambda: clock[0],
        )
        policy.acquire()
        policy.acquire()
        assert len(slept) == 1
        assert policy.total_backoff_ms > 0

    def test_window_resets_after_it_elapses(self):
        slept: list[float] = []
        clock = [0.0]
        policy = FixedWindowRateLimitPolicy(
            connection_id="c-one",
            max_requests=1,
            window_seconds=1.0,
            sleep=slept.append,
            monotonic=lambda: clock[0],
        )
        policy.acquire()
        clock[0] = 2.0
        policy.acquire()
        assert slept == []

    def test_zero_max_requests_is_rejected(self):
        with pytest.raises(ValueError, match="at least 1"):
            FixedWindowRateLimitPolicy(connection_id="c-one", max_requests=0)


class TestTokenBucketPolicy:
    def test_burst_is_absorbed_by_capacity(self):
        slept: list[float] = []
        clock = [0.0]
        policy = TokenBucketRateLimitPolicy(
            connection_id="c-one",
            capacity=3,
            refill_per_second=1.0,
            sleep=slept.append,
            monotonic=lambda: clock[0],
        )
        for _ in range(3):
            policy.acquire()
        assert slept == []

    def test_exhausted_bucket_waits_for_a_refill(self):
        slept: list[float] = []
        clock = [0.0]
        policy = TokenBucketRateLimitPolicy(
            connection_id="c-one",
            capacity=1,
            refill_per_second=1.0,
            sleep=slept.append,
            monotonic=lambda: clock[0],
        )
        policy.acquire()
        policy.acquire()
        assert len(slept) == 1

    def test_invalid_parameters_are_rejected(self):
        with pytest.raises(ValueError, match="capacity must be at least 1"):
            TokenBucketRateLimitPolicy(connection_id="c-one", capacity=0, refill_per_second=1)
        with pytest.raises(ValueError, match="refill_per_second must be positive"):
            TokenBucketRateLimitPolicy(connection_id="c-one", capacity=1, refill_per_second=0)


class TestRetryAfterPolicy:
    def test_honours_the_provider_retry_after(self):
        slept: list[float] = []
        policy = RetryAfterRateLimitPolicy(connection_id="c-one", sleep=slept.append)
        policy.acquire()
        assert slept == []
        policy.observe({"Retry-After": "4", "x-edl-response-status": "429"})
        policy.acquire()
        assert len(slept) == 1
        assert 2.0 <= slept[0] <= 4.0

    def test_backs_off_exponentially_without_a_header(self):
        slept: list[float] = []
        policy = RetryAfterRateLimitPolicy(
            connection_id="c-one", base_backoff_seconds=1.0, sleep=slept.append
        )
        policy.observe({"x-edl-response-status": "429"})
        policy.acquire()
        first = slept[-1]
        policy.observe({"x-edl-response-status": "429"})
        policy.acquire()
        assert slept[-1] > first

    def test_success_resets_the_consecutive_counter(self):
        policy = RetryAfterRateLimitPolicy(connection_id="c-one", sleep=lambda s: None)
        policy.observe({"x-edl-response-status": "429"})
        assert policy.consecutive_throttles == 1
        policy.observe({"x-edl-response-status": "200"})
        assert policy.consecutive_throttles == 0

    def test_sustained_throttling_asks_the_caller_to_checkpoint(self):
        policy = RetryAfterRateLimitPolicy(
            connection_id="c-one", sustained_throttle_limit=2, sleep=lambda s: None
        )
        policy.observe({"x-edl-response-status": "429"})
        policy.observe({"x-edl-response-status": "429"})
        with pytest.raises(SustainedThrottleError, match="Checkpoint the extraction"):
            policy.acquire()

    def test_a_long_backoff_asks_to_be_resumed_rather_than_sleeping(self):
        # L14: sleeping inside a Lambda is billed wall-clock inside a 900s budget, so a wait the
        # policy cannot absorb becomes a checkpoint the state machine's Wait state covers for free.
        slept: list[float] = []
        policy = RetryAfterRateLimitPolicy(connection_id="c-one", sleep=slept.append)
        policy.observe({"Retry-After": "99999", "x-edl-response-status": "429"})
        with pytest.raises(ResumeAfterBackoffRequired) as caught:
            policy.acquire()
        assert slept == [], "the policy slept instead of handing the wait to the state machine"
        # Still capped: the caller is told to wait the bounded amount, not the provider's 99999s.
        assert caught.value.retry_after_seconds <= 60.0
        assert caught.value.connection_id == "c-one"

    def test_a_short_backoff_is_still_absorbed_in_process(self):
        # Handing a two-second wait to the state machine would cost a state transition to save
        # two seconds of Lambda time, so short waits stay in-process.
        slept: list[float] = []
        policy = RetryAfterRateLimitPolicy(connection_id="c-two", sleep=slept.append)
        policy.observe({"Retry-After": "2", "x-edl-response-status": "429"})
        policy.acquire()
        assert slept and slept[0] <= 2.0

    def test_telemetry_reports_hits_and_backoff(self):
        policy = RetryAfterRateLimitPolicy(connection_id="c-one", sleep=lambda s: None)
        policy.observe({"x-edl-response-status": "429"})
        policy.acquire()
        telemetry = telemetry_for(policy, checkpointed=True)
        assert telemetry.hits == 1
        assert telemetry.connection_id == "c-one"
        assert telemetry.checkpointed is True


class TestRateLimitRegistry:
    def test_every_customer_source_has_a_registered_policy(self):
        registered = rate_limit_policy_registry.registered_names()
        for expected in (
            "hubspot-standard",
            "wellsky-conservative",
            "google-ads-standard",
            "google-analytics-standard",
            "meta-ads-standard",
            "dialpad-standard",
            "housecall-pro-standard",
            "maid-central-standard",
            "servman-pro-standard",
            "seniorplace-standard",
            "sage-intacct-standard",
        ):
            assert expected in registered

    def test_per_connection_policies_are_independent(self):
        first = rate_limit_policy_registry.resolve("hubspot-standard", "hubspot-grasons")
        second = rate_limit_policy_registry.resolve("hubspot-standard", "hubspot-shine")
        assert first is not second

    def test_the_same_connection_reuses_its_policy(self):
        first = rate_limit_policy_registry.resolve("hubspot-standard", "hubspot-grasons")
        assert rate_limit_policy_registry.resolve("hubspot-standard", "hubspot-grasons") is first

    def test_shared_quota_providers_share_one_instance(self):
        first = rate_limit_policy_registry.resolve("meta-ads-standard", "meta-a")
        second = rate_limit_policy_registry.resolve("meta-ads-standard", "meta-b")
        assert first is second

    def test_unknown_policy_raises(self):
        with pytest.raises(KeyError, match="No rate-limit policy"):
            rate_limit_policy_registry.resolve("nope", "c-one")

    def test_duplicate_registration_is_refused(self):
        with pytest.raises(ValueError, match="already registered"):
            rate_limit_policy_registry.register(
                "hubspot-standard", RateLimitPolicySpec(RateLimitStrategy.FIXED_WINDOW)
            )


class TestPagination:
    def test_offset_limit_ends_on_a_short_page(self):
        pages = [
            SourcePage(records=[{"id": i} for i in range(2)]),
            SourcePage(records=[{"id": 2}]),
        ]
        calls: list[dict] = []

        def fetch(parameters):
            calls.append(dict(parameters))
            return pages[len(calls) - 1]

        strategy = OffsetLimitPagination(fetch)
        request = SourceRequest(entity_id="hubspot-company", page_size=2)
        assert len(list(stream_records(strategy, request))) == 3
        assert calls[1]["offset"] == 2

    def test_cursor_follows_the_provider_cursor(self):
        pages = [
            SourcePage(records=[{"id": 1}], next_cursor="c1"),
            SourcePage(records=[{"id": 2}], next_cursor=None),
        ]
        index = [0]

        def fetch(parameters):
            page = pages[index[0]]
            index[0] += 1
            return page

        strategy = CursorPagination(fetch)
        assert len(list(stream_records(strategy, SourceRequest(entity_id="x-entity")))) == 2

    def test_a_repeated_cursor_is_a_provider_defect(self):
        def fetch(parameters):
            return SourcePage(records=[{"id": 1}], next_cursor="same")

        strategy = CursorPagination(fetch)
        with pytest.raises(PaginationExhaustionError, match="not advancing"):
            list(stream_records(strategy, SourceRequest(entity_id="x-entity")))

    def test_keyset_seeks_by_the_last_key(self):
        pages = [
            SourcePage(records=[{"id": 1}, {"id": 2}]),
            SourcePage(records=[{"id": 3}]),
        ]
        calls: list[dict] = []

        def fetch(parameters):
            calls.append(dict(parameters))
            return pages[len(calls) - 1]

        strategy = KeysetPagination(fetch)
        request = SourceRequest(entity_id="x-entity", page_size=2, keyset_field="id")
        assert len(list(stream_records(strategy, request))) == 3
        assert calls[1]["after_key"] == 2

    def test_keyset_requires_a_keyset_field(self):
        strategy = KeysetPagination(lambda parameters: SourcePage(records=[]))
        with pytest.raises(ValueError, match="requires keyset_field"):
            list(strategy.pages(SourceRequest(entity_id="x-entity")))

    def test_keyset_stops_when_the_key_does_not_advance(self):
        def fetch(parameters):
            return SourcePage(records=[{"id": 1}, {"id": 1}])

        strategy = KeysetPagination(fetch)
        request = SourceRequest(entity_id="x-entity", page_size=2, keyset_field="id")
        assert len(list(stream_records(strategy, request))) == 2

    def test_link_header_pagination_follows_next(self):
        pages = [
            SourcePage(
                records=[{"id": 1}],
                headers={"Link": '<https://api.example.test/x?page=2>; rel="next"'},
            ),
            SourcePage(records=[{"id": 2}], headers={}),
        ]
        index = [0]

        def fetch(parameters):
            page = pages[index[0]]
            index[0] += 1
            return page

        strategy = LinkHeaderPagination(fetch)
        assert len(list(stream_records(strategy, SourceRequest(entity_id="x-entity")))) == 2

    def test_next_link_parsing(self):
        header = '<https://a.test/1>; rel="prev", <https://a.test/3>; rel="next"'
        assert parse_next_link(header) == "https://a.test/3"
        assert parse_next_link("") is None
        assert parse_next_link('<https://a.test/1>; rel="prev"') is None

    def test_page_budget_prevents_an_infinite_loop(self):
        def fetch(parameters):
            return SourcePage(records=[{"id": 1}] * 2)

        strategy = OffsetLimitPagination(fetch, max_pages=3)
        request = SourceRequest(entity_id="x-entity", page_size=2)
        with pytest.raises(PaginationExhaustionError, match="without signalling exhaustion"):
            list(stream_records(strategy, request))

    def test_every_kind_is_registered(self):
        assert set(pagination_strategy_registry.registered_names()) == {
            k.value for k in PaginationKind
        }

    def test_registry_resolves_a_strategy(self):
        strategy = pagination_strategy_registry.resolve(
            PaginationKind.CURSOR.value, lambda p: SourcePage(records=[])
        )
        assert isinstance(strategy, CursorPagination)

    def test_unknown_strategy_raises(self):
        with pytest.raises(KeyError, match="No pagination strategy"):
            pagination_strategy_registry.resolve("nope", lambda p: SourcePage(records=[]))


class TestSyncStrategies:
    def test_watermark_polling_builds_a_window_from_the_watermark(self):
        last = datetime.now(UTC) - timedelta(days=1)
        plan = WatermarkPollingSyncStrategy().plan(_config(), WatermarkState(last, last))
        assert plan.mode is ExtractionMode.INCREMENTAL_WINDOW
        assert plan.watermark_lower is not None
        assert plan.watermark_lower < last

    def test_first_run_uses_the_configured_window(self):
        plan = WatermarkPollingSyncStrategy().plan(_config(), None)
        assert plan.window_days is not None
        assert 6.9 < plan.window_days < 7.1

    def test_full_load_needs_no_window(self):
        config = _config(load_type=LoadType.FULL, watermark_field=None)
        plan = WatermarkPollingSyncStrategy().plan(config, None)
        assert plan.mode is ExtractionMode.FULL
        assert plan.window_days is None

    def test_webhook_drain_when_the_stream_is_current(self):
        recent = datetime.now(UTC) - timedelta(minutes=5)
        plan = WebhookIngestSyncStrategy(drain_queue_url="https://sqs.test/q").plan(
            _config(sync_strategy="webhook_ingest"), WatermarkState(recent, recent)
        )
        assert plan.mode is ExtractionMode.WEBHOOK_DRAIN
        assert plan.drain_queue_url == "https://sqs.test/q"

    def test_webhook_gap_falls_back_to_a_polling_backfill(self):
        stale = datetime.now(UTC) - timedelta(days=2)
        plan = WebhookIngestSyncStrategy().plan(
            _config(sync_strategy="webhook_ingest"), WatermarkState(stale, stale)
        )
        assert plan.mode is ExtractionMode.GAP_BACKFILL
        assert "fell behind" in plan.reason

    def test_webhook_with_no_watermark_backfills(self):
        plan = WebhookIngestSyncStrategy().plan(_config(sync_strategy="webhook_ingest"), None)
        assert plan.mode is ExtractionMode.GAP_BACKFILL

    def test_cdc_streams_from_the_recorded_position(self):
        plan = LogBasedCdcSyncStrategy(cdc_position="mysql-bin.000042:1234").plan(
            _config(sync_strategy="log_based_cdc"), None
        )
        assert plan.mode is ExtractionMode.CDC_STREAM
        assert plan.cdc_start_position == "mysql-bin.000042:1234"

    def test_cdc_without_a_position_degrades_to_polling(self):
        plan = LogBasedCdcSyncStrategy().plan(_config(sync_strategy="log_based_cdc"), None)
        assert plan.mode is ExtractionMode.GAP_BACKFILL

    def test_every_kind_is_registered(self):
        assert set(sync_strategy_registry.registered_names()) == {k.value for k in SyncStrategyKind}

    def test_plan_for_config_resolves_the_configured_strategy(self):
        plan = plan_for_config(_config(sync_strategy="watermark_polling"), None)
        assert plan.mode is ExtractionMode.INCREMENTAL_WINDOW

    def test_unknown_strategy_raises(self):
        with pytest.raises(KeyError, match="No sync strategy"):
            sync_strategy_registry.resolve("nope")


class TestSourceCapabilities:
    def test_all_ten_customer_sources_declare_capabilities(self):
        import connector_runtime.adapters.dialpad.dialpad_connector
        import connector_runtime.adapters.google_ads.google_ads_connector
        import connector_runtime.adapters.google_analytics.google_analytics_connector
        import connector_runtime.adapters.housecall_pro.housecall_pro_connector
        import connector_runtime.adapters.hubspot.hubspot_connector
        import connector_runtime.adapters.maid_central.maid_central_connector
        import connector_runtime.adapters.meta_ads.meta_ads_connector
        import connector_runtime.adapters.seniorplace.seniorplace_connector
        import connector_runtime.adapters.servman_pro.servman_pro_connector
        import connector_runtime.adapters.wellsky.wellsky_connector
        import connector_runtime.legacy_source_capabilities  # noqa: F401

        declared = set(source_capability_registry.registered_source_ids())
        for source_id in (
            "hubspot",
            "maid-central",
            "servman-pro",
            "wellsky",
            "housecall-pro",
            "seniorplace",
            "google-ads",
            "google-analytics",
            "meta-ads",
            "dialpad",
            "sage",
        ):
            assert source_id in declared

    def test_capability_query_is_declarative_not_name_based(self):
        import connector_runtime.adapters.hubspot.hubspot_connector  # noqa: F401

        webhook_sources = source_capability_registry.sources_supporting(SourceCapability.WEBHOOKS)
        assert "hubspot" in webhook_sources

    def test_require_raises_a_distinguishable_error(self):
        declaration = SourceCapabilityDeclaration(
            source_id="x-source",
            display_name="X",
            capabilities=frozenset({SourceCapability.INCREMENTAL}),
        )
        assert declaration.supports(SourceCapability.INCREMENTAL) is True
        with pytest.raises(SourceCapabilityUnavailableError, match="does not support"):
            declaration.require(SourceCapability.WRITEBACK)

    def test_duplicate_declaration_is_refused(self):
        import connector_runtime.adapters.hubspot.hubspot_connector  # noqa: F401

        with pytest.raises(ValueError, match="already exists"):
            source_capability_registry.register(
                SourceCapabilityDeclaration(
                    source_id="hubspot", display_name="Dup", capabilities=frozenset()
                )
            )

    def test_unknown_source_raises(self):
        with pytest.raises(KeyError, match="No capability declaration"):
            source_capability_registry.get("not-a-source")

    def test_outbound_host_allowlist_blocks_ssrf(self):
        import connector_runtime.adapters.hubspot.hubspot_connector  # noqa: F401

        enforce_allowed_host("hubspot", "api.hubapi.com")
        with pytest.raises(OutboundHostNotAllowedError, match="not allowlisted"):
            enforce_allowed_host("hubspot", "169.254.169.254")

    def test_a_source_declaring_no_hosts_is_unrestricted_by_declaration(self):
        import connector_runtime.legacy_source_capabilities  # noqa: F401

        enforce_allowed_host("salesforce", "anything.example.test")

    def test_allowed_hostnames_union(self):
        import connector_runtime.adapters.hubspot.hubspot_connector  # noqa: F401

        assert "api.hubapi.com" in source_capability_registry.allowed_hostnames()


class TestWebhookSignatures:
    _SECRET = "shhh"
    _BODY = '{"eventId": 1}'

    def test_hex_hmac_round_trip(self):
        spec = SignatureSpec(algorithm=SignatureAlgorithm.HMAC_SHA256_HEX, signature_header="X-Sig")
        signature = compute_signature(spec.algorithm, self._SECRET, self._BODY)
        verify_webhook_signature(spec, self._SECRET, self._BODY, {"X-Sig": signature})

    def test_base64_hmac_round_trip(self):
        spec = SignatureSpec(
            algorithm=SignatureAlgorithm.HMAC_SHA256_BASE64, signature_header="X-Sig"
        )
        signature = compute_signature(spec.algorithm, self._SECRET, self._BODY)
        verify_webhook_signature(spec, self._SECRET, self._BODY, {"X-Sig": signature})

    def test_sha1_is_supported_for_legacy_providers(self):
        spec = SignatureSpec(algorithm=SignatureAlgorithm.HMAC_SHA1_HEX, signature_header="X-Sig")
        signature = compute_signature(spec.algorithm, self._SECRET, self._BODY)
        verify_webhook_signature(spec, self._SECRET, self._BODY, {"X-Sig": signature})

    def test_missing_header_fails_closed(self):
        spec = SignatureSpec(algorithm=SignatureAlgorithm.HMAC_SHA256_HEX, signature_header="X-Sig")
        with pytest.raises(WebhookSignatureError, match="Unsigned providers must be polled"):
            verify_webhook_signature(spec, self._SECRET, self._BODY, {})

    def test_wrong_signature_is_refused(self):
        spec = SignatureSpec(algorithm=SignatureAlgorithm.HMAC_SHA256_HEX, signature_header="X-Sig")
        with pytest.raises(WebhookSignatureError, match="does not match"):
            verify_webhook_signature(spec, self._SECRET, self._BODY, {"X-Sig": "0" * 64})

    def test_a_different_secret_is_refused(self):
        spec = SignatureSpec(algorithm=SignatureAlgorithm.HMAC_SHA256_HEX, signature_header="X-Sig")
        signature = compute_signature(spec.algorithm, "other-secret", self._BODY)
        with pytest.raises(WebhookSignatureError):
            verify_webhook_signature(spec, self._SECRET, self._BODY, {"X-Sig": signature})

    def test_timestamped_signature_bounds_replay(self):
        spec = SignatureSpec(
            algorithm=SignatureAlgorithm.HMAC_SHA256_BASE64,
            signature_header="X-Sig",
            timestamp_header="X-Ts",
            signed_payload_template="{body}{timestamp}",
        )
        signature = compute_signature(spec.algorithm, self._SECRET, f"{self._BODY}1000")
        verify_webhook_signature(
            spec,
            self._SECRET,
            self._BODY,
            {"X-Sig": signature, "X-Ts": "1000"},
            now_epoch_seconds=1010,
        )
        with pytest.raises(WebhookSignatureError, match="treating it as a replay"):
            verify_webhook_signature(
                spec,
                self._SECRET,
                self._BODY,
                {"X-Sig": signature, "X-Ts": "1000"},
                now_epoch_seconds=100_000,
            )

    def test_missing_timestamp_fails_closed(self):
        spec = SignatureSpec(
            algorithm=SignatureAlgorithm.HMAC_SHA256_HEX,
            signature_header="X-Sig",
            timestamp_header="X-Ts",
        )
        with pytest.raises(WebhookSignatureError, match="replay cannot be bounded"):
            verify_webhook_signature(spec, self._SECRET, self._BODY, {"X-Sig": "x"})

    def test_non_numeric_timestamp_is_refused(self):
        spec = SignatureSpec(
            algorithm=SignatureAlgorithm.HMAC_SHA256_HEX,
            signature_header="X-Sig",
            timestamp_header="X-Ts",
        )
        with pytest.raises(WebhookSignatureError, match="not a numeric epoch"):
            verify_webhook_signature(spec, self._SECRET, self._BODY, {"X-Sig": "x", "X-Ts": "now"})

    def test_millisecond_timestamps_are_normalised(self):
        spec = SignatureSpec(
            algorithm=SignatureAlgorithm.HMAC_SHA256_BASE64,
            signature_header="X-Sig",
            timestamp_header="X-Ts",
            signed_payload_template="{body}{timestamp}",
        )
        signature = compute_signature(spec.algorithm, self._SECRET, f"{self._BODY}1700000000000")
        verify_webhook_signature(
            spec,
            self._SECRET,
            self._BODY,
            {"X-Sig": signature, "X-Ts": "1700000000000"},
            now_epoch_seconds=1_700_000_010,
        )

    def test_webhook_capable_providers_have_specs(self):
        for source_id in ("hubspot", "dialpad", "housecall-pro"):
            assert spec_for_source(source_id)

    def test_a_source_with_no_spec_must_be_polled(self):
        with pytest.raises(WebhookSignatureError, match="Poll it instead"):
            spec_for_source("maid-central")

    def test_dedup_hash_is_stable(self):
        assert sha256_hex("a") == sha256_hex("a")
        assert sha256_hex("a") != sha256_hex("b")
