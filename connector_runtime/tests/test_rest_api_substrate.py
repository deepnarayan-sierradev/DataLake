"""
Tests for the shared REST substrate: the HTTP session and the spec-driven connector.

Every one of the ten new sources runs on this code, so its security properties are the ones
that matter most: TLS only, outbound host allowlisting, no credential in a log line, and an
error taxonomy the retry policy can act on.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from connector_runtime.adapters.rest_api.rest_api_connector import RestApiConnector
from connector_runtime.adapters.rest_api.rest_http_session import (
    RestHttpSession,
    RestResponse,
    RestSourceCredentialError,
    RestSourceObjectError,
    RestSourceRequestError,
    RestSourceThrottledError,
    RestSourceTransientError,
)
from connector_runtime.adapters.rest_api.rest_source_spec import (
    AuthKind,
    EntityShape,
    RestEntitySpec,
    RestSourceSpec,
)
from connector_runtime.interfaces.connector_interface import ExtractionErrorClassification
from connector_runtime.rate_limiting import RateLimitPolicy
from connector_runtime.source_capabilities import (
    OutboundHostNotAllowedError,
    SourceCapability,
    SourceCapabilityDeclaration,
    SourceCapabilityUnavailableError,
    source_capability_registry,
)
from contracts.entity_configuration_contract import FieldMode, LoadType


class _CountingPolicy(RateLimitPolicy):
    """Records acquisitions rather than sleeping."""

    def __init__(self) -> None:
        super().__init__(connection_id="test-connection", sleep=lambda _: None)
        self.acquisitions = 0

    def acquire(self) -> None:
        self.acquisitions += 1


class _FakeHttpResponse:
    def __init__(
        self, status_code: int = 200, body: Any = None, headers: dict[str, str] | None = None
    ) -> None:
        self.status_code = status_code
        self.text = "" if body is None else json.dumps(body)
        self.headers = headers or {}


class _RecordingSession:
    """A requests.Session stand-in that records calls and returns queued responses."""

    def __init__(self, responses: list[Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = responses or [_FakeHttpResponse(200, {"results": []})]

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response


_ENTITY = RestEntitySpec(
    entity_id="widget",
    path="/crm/v3/objects/widget",
    records_json_path=("results",),
    watermark_field="updatedAt",
    natural_key_field="id",
    page_size=2,
    writeback_path="/crm/v3/objects/widget",
    writeback_external_id_field="id",
)

_REPORT_ENTITY = RestEntitySpec(
    entity_id="spend-report",
    path="/reports/spend",
    shape=EntityShape.REPORT,
    report_metrics=("cost", "clicks"),
    report_dimensions=("date",),
)

_READ_ONLY_ENTITY = RestEntitySpec(entity_id="readonly", path="/v1/readonly")


def _spec(**overrides: Any) -> RestSourceSpec:
    payload: dict[str, Any] = {
        "source_id": "substrate-test",
        "display_name": "Substrate Test",
        "base_url": "https://api.example.com/",
        "auth_kind": AuthKind.BEARER_TOKEN,
        "entities": (_ENTITY, _REPORT_ENTITY, _READ_ONLY_ENTITY),
        "capabilities": frozenset({SourceCapability.INCREMENTAL}),
        "default_pagination_strategy": "offset_limit",
    }
    payload.update(overrides)
    return RestSourceSpec(**payload)


@pytest.fixture(autouse=True)
def _declared_source() -> Any:
    """Register the test source's capability declaration, then remove it again."""
    declaration = SourceCapabilityDeclaration(
        source_id="substrate-test",
        display_name="Substrate Test",
        capabilities=frozenset({SourceCapability.INCREMENTAL}),
        allowed_hostnames=("api.example.com",),
    )
    source_capability_registry.register(declaration)
    yield
    source_capability_registry._declarations.pop("substrate-test", None)


def _session(responses: list[Any] | None = None) -> tuple[RestHttpSession, _RecordingSession]:
    transport = _RecordingSession(responses)
    return (
        RestHttpSession(
            _spec(), {"access_token": "super-secret-token"}, _CountingPolicy(), session=transport
        ),
        transport,
    )


