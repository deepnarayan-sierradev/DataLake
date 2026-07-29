"""
Every assertion here restates a fact from a vendor's published documentation.

This file is the control that the previous generation of specs lacked. A spec can be
syntactically valid, register cleanly, and pass the whole substrate suite while naming
endpoints that do not exist — which is exactly what MaidCentral, WellSky and SeniorPlace
did until 2026-07-29. A unit test that only exercises the spec against itself cannot catch
that; a test that restates the *document* can, because the two have to be changed together.

Sources of truth, each re-read on 2026-07-29:

- MaidCentral — *MaidCentral Reporting API Guide* (`API Docs/maid_central/`)
- WellSky Personal Care — Home Connect API, Swagger 2.0 (`apidocs.clearcareonline.com`)
- SeniorPlace — OpenAPI 3.0.3 (`seniorplace-public.s3.us-west-2.amazonaws.com/docs`)
- BePro — OpenAPI 3.1 (`data-api-doc.bepro.ai`)
- ServiceBridge — Public API help centre article 2490148 and the 2.0 upgrade note
- DialPad — `developers.dialpad.com` rate-limits page
"""

from __future__ import annotations

from typing import Final

import pytest

from connector_runtime.adapters.bepro.bepro_connector import (
    BEPRO_SPEC,
    DOCUMENTED_BURST_PER_SECOND,
    DOCUMENTED_REQUESTS_PER_MINUTE,
    MATCH_SCOPED_ENTITY_IDS,
)
from connector_runtime.adapters.maid_central.maid_central_connector import (
    MAID_CENTRAL_SPEC,
    MAX_RESULT_COUNT,
)
from connector_runtime.adapters.rest_api.rest_source_spec import (
    AuthKind,
    RestSourceSpec,
    TokenGrantKind,
)
from connector_runtime.adapters.seniorplace.seniorplace_connector import SENIORPLACE_SPEC
from connector_runtime.adapters.servicebridge.servicebridge_connector import (
    DOCUMENTED_REQUESTS_PER_HOUR,
    DOCUMENTED_REQUESTS_PER_SECOND,
    SERVICEBRIDGE_SPEC,
)
from connector_runtime.adapters.servicebridge.servicebridge_connector import (
    RATE_LIMIT_POLICY_NAME as SERVICEBRIDGE_POLICY,
)
from connector_runtime.adapters.wellsky.wellsky_connector import MAX_COUNT, WELLSKY_SPEC
from connector_runtime.rate_limiting import (
    RateLimitStrategy,
    rate_limit_policy_registry,
)
from connector_runtime.source_capabilities import SourceCapability

_SPECS: Final[tuple[RestSourceSpec, ...]] = (
    BEPRO_SPEC,
    MAID_CENTRAL_SPEC,
    SENIORPLACE_SPEC,
    SERVICEBRIDGE_SPEC,
    WELLSKY_SPEC,
)


def _policy_spec(name: str) -> object:
    return rate_limit_policy_registry._specs[name]


class TestServiceBridge:
    def test_the_base_url_is_the_documented_host(self) -> None:
        assert SERVICEBRIDGE_SPEC.base_url == "https://cloud.servicebridge.com"

    def test_the_documented_quota_is_recorded_verbatim(self) -> None:
        assert DOCUMENTED_REQUESTS_PER_SECOND == 50
        assert DOCUMENTED_REQUESTS_PER_HOUR == 60_000

    def test_the_quota_is_shared_because_it_is_per_ip_not_per_token(self) -> None:
        # The single most important property of this connector: every Lambda egresses
        # through one NAT address, so N connections spend one budget. A per-connection
        # policy would let N extractions each believe they own 50 rps.
        assert _policy_spec(SERVICEBRIDGE_POLICY).shared_across_connections is True

    def test_the_sustained_rate_stays_under_the_hourly_ceiling(self) -> None:
        spec = _policy_spec(SERVICEBRIDGE_POLICY)
        assert spec.strategy is RateLimitStrategy.TOKEN_BUCKET
        assert spec.refill_per_second < DOCUMENTED_REQUESTS_PER_HOUR / 3600
        assert spec.capacity < DOCUMENTED_REQUESTS_PER_SECOND

    def test_the_same_policy_instance_serves_two_connections(self) -> None:
        first = rate_limit_policy_registry.resolve(SERVICEBRIDGE_POLICY, "connection-a")
        second = rate_limit_policy_registry.resolve(SERVICEBRIDGE_POLICY, "connection-b")
        assert first is second

    def test_authentication_is_a_query_string_session_key(self) -> None:
        assert SERVICEBRIDGE_SPEC.auth_kind is AuthKind.SESSION_KEY_QUERY
        assert SERVICEBRIDGE_SPEC.session_key_parameter == "sessionKey"

    def test_the_session_is_re_acquired_through_the_login_endpoint(self) -> None:
        # A 30-minute sliding expiry outlives no realistic sweep on its own.
        assert SERVICEBRIDGE_SPEC.token_endpoint_path == "/api/v1/login"
        assert SERVICEBRIDGE_SPEC.token_grant_kind is TokenGrantKind.SESSION_LOGIN

    def test_the_login_credential_keys_are_required(self) -> None:
        assert SERVICEBRIDGE_SPEC.required_credential_keys == frozenset({"user_id", "password"})

    def test_every_documented_resource_is_declared(self) -> None:
        expected = {
            "servicebridge-customer",
            "servicebridge-location",
            "servicebridge-contact",
            "servicebridge-work-order",
            "servicebridge-estimate",
            "servicebridge-invoice",
            "servicebridge-appointment",
            "servicebridge-employee",
            "servicebridge-service",
            "servicebridge-marketing-category",
        }
        assert set(SERVICEBRIDGE_SPEC.entity_ids()) == expected

    def test_customers_locations_and_contacts_use_the_v2_shape(self) -> None:
        # The 2.0 upgrade note is explicit that these three changed shape in v2.
        for suffix in ("customer", "location", "contact"):
            assert SERVICEBRIDGE_SPEC.entity(f"servicebridge-{suffix}").path.startswith("/api/v2/")

    def test_paging_is_page_indexed_from_one(self) -> None:
        names = SERVICEBRIDGE_SPEC.pagination_parameters
        assert names.first_page_index == 1
        assert SERVICEBRIDGE_SPEC.default_pagination_strategy == "page_number"