class TestSpecValidation:
    def test_a_plaintext_base_url_is_refused(self) -> None:
        with pytest.raises(ValueError, match="absolute https"):
            _spec(base_url="http://api.example.com")

    def test_a_relative_base_url_is_refused(self) -> None:
        with pytest.raises(ValueError, match="absolute https"):
            _spec(base_url="/api")

    def test_a_source_with_no_entities_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no entities"):
            _spec(entities=())

    def test_duplicate_entity_ids_are_refused(self) -> None:
        with pytest.raises(ValueError, match="duplicate entity ids"):
            _spec(entities=(_ENTITY, _ENTITY))

    def test_an_unsafe_endpoint_path_is_refused(self) -> None:
        with pytest.raises(ValueError, match="safe endpoint path"):
            RestEntitySpec(entity_id="bad", path="../../etc/passwd")

    def test_an_unsafe_writeback_path_is_refused(self) -> None:
        with pytest.raises(ValueError, match="safe endpoint path"):
            RestEntitySpec(entity_id="bad", path="/v1/ok", writeback_path="../../etc")

    def test_a_report_entity_must_declare_metrics(self) -> None:
        with pytest.raises(ValueError, match="must declare metrics"):
            RestEntitySpec(entity_id="r", path="/v1/r", shape=EntityShape.REPORT)

    def test_an_unknown_entity_id_raises(self) -> None:
        with pytest.raises(KeyError):
            _spec().entity("does-not-exist")

    def test_writeback_needs_both_a_path_and_an_external_id_field(self) -> None:
        assert _READ_ONLY_ENTITY.supports_writeback is False
        assert _ENTITY.supports_writeback is True
        partial = RestEntitySpec(entity_id="p", path="/v1/p", writeback_path="/v1/p")
        assert partial.supports_writeback is False


class TestResponseRecordExtraction:
    def test_the_declared_json_path_is_walked(self) -> None:
        response = RestResponse(200, {"data": {"items": [{"id": "1"}]}}, {})
        assert response.records(("data", "items")) == [{"id": "1"}]

    def test_a_missing_path_yields_no_records_rather_than_raising(self) -> None:
        response = RestResponse(200, {"data": {}}, {})
        assert response.records(("data", "items")) == []

    def test_a_scalar_at_the_path_yields_no_records(self) -> None:
        response = RestResponse(200, {"results": 5}, {})
        assert response.records(("results",)) == []

    def test_a_single_object_becomes_one_record(self) -> None:
        response = RestResponse(200, {"results": {"id": "1"}}, {})
        assert response.records(("results",)) == [{"id": "1"}]

    def test_non_object_list_entries_are_dropped(self) -> None:
        response = RestResponse(200, {"results": [{"id": "1"}, "junk", None]}, {})
        assert response.records(("results",)) == [{"id": "1"}]

    def test_a_non_mapping_body_yields_no_records(self) -> None:
        response = RestResponse(200, ["a"], {})
        assert response.records(("results",)) == []


class TestHttpSessionSecurity:
    def test_a_host_outside_the_allowlist_is_never_called(self) -> None:
        session, transport = _session()
        with pytest.raises(OutboundHostNotAllowedError):
            session.get("https://evil.example.com/steal")
        assert transport.calls == []

    def test_the_declared_host_is_called(self) -> None:
        session, transport = _session()
        session.get("/crm/v3/objects/widget")
        assert transport.calls[0]["url"] == "https://api.example.com/crm/v3/objects/widget"

    def test_an_absolute_link_header_url_still_passes_the_allowlist(self) -> None:
        session, transport = _session()
        session.get("https://api.example.com/crm/v3/objects/widget?after=2")
        assert transport.calls[0]["url"].startswith("https://api.example.com/")

    def test_a_bearer_token_is_sent_as_a_header_not_a_query_parameter(self) -> None:
        session, transport = _session()
        session.get("/crm/v3/objects/widget", {"limit": 2})
        call = transport.calls[0]
        assert call["headers"]["Authorization"] == "Bearer super-secret-token"
        assert "super-secret-token" not in json.dumps(call["params"])

    def test_an_api_key_source_uses_its_declared_header(self) -> None:
        spec = _spec(auth_kind=AuthKind.API_KEY_HEADER, api_key_header_name="X-Api-Key")
        transport = _RecordingSession()
        RestHttpSession(spec, {"api_key": "k"}, _CountingPolicy(), session=transport).get("/v1/x")
        assert transport.calls[0]["headers"]["X-Api-Key"] == "k"

    def test_basic_auth_is_base64_encoded(self) -> None:
        spec = _spec(auth_kind=AuthKind.BASIC)
        transport = _RecordingSession()
        RestHttpSession(
            spec, {"username": "u", "password": "p"}, _CountingPolicy(), session=transport
        ).get("/v1/x")
        # "u:p" base64-encoded
        assert transport.calls[0]["headers"]["Authorization"] == "Basic dTpw"

    def test_a_timeout_is_always_set(self) -> None:
        session, transport = _session()
        session.get("/v1/x")
        assert transport.calls[0]["timeout"] > 0

    def test_the_rate_limit_policy_is_acquired_before_every_request(self) -> None:
        policy = _CountingPolicy()
        transport = _RecordingSession()
        session = RestHttpSession(_spec(), {"access_token": "t"}, policy, session=transport)
        session.get("/v1/x")
        session.get("/v1/x")
        assert policy.acquisitions == 2

    def test_credentials_never_reach_the_log_line(self, caplog: Any) -> None:
        session, _ = _session()
        with caplog.at_level("INFO"):
            session.get("/crm/v3/objects/widget")
        assert "super-secret-token" not in caplog.text


class TestHttpSessionErrorTaxonomy:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (429, RestSourceThrottledError),
            (401, RestSourceCredentialError),
            (403, RestSourceCredentialError),
            (404, RestSourceObjectError),
            (400, RestSourceRequestError),
            (422, RestSourceRequestError),
            (500, RestSourceTransientError),
            (503, RestSourceTransientError),
        ],
    )
    def test_each_status_maps_to_its_classification(
        self, status: int, expected: type[Exception]
    ) -> None:
        session, _ = _session([_FakeHttpResponse(status, {})])
        with pytest.raises(expected):
            session.get("/v1/x")

    def test_a_network_failure_is_transient(self) -> None:
        import requests

        session, _ = _session([requests.ConnectionError("dns")])
        with pytest.raises(RestSourceTransientError):
            session.get("/v1/x")

    def test_the_network_error_message_does_not_leak_the_url_query(self) -> None:
        import requests

        session, _ = _session([requests.ConnectionError("connect to api.example.com?token=x")])
        with pytest.raises(RestSourceTransientError) as caught:
            session.get("/v1/x")
        assert "token=x" not in str(caught.value)

    def test_an_empty_body_parses_as_an_empty_object(self) -> None:
        session, _ = _session([_FakeHttpResponse(200, None)])
        assert session.get("/v1/x").body == {}

    def test_a_non_json_body_is_a_deterministic_request_error(self) -> None:
        class _Html:
            status_code = 200
            text = "<html>maintenance</html>"
            headers: dict[str, str] = {}

        session, _ = _session([_Html()])
        with pytest.raises(RestSourceRequestError, match="non-JSON"):
            session.get("/v1/x")

    def test_the_response_status_is_fed_to_the_rate_limit_policy(self) -> None:
        policy = _CountingPolicy()
        transport = _RecordingSession([_FakeHttpResponse(429, {})])
        session = RestHttpSession(_spec(), {"access_token": "t"}, policy, session=transport)
        with pytest.raises(RestSourceThrottledError):
            session.get("/v1/x")
        # observe() saw the 429 even though the provider sent no Retry-After header.
        assert policy.total_throttles == 1


def _connector(
    responses: list[Any] | None = None, entity_id: str = "widget"
) -> tuple[RestApiConnector, _RecordingSession]:
    session, transport = _session(responses)
    return (
        RestApiConnector(
            spec=_spec(),
            entity_id=entity_id,
            session=session,
            rate_limit_policy=_CountingPolicy(),
            connection_id="substrate-test-west",
        ),
        transport,
    )


class TestConnectorCapabilities:
    def test_capabilities_come_from_the_spec_declaration(self) -> None:
        connector, _ = _connector()
        capabilities = connector.get_capability_declaration()
        assert capabilities.supports_incremental is True
        assert capabilities.supports_bulk_extraction is False
        assert capabilities.supports_metadata_discovery is False

    def test_health_check_is_structural_and_issues_no_request(self) -> None:
        connector, transport = _connector()
        assert connector.health_check() is True
        assert transport.calls == []