class TestBePro:
    def test_the_base_url_is_the_documented_server(self) -> None:
        assert BEPRO_SPEC.base_url == "https://ds.bepro.ai"

    def test_the_documented_quota_is_recorded_verbatim(self) -> None:
        assert DOCUMENTED_REQUESTS_PER_MINUTE == 1_000
        assert DOCUMENTED_BURST_PER_SECOND == 100

    def test_the_two_tier_quota_is_expressed_as_burst_capacity_and_sustained_refill(
        self,
    ) -> None:
        spec = _policy_spec("bepro-standard")
        assert spec.strategy is RateLimitStrategy.TOKEN_BUCKET
        assert spec.capacity <= DOCUMENTED_BURST_PER_SECOND
        assert spec.refill_per_second <= DOCUMENTED_REQUESTS_PER_MINUTE / 60

    def test_the_quota_is_per_token_so_the_policy_is_not_shared(self) -> None:
        # Negative control against the ServiceBridge case above: not every source shares.
        assert _policy_spec("bepro-standard").shared_across_connections is False

    def test_incremental_is_not_claimed_because_no_endpoint_exposes_a_timestamp(self) -> None:
        assert SourceCapability.INCREMENTAL not in BEPRO_SPEC.capabilities

    def test_no_entity_declares_a_watermark_field(self) -> None:
        assert all(entity.watermark_field is None for entity in BEPRO_SPEC.entities)

    def test_every_documented_endpoint_family_is_declared(self) -> None:
        paths = {entity.path for entity in BEPRO_SPEC.entities}
        assert paths == {
            "/data-api/meta/clubs",
            "/data-api/meta/leagues",
            "/data-api/meta/seasons",
            "/data-api/meta/teams",
            "/data-api/meta/players",
            "/data-api/meta/matches",
            "/data-api/meta/lineups",
            "/data-api/data/events",
            "/data-api/data/sequences",
            "/data-api/data/stats/players",
            "/data-api/data/stats/teams",
            "/data-api/data/schemas",
            "/data-api/data/tracking",
            "/data-api/video/timings",
            "/data-api/external/clubs",
            "/data-api/external/leagues",
            "/data-api/external/seasons",
            "/data-api/external/teams",
            "/data-api/external/players",
            "/data-api/external/matches",
        }

    def test_the_envelope_record_path_is_data_on_every_entity(self) -> None:
        assert all(entity.records_json_path == ("data",) for entity in BEPRO_SPEC.entities)

    def test_the_match_scoped_endpoints_declare_their_required_parameter(self) -> None:
        assert MATCH_SCOPED_ENTITY_IDS == {"bepro-tracking", "bepro-video-timing"}
        for entity_id in MATCH_SCOPED_ENTITY_IDS:
            assert BEPRO_SPEC.entity(entity_id).required_run_parameters == ("match_id",)

    def test_the_unpaginated_endpoints_are_declared_as_single_request(self) -> None:
        for entity_id in ("bepro-tracking", "bepro-video-timing", "bepro-event-schema"):
            assert BEPRO_SPEC.entity(entity_id).pagination_strategy == "single_request"

    def test_the_schemas_endpoint_supplies_its_required_sport_type(self) -> None:
        entity = BEPRO_SPEC.entity("bepro-event-schema")
        assert entity.static_query_parameters["sport_type"]
        assert entity.required_run_parameters == ()


class TestMaidCentral:
    def test_the_reporting_prefix_is_on_every_path(self) -> None:
        assert all(
            entity.path.startswith("/api/v1/reporting/") for entity in MAID_CENTRAL_SPEC.entities
        )

    def test_all_thirteen_documented_entities_are_declared(self) -> None:
        assert len(MAID_CENTRAL_SPEC.entities) == 13

    def test_authentication_is_oauth_not_an_api_key_header(self) -> None:
        assert MAID_CENTRAL_SPEC.auth_kind is AuthKind.OAUTH2_REFRESH
        assert MAID_CENTRAL_SPEC.token_endpoint_path == "/token"
        assert MAID_CENTRAL_SPEC.token_grant_kind is TokenGrantKind.PASSWORD

    def test_the_envelope_is_result_items(self) -> None:
        assert all(
            entity.records_json_path == ("Result", "Items") for entity in MAID_CENTRAL_SPEC.entities
        )

    def test_paging_uses_skip_count_and_max_result_count(self) -> None:
        names = MAID_CENTRAL_SPEC.pagination_parameters
        assert (names.offset, names.limit) == ("skipCount", "maxResultCount")

    def test_the_page_size_is_the_documented_maximum(self) -> None:
        assert MAX_RESULT_COUNT == 1_000
        assert all(entity.page_size == MAX_RESULT_COUNT for entity in MAID_CENTRAL_SPEC.entities)

    def test_no_entity_uses_a_generic_id_as_its_natural_key(self) -> None:
        # Not one reporting DTO calls its identifier `id`; assuming one would null every key.
        assert all(entity.natural_key_field != "id" for entity in MAID_CENTRAL_SPEC.entities)

    def test_the_hourly_budget_is_the_binding_constraint(self) -> None:
        spec = _policy_spec("maid-central-hourly")
        assert spec.strategy is RateLimitStrategy.TOKEN_BUCKET
        assert spec.refill_per_second < 1_000 / 3600
        assert spec.capacity <= 100

    def test_the_previous_policy_name_still_resolves(self) -> None:
        # A connection configured before the rewrite must not fail at build time.
        assert rate_limit_policy_registry.resolve("maid-central-standard", "c") is not None


class TestWellSky:
    def test_the_base_url_is_the_connect_api_host(self) -> None:
        assert WELLSKY_SPEC.base_url == "https://connect.clearcareonline.com"

    def test_reads_are_post_searches_where_the_api_publishes_one(self) -> None:
        searchable = [e for e in WELLSKY_SPEC.entities if e.path.endswith("/_search/")]
        assert searchable
        assert all(e.read_method == "POST" for e in searchable)

    def test_every_path_carries_the_mandatory_trailing_slash(self) -> None:
        assert all(entity.path.endswith("/") for entity in WELLSKY_SPEC.entities)

    def test_records_are_unwrapped_from_the_fhir_bundle(self) -> None:
        assert all(entity.records_json_path == ("entry",) for entity in WELLSKY_SPEC.entities)
        assert all(entity.record_unwrap_field == "resource" for entity in WELLSKY_SPEC.entities)

    def test_paging_is_page_indexed_and_capped_at_the_documented_maximum(self) -> None:
        names = WELLSKY_SPEC.pagination_parameters
        assert (names.page, names.limit) == ("_page", "_count")
        assert names.first_page_index == 0
        assert MAX_COUNT == 100
        assert all(entity.page_size <= MAX_COUNT for entity in WELLSKY_SPEC.entities)

    def test_the_incremental_bound_is_a_comparator_prefixed_body_field(self) -> None:
        patient = WELLSKY_SPEC.entity("wellsky-patient")
        assert patient.watermark_body_field == "updated"
        assert patient.watermark_comparator_prefix == "ge"

    def test_authentication_is_client_credentials(self) -> None:
        assert WELLSKY_SPEC.token_endpoint_path == "/oauth/accesstoken"
        assert WELLSKY_SPEC.token_grant_kind is TokenGrantKind.CLIENT_CREDENTIALS
        assert WELLSKY_SPEC.required_credential_keys == frozenset({"client_id", "client_secret"})

    def test_the_policy_sits_far_below_the_requested_ceiling(self) -> None:
        # The vendor asks for <=100 req/s and advises against batch use.
        spec = _policy_spec("wellsky-conservative")
        assert spec.strategy is RateLimitStrategy.TOKEN_BUCKET
        assert spec.refill_per_second <= 10.0