class TestConnectorFieldDiscovery:
    def test_fields_are_inferred_from_a_sample_page(self) -> None:
        connector, _ = _connector([_FakeHttpResponse(200, {"results": [{"id": "1", "name": "a"}]})])
        contract = connector.discover_queryable_fields(
            "substrate-test", "widget", FieldMode.ALL, [], []
        )
        assert [f.name for f in contract.fields] == ["id", "name"]

    def test_a_new_source_field_appears_without_a_code_change(self) -> None:
        connector, _ = _connector(
            [_FakeHttpResponse(200, {"results": [{"id": "1", "newly_added": "x"}]})]
        )
        contract = connector.discover_queryable_fields(
            "substrate-test", "widget", FieldMode.ALL, [], []
        )
        assert "newly_added" in [f.name for f in contract.fields]

    def test_excluded_fields_are_dropped(self) -> None:
        connector, _ = _connector([_FakeHttpResponse(200, {"results": [{"id": "1", "ssn": "x"}]})])
        contract = connector.discover_queryable_fields(
            "substrate-test", "widget", FieldMode.ALL, [], ["ssn"]
        )
        assert [f.name for f in contract.fields] == ["id"]

    def test_include_only_without_include_fields_is_refused(self) -> None:
        connector, _ = _connector()
        with pytest.raises(RestSourceRequestError, match="INCLUDE_ONLY"):
            connector.discover_queryable_fields(
                "substrate-test", "widget", FieldMode.INCLUDE_ONLY, [], []
            )

    def test_include_only_uses_the_named_fields(self) -> None:
        connector, _ = _connector([_FakeHttpResponse(200, {"results": [{"id": "1", "name": "a"}]})])
        contract = connector.discover_queryable_fields(
            "substrate-test", "widget", FieldMode.INCLUDE_ONLY, ["name"], []
        )
        assert [f.name for f in contract.fields] == ["name"]

    def test_an_empty_entity_still_yields_a_non_empty_contract(self) -> None:
        # The drift evaluator needs something to compare against on the next run.
        connector, _ = _connector([_FakeHttpResponse(200, {"results": []})])
        contract = connector.discover_queryable_fields(
            "substrate-test", "widget", FieldMode.ALL, [], []
        )
        assert {f.name for f in contract.fields} == {"id", "updatedAt"}

    def test_discovery_does_not_guess_types(self) -> None:
        connector, _ = _connector([_FakeHttpResponse(200, {"results": [{"id": 1, "amount": 2.5}]})])
        contract = connector.discover_queryable_fields(
            "substrate-test", "widget", FieldMode.ALL, [], []
        )
        assert {f.data_type for f in contract.fields} == {"string"}

    def test_custom_prefixes_are_flagged(self) -> None:
        connector, _ = _connector(
            [_FakeHttpResponse(200, {"results": [{"id": "1", "hs_custom_x": "y"}]})]
        )
        contract = connector.discover_queryable_fields(
            "substrate-test", "widget", FieldMode.ALL, [], []
        )
        custom = {f.name for f in contract.fields if f.is_custom}
        assert custom == {"hs_custom_x"}

    def test_the_fingerprint_changes_when_the_field_set_changes(self) -> None:
        first, _ = _connector([_FakeHttpResponse(200, {"results": [{"id": "1"}]})])
        second, _ = _connector([_FakeHttpResponse(200, {"results": [{"id": "1", "name": "a"}]})])
        a = first.discover_queryable_fields("substrate-test", "widget", FieldMode.ALL, [], [])
        b = second.discover_queryable_fields("substrate-test", "widget", FieldMode.ALL, [], [])
        assert a.schema_fingerprint != b.schema_fingerprint


class TestConnectorQueryBuild:
    def _contract(self, connector: RestApiConnector) -> Any:
        return connector.discover_queryable_fields(
            "substrate-test", "widget", FieldMode.INCLUDE_ONLY, ["id", "name"], []
        )

    def test_the_endpoint_path_is_the_query_text_and_values_stay_parameters(self) -> None:
        # Interpolating a watermark into the path would be the injection this avoids.
        connector, _ = _connector()
        query = connector.build_extraction_query(
            self._contract(connector), LoadType.INCREMENTAL, None, "2026-01-01", None, 7
        )
        assert query.query_text == "/crm/v3/objects/widget"
        assert query.query_parameters["updated_after"] == "2026-01-01"

    def test_a_full_load_carries_no_watermark_bounds(self) -> None:
        connector, _ = _connector()
        query = connector.build_extraction_query(
            self._contract(connector), LoadType.FULL, None, "2026-01-01", "2026-02-01", 7
        )
        assert "updated_after" not in query.query_parameters
        assert "updated_before" not in query.query_parameters

    def test_the_discovered_fields_are_requested_from_the_source(self) -> None:
        connector, _ = _connector()
        query = connector.build_extraction_query(
            self._contract(connector), LoadType.FULL, None, None, None, 7
        )
        assert query.query_parameters["properties"] == "id,name"

    def test_a_report_entity_sends_metrics_and_dimensions(self) -> None:
        connector, _ = _connector(entity_id="spend-report")
        contract = connector.discover_queryable_fields(
            "substrate-test", "spend-report", FieldMode.INCLUDE_ONLY, ["cost"], []
        )
        query = connector.build_extraction_query(contract, LoadType.FULL, None, None, None, 7)
        assert query.query_parameters["metrics"] == "cost,clicks"
        assert query.query_parameters["dimensions"] == "date"
        assert "properties" not in query.query_parameters

    def test_the_spec_watermark_field_is_used_when_the_config_names_none(self) -> None:
        connector, _ = _connector()
        query = connector.build_extraction_query(
            self._contract(connector), LoadType.INCREMENTAL, None, None, None, 7
        )
        assert query.watermark_field == "updatedAt"


class TestConnectorExtraction:
    def test_records_are_yielded_with_their_source_timestamp(self) -> None:
        connector, _ = _connector(
            [
                _FakeHttpResponse(
                    200, {"results": [{"id": "1", "updatedAt": "2026-01-02T00:00:00Z"}]}
                )
            ]
        )
        query = connector.build_extraction_query(
            connector.discover_queryable_fields(
                "substrate-test", "widget", FieldMode.INCLUDE_ONLY, ["id"], []
            ),
            LoadType.INCREMENTAL,
            "updatedAt",
            None,
            None,
            7,
        )
        records = list(connector.execute_extraction(query, "run-1"))
        assert len(records) == 1
        assert records[0].source_timestamp == "2026-01-02T00:00:00Z"

    def test_pagination_continues_until_a_short_page(self) -> None:
        connector, _ = _connector(
            [
                # The first response is consumed by field discovery below.
                _FakeHttpResponse(200, {"results": [{"id": "0"}]}),
                _FakeHttpResponse(200, {"results": [{"id": "1"}, {"id": "2"}]}),
                _FakeHttpResponse(200, {"results": [{"id": "3"}]}),
            ]
        )
        query = connector.build_extraction_query(
            connector.discover_queryable_fields(
                "substrate-test", "widget", FieldMode.INCLUDE_ONLY, ["id"], []
            ),
            LoadType.FULL,
            None,
            None,
            None,
            7,
        )
        records = list(connector.execute_extraction(query, "run-1"))
        assert [r.payload["id"] for r in records] == ["1", "2", "3"]
        assert connector.pages_fetched == 2

    def test_a_hubspot_style_nested_cursor_is_followed(self) -> None:
        from connector_runtime.adapters.rest_api.rest_api_connector import _next_cursor

        response = RestResponse(200, {"paging": {"next": {"after": "abc"}}}, {})
        assert _next_cursor(response) == "abc"

    def test_a_flat_cursor_field_is_recognised(self) -> None:
        from connector_runtime.adapters.rest_api.rest_api_connector import _next_cursor

        assert _next_cursor(RestResponse(200, {"nextPageToken": "t"}, {})) == "t"

    def test_no_cursor_yields_none(self) -> None:
        from connector_runtime.adapters.rest_api.rest_api_connector import _next_cursor

        assert _next_cursor(RestResponse(200, {"results": []}, {})) is None
        assert _next_cursor(RestResponse(200, ["not-a-mapping"], {})) is None


class TestConnectorWriteBack:
    def test_write_back_is_refused_for_an_entity_that_declares_no_path(self) -> None:
        connector, _ = _connector(entity_id="readonly")
        writeback_session, _ = _session()
        with pytest.raises(SourceCapabilityUnavailableError, match="opt-in"):
            connector.write_back([{"id": "1"}], writeback_session)

    def test_a_record_without_an_external_id_is_refused(self) -> None:
        # An upsert with no external id would create a duplicate on every retry.
        connector, _ = _connector()
        writeback_session, transport = _session()
        with pytest.raises(RestSourceRequestError, match="external id"):
            connector.write_back([{"name": "a"}], writeback_session)
        assert transport.calls == []

    def test_an_empty_external_id_is_refused(self) -> None:
        connector, _ = _connector()
        writeback_session, _ = _session()
        with pytest.raises(RestSourceRequestError):
            connector.write_back([{"id": ""}], writeback_session)

    def test_the_external_id_addresses_the_record_and_is_not_resent_in_the_body(self) -> None:
        connector, _ = _connector()
        writeback_session, transport = _session([_FakeHttpResponse(200, {})])
        assert connector.write_back([{"id": "42", "name": "a"}], writeback_session) == 1
        call = transport.calls[0]
        assert call["method"] == "PATCH"
        assert call["url"].endswith("/crm/v3/objects/widget/42")
        assert call["json"] == {"name": "a"}

    def test_every_record_is_written(self) -> None:
        connector, _ = _connector()
        writeback_session, transport = _session([_FakeHttpResponse(200, {})])
        written = connector.write_back([{"id": "1"}, {"id": "2"}, {"id": "3"}], writeback_session)
        assert written == 3
        assert len(transport.calls) == 3