class TestSeniorPlace:
    def test_the_base_url_is_the_documented_production_server(self) -> None:
        assert SENIORPLACE_SPEC.base_url == "https://app.seniorplace.com"

    def test_no_entity_is_modelled_as_odata(self) -> None:
        # The OData contract belongs to ALL IN, the downstream system — not to SeniorPlace.
        assert all("/odata/" not in entity.path for entity in SENIORPLACE_SPEC.entities)

    def test_authentication_uses_the_api_key_scheme_word(self) -> None:
        assert SENIORPLACE_SPEC.auth_kind is AuthKind.API_KEY_HEADER
        assert SENIORPLACE_SPEC.api_key_header_name == "Authorization"
        assert SENIORPLACE_SPEC.api_key_value_prefix == "ApiKey "

    def test_only_clients_declares_an_incremental_filter(self) -> None:
        watermarked = [e.entity_id for e in SENIORPLACE_SPEC.entities if e.watermark_field]
        assert watermarked == ["seniorplace-client"]
        assert SENIORPLACE_SPEC.watermark_lower_parameter == "updatedAfter"

    def test_no_paging_parameter_is_invented(self) -> None:
        assert all(
            entity.pagination_strategy == "single_request" for entity in SENIORPLACE_SPEC.entities
        )

    def test_collections_are_read_as_a_bare_array(self) -> None:
        assert all(entity.records_json_path == () for entity in SENIORPLACE_SPEC.entities)


class TestDialpad:
    def test_the_refill_matches_the_documented_per_company_limit(self) -> None:
        spec = _policy_spec("dialpad-standard")
        assert spec.capacity == 20
        assert 10.0 < spec.refill_per_second <= 20.0


class TestEverySpecIsInternallyConsistent:
    """Cross-cutting properties no individual source should be allowed to break."""

    @pytest.mark.parametrize("spec", _SPECS, ids=lambda s: s.source_id)
    def test_the_transport_is_tls(self, spec: RestSourceSpec) -> None:
        assert spec.base_url.startswith("https://")

    @pytest.mark.parametrize("spec", _SPECS, ids=lambda s: s.source_id)
    def test_the_declared_rate_limit_policy_is_registered(self, spec: RestSourceSpec) -> None:
        assert spec.default_rate_limit_policy in rate_limit_policy_registry.registered_names()

    @pytest.mark.parametrize("spec", _SPECS, ids=lambda s: s.source_id)
    def test_no_source_is_left_unthrottled(self, spec: RestSourceSpec) -> None:
        assert spec.default_rate_limit_policy is not None

    @pytest.mark.parametrize("spec", _SPECS, ids=lambda s: s.source_id)
    def test_incremental_is_claimed_only_when_an_entity_can_filter(
        self, spec: RestSourceSpec
    ) -> None:
        # Claiming INCREMENTAL without a watermark field advances the watermark against
        # data that was never filtered — a silent, permanent gap.
        if SourceCapability.INCREMENTAL in spec.capabilities:
            assert any(entity.watermark_field for entity in spec.entities)

    @pytest.mark.parametrize("spec", _SPECS, ids=lambda s: s.source_id)
    def test_a_token_exchanging_source_stores_the_keys_its_grant_needs(
        self, spec: RestSourceSpec
    ) -> None:
        if spec.token_grant_kind is TokenGrantKind.CLIENT_CREDENTIALS:
            assert {"client_id", "client_secret"} <= spec.required_credential_keys
        elif spec.token_grant_kind is TokenGrantKind.SESSION_LOGIN:
            assert {"user_id", "password"} <= spec.required_credential_keys
        elif spec.token_grant_kind is TokenGrantKind.PASSWORD:
            assert {"username", "password"} <= spec.required_credential_keys

    @pytest.mark.parametrize("spec", _SPECS, ids=lambda s: s.source_id)
    def test_no_undocumented_field_projection_parameter_is_sent(self, spec: RestSourceSpec) -> None:
        # None of these five documents a projection parameter. The substrate used to send
        # HubSpot's `properties` to every source; an API that validates its query string
        # answers 400 rather than ignoring it.
        assert spec.field_projection_parameter is None

    @pytest.mark.parametrize("spec", _SPECS, ids=lambda s: s.source_id)
    def test_the_declared_pagination_strategies_all_resolve(self, spec: RestSourceSpec) -> None:
        from connector_runtime.pagination import pagination_strategy_registry

        registered = set(pagination_strategy_registry.registered_names())
        assert spec.default_pagination_strategy in registered
        for entity in spec.entities:
            if entity.pagination_strategy:
                assert entity.pagination_strategy in registered