class TestConnectorErrorClassification:
    def test_a_missing_vendor_endpoint_is_a_configuration_fact_not_an_outage(self) -> None:
        connector, _ = _connector()
        assert (
            connector.classify_extraction_error(
                SourceCapabilityUnavailableError("no such endpoint")
            )
            is ExtractionErrorClassification.DETERMINISTIC_INVALID_CONFIGURATION
        )

    def test_a_throttle_classifies_as_a_throttle(self) -> None:
        connector, _ = _connector()
        assert (
            connector.classify_extraction_error(RestSourceThrottledError("429"))
            is ExtractionErrorClassification.TRANSIENT_THROTTLE
        )

    def test_bad_credentials_classify_deterministically(self) -> None:
        connector, _ = _connector()
        assert (
            connector.classify_extraction_error(RestSourceCredentialError("401"))
            is ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS
        )

    def test_an_unrecognised_exception_is_unknown_rather_than_guessed(self) -> None:
        connector, _ = _connector()
        assert (
            connector.classify_extraction_error(ValueError("?"))
            is ExtractionErrorClassification.UNKNOWN
        )


class TestAllowlistDeclarationSemantics:
    def test_a_source_declaring_no_hostnames_is_unrestricted_by_declaration(self) -> None:
        # Documented deliberately: the gap is visible in the registry, not implied by omission.
        declaration = SourceCapabilityDeclaration(
            source_id="unrestricted-test",
            display_name="Unrestricted Test",
            capabilities=frozenset({SourceCapability.INCREMENTAL}),
            allowed_hostnames=(),
        )
        source_capability_registry.register(declaration)
        try:
            from connector_runtime.source_capabilities import enforce_allowed_host

            enforce_allowed_host("unrestricted-test", "anywhere.example.com")
        finally:
            source_capability_registry._declarations.pop("unrestricted-test", None)

    def test_hostname_comparison_is_case_insensitive(self) -> None:
        from connector_runtime.source_capabilities import enforce_allowed_host

        enforce_allowed_host("substrate-test", "API.EXAMPLE.COM")


class TestFetchPageContract:
    def test_a_page_carries_the_response_headers_for_the_rate_limit_policy(self) -> None:
        connector, _ = _connector(
            [_FakeHttpResponse(200, {"results": [{"id": "1"}]}, {"X-RateLimit-Remaining": "5"})]
        )
        page = connector._fetch_page({})
        assert page.headers["X-RateLimit-Remaining"] == "5"

    def test_an_absolute_url_parameter_overrides_the_entity_path(self) -> None:
        connector, transport = _connector([_FakeHttpResponse(200, {"results": []})])
        connector._fetch_page({"url": "https://api.example.com/next-page"})
        assert transport.calls[0]["url"] == "https://api.example.com/next-page"

    def test_the_url_parameter_is_not_forwarded_as_a_query_parameter(self) -> None:
        connector, transport = _connector([_FakeHttpResponse(200, {"results": []})])
        connector._fetch_page({"url": "https://api.example.com/next-page", "limit": 2})
        assert "url" not in transport.calls[0]["params"]
        assert transport.calls[0]["params"] == {"limit": 2}


class TestJsonPathTypes:
    def test_records_json_path_accepts_a_mapping_body(self) -> None:
        assert isinstance(_ENTITY.records_json_path, tuple)

    def test_static_query_parameters_default_to_an_empty_mapping(self) -> None:
        assert isinstance(_READ_ONLY_ENTITY.static_query_parameters, Mapping)
        assert dict(_READ_ONLY_ENTITY.static_query_parameters) == {}
