"""
Tests for the substrate extensions the real vendor documentation forced (2026-07-29).

Every case here exists because a *published* API needs it, and each one has a negative
control — the assertion has to be able to fail. The prior generation of specs passed every
test in `test_rest_api_substrate.py` while being unable to page MaidCentral, unwrap a
WellSky bundle, or authenticate to ServiceBridge, because the substrate only ever exercised
its own defaults.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from connector_runtime.adapters.rest_api.rest_adapter_registration import (
    RestSourceParams,
    UndeclaredEntityError,
    resolve_entity_spec,
)
from connector_runtime.adapters.rest_api.rest_api_connector import RestApiConnector
from connector_runtime.adapters.rest_api.rest_http_session import (
    RestHttpSession,
    RestResponse,
    RestSourceCredentialError,
)
from connector_runtime.adapters.rest_api.rest_source_spec import (
    AuthKind,
    RestEntitySpec,
    RestSourceSpec,
    TokenGrantKind,
)
from connector_runtime.adapters.rest_api.rest_token_exchange import (
    DEFAULT_TOKEN_LIFETIME_SECONDS,
    RestTokenExchange,
    TokenEndpointUnavailableError,
    TokenExchangeFailedError,
)
from connector_runtime.pagination import (
    PageNumberPagination,
    PaginationParameters,
    SingleRequestPagination,
    SourcePage,
    SourceRequest,
    pagination_strategy_registry,
)
from connector_runtime.rate_limiting import RateLimitPolicy
from connector_runtime.source_capabilities import (
    OutboundHostNotAllowedError,
    SourceCapability,
    SourceCapabilityDeclaration,
    SourceCapabilityUnavailableError,
    source_capability_registry,
)
from contracts.entity_configuration_contract import FieldMode, LoadType

_HOST = "api.substrate-ext.example.com"


class _CountingPolicy(RateLimitPolicy):
    def __init__(self) -> None:
        super().__init__(connection_id="ext-connection", sleep=lambda _: None)
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
    def __init__(self, responses: list[Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = responses or [_FakeHttpResponse(200, {"results": []})]

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture(autouse=True)
def _declared_source() -> Any:
    declaration = SourceCapabilityDeclaration(
        source_id="substrate-ext",
        display_name="Substrate Extensions",
        capabilities=frozenset({SourceCapability.INCREMENTAL}),
        allowed_hostnames=(_HOST,),
    )
    source_capability_registry.register(declaration)
    yield
    source_capability_registry._declarations.pop("substrate-ext", None)


def _spec(entities: tuple[RestEntitySpec, ...], **overrides: Any) -> RestSourceSpec:
    payload: dict[str, Any] = {
        "source_id": "substrate-ext",
        "display_name": "Substrate Extensions",
        "base_url": f"https://{_HOST}",
        "auth_kind": AuthKind.BEARER_TOKEN,
        "entities": entities,
        "capabilities": frozenset({SourceCapability.INCREMENTAL}),
    }
    payload.update(overrides)
    return RestSourceSpec(**payload)


def _pages_from(strategy_name: str, request: SourceRequest, pages: list[SourcePage]) -> list[Any]:
    seen: list[dict[str, Any]] = []
    queue = list(pages)

    def fetch(parameters: Any) -> SourcePage:
        seen.append(dict(parameters))
        return queue.pop(0) if queue else SourcePage(records=[])

    strategy = pagination_strategy_registry.resolve(strategy_name, fetch)
    list(strategy.pages(request))
    return seen


class TestPaginationParameterNaming:
    """MaidCentral pages on `skipCount`/`maxResultCount`, not `offset`/`limit`."""

    def test_offset_paging_uses_the_declared_names(self) -> None:
        request = SourceRequest(
            entity_id="e",
            page_size=2,
            parameters=PaginationParameters(offset="skipCount", limit="maxResultCount"),
        )
        seen = _pages_from(
            "offset_limit", request, [SourcePage(records=[{"a": 1}, {"a": 2}]), SourcePage([])]
        )
        assert seen[0] == {"skipCount": 0, "maxResultCount": 2}
        assert seen[1] == {"skipCount": 2, "maxResultCount": 2}

    def test_the_default_names_are_unchanged(self) -> None:
        seen = _pages_from("offset_limit", SourceRequest(entity_id="e", page_size=2), [])
        assert seen[0] == {"offset": 0, "limit": 2}

    def test_cursor_paging_uses_the_declared_cursor_name(self) -> None:
        request = SourceRequest(
            entity_id="e",
            page_size=1,
            parameters=PaginationParameters(cursor="pageToken", limit="pageSize"),
        )
        seen = _pages_from(
            "cursor",
            request,
            [SourcePage(records=[{"a": 1}], next_cursor="c1"), SourcePage(records=[])],
        )
        assert seen[0] == {"pageSize": 1}
        assert seen[1] == {"pageSize": 1, "pageToken": "c1"}

    def test_keyset_paging_uses_the_declared_names(self) -> None:
        request = SourceRequest(
            entity_id="e",
            page_size=1,
            keyset_field="id",
            parameters=PaginationParameters(keyset_after="sinceId", keyset_field="orderBy"),
        )
        seen = _pages_from(
            "keyset",
            request,
            [SourcePage(records=[{"id": 7}]), SourcePage(records=[{"id": 9}])],
        )
        assert seen[1]["sinceId"] == 7
        assert seen[1]["orderBy"] == "id"


class TestPageNumberPagination:
    """WellSky counts pages, not rows — offset paging would skip 99 of every 100."""

    def test_the_page_index_advances_by_one_not_by_the_row_count(self) -> None:
        request = SourceRequest(
            entity_id="e",
            page_size=2,
            parameters=PaginationParameters(page="_page", limit="_count"),
        )
        seen = _pages_from(
            "page_number",
            request,
            [SourcePage(records=[{"a": 1}, {"a": 2}]), SourcePage(records=[{"a": 3}])],
        )
        assert [call["_page"] for call in seen] == [0, 1]
        assert all(call["_count"] == 2 for call in seen)

    def test_a_one_based_provider_starts_at_one(self) -> None:
        request = SourceRequest(
            entity_id="e", page_size=1, parameters=PaginationParameters(first_page_index=1)
        )
        seen = _pages_from("page_number", request, [SourcePage(records=[{"a": 1}])])
        assert seen[0]["page"] == 1

    def test_a_short_page_ends_the_sweep(self) -> None:
        request = SourceRequest(entity_id="e", page_size=5)
        seen = _pages_from("page_number", request, [SourcePage(records=[{"a": 1}])])
        assert len(seen) == 1

    def test_it_is_registered_under_its_kind(self) -> None:
        strategy = pagination_strategy_registry.resolve(
            "page_number", lambda _: SourcePage(records=[])
        )
        assert isinstance(strategy, PageNumberPagination)


class TestSingleRequestPagination:
    """SeniorPlace and BePro's tracking endpoint document no paging at all."""

    def test_exactly_one_request_is_issued(self) -> None:
        seen = _pages_from(
            "single_request",
            SourceRequest(entity_id="e", page_size=2),
            [SourcePage(records=[{"a": 1}, {"a": 2}])],
        )
        assert len(seen) == 1

    def test_no_paging_parameter_is_invented(self) -> None:
        seen = _pages_from(
            "single_request",
            SourceRequest(entity_id="e", page_size=2, query_parameters={"officeId": "7"}),
            [SourcePage(records=[{"a": 1}])],
        )
        assert seen[0] == {"officeId": "7"}

    def test_it_is_registered_under_its_kind(self) -> None:
        strategy = pagination_strategy_registry.resolve(
            "single_request", lambda _: SourcePage(records=[])
        )
        assert isinstance(strategy, SingleRequestPagination)


class TestRecordUnwrapping:
    """A FHIR bundle nests each row under `resource`."""

    def test_the_wrapper_is_removed(self) -> None:
        response = RestResponse(
            200, {"entry": [{"resource": {"id": "1"}}, {"resource": {"id": "2"}}]}, {}
        )
        assert response.records(("entry",), "resource") == [{"id": "1"}, {"id": "2"}]

    def test_an_entry_without_the_wrapper_is_dropped_not_stored(self) -> None:
        response = RestResponse(200, {"entry": [{"resource": {"id": "1"}}, {"noise": 1}]}, {})
        assert response.records(("entry",), "resource") == [{"id": "1"}]

    def test_without_an_unwrap_field_the_envelope_is_returned_as_is(self) -> None:
        response = RestResponse(200, {"entry": [{"resource": {"id": "1"}}]}, {})
        assert response.records(("entry",)) == [{"resource": {"id": "1"}}]


class TestConfigDeclaredEntities:
    """
    An entity the console added, which this repo has never heard of (DL-CONN-21).

    Salesforce (`object_name`), MySQL (`table_name`) and NetSuite (`record_type`) have always
    taken their entity from configuration. The REST substrate required a code change, which
    contradicted the platform's configuration-driven premise; these tests hold the fix.
    """

    def _params(self, **overrides: Any) -> RestSourceParams:
        payload: dict[str, Any] = {"entity_id": "substrate-ext-quote"}
        payload.update(overrides)
        return RestSourceParams.model_validate(payload)

    def _source(self, **overrides: Any) -> RestSourceSpec:
        return _spec((RestEntitySpec(entity_id="declared", path="/v1/declared"),), **overrides)

    def test_a_declared_entity_still_wins_over_configuration(self) -> None:
        resolved = resolve_entity_spec(
            self._source(),
            self._params(entity_id="declared", entity_path="/v1/somewhere-else"),
        )
        assert resolved.path == "/v1/declared"

    def test_an_unknown_entity_with_a_path_is_extractable(self) -> None:
        resolved = resolve_entity_spec(self._source(), self._params(entity_path="/api/v2/quotes"))
        assert resolved.entity_id == "substrate-ext-quote"
        assert resolved.path == "/api/v2/quotes"

    def test_it_inherits_the_sources_envelope_and_page_size(self) -> None:
        source = self._source(
            default_records_json_path=("Results",),
            default_page_size=200,
            default_pagination_strategy="page_number",
        )
        resolved = resolve_entity_spec(source, self._params(entity_path="/api/v2/quotes"))
        assert resolved.records_json_path == ("Results",)
        assert resolved.page_size == 200
        assert resolved.pagination_strategy == "page_number"

    def test_the_console_can_override_every_inherited_convention(self) -> None:
        resolved = resolve_entity_spec(
            self._source(default_records_json_path=("Results",)),
            self._params(
                entity_path="/api/v2/quotes",
                entity_records_json_path="Result.Items",
                entity_watermark_field="DateLastModified",
                entity_natural_key_field="QuoteId",
                entity_pagination_strategy="page_number",
                page_size=500,
            ),
        )
        assert resolved.records_json_path == ("Result", "Items")
        assert resolved.watermark_field == "DateLastModified"
        assert resolved.natural_key_field == "QuoteId"
        assert resolved.pagination_strategy == "page_number"
        assert resolved.page_size == 500

    def test_an_empty_records_path_means_the_body_is_the_array(self) -> None:
        resolved = resolve_entity_spec(
            self._source(default_records_json_path=("results",)),
            self._params(entity_path="/api/v1/clients", entity_records_json_path=""),
        )
        assert resolved.records_json_path == ()

    def test_an_unknown_entity_without_a_path_names_what_to_supply(self) -> None:
        with pytest.raises(UndeclaredEntityError) as caught:
            resolve_entity_spec(self._source(), self._params())
        message = str(caught.value)
        assert "entity_path" in message
        assert "declared" in message

    def test_that_failure_is_deterministic_so_it_is_never_retried(self) -> None:
        error = UndeclaredEntityError("x")
        assert error.classification.name == "DETERMINISTIC_INVALID_CONFIGURATION"

    def test_the_connector_extracts_a_config_declared_entity_end_to_end(self) -> None:
        source = self._source(default_records_json_path=("Results",))
        entity = resolve_entity_spec(source, self._params(entity_path="/api/v2/quotes"))
        transport = _RecordingSession([_FakeHttpResponse(200, {"Results": [{"id": "q1"}]})])
        connector = RestApiConnector(
            source,
            entity.entity_id,
            RestHttpSession(source, {"access_token": "t"}, _CountingPolicy(), session=transport),
            _CountingPolicy(),
            entity=entity,
        )
        contract = connector.build_extraction_query(
            field_contract=connector.discover_queryable_fields(
                "substrate-ext", entity.entity_id, FieldMode.INCLUDE_ONLY, ["id"], []
            ),
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=7,
        )
        assert [r.payload for r in connector.execute_extraction(contract, run_id="r")] == [
            {"id": "q1"}
        ]
        assert connector.health_check() is True

    @pytest.mark.parametrize(
        "path",
        [
            "https://evil.example.com/x",
            "/v1/a?b=c",
            "/v1/a b",
            "/v1/a#frag",
            "v1/no-leading-slash",
            "//evil.example.com/x",
        ],
    )
    def test_a_malformed_or_absolute_path_is_refused(self, path: str) -> None:
        with pytest.raises(ValueError, match="not a safe endpoint path"):
            resolve_entity_spec(self._source(), self._params(entity_path=path))

    @pytest.mark.parametrize("path", ["/../../etc/passwd", "/v1/../admin", "/v1/a/../../b", "/.."])
    def test_a_parent_directory_segment_is_refused(self, path: str) -> None:
        with pytest.raises(ValueError, match="parent-directory segment"):
            resolve_entity_spec(self._source(), self._params(entity_path=path))

    def test_a_dot_inside_a_segment_is_still_allowed(self) -> None:
        resolved = resolve_entity_spec(self._source(), self._params(entity_path="/v1/report.json"))
        assert resolved.path == "/v1/report.json"

    def test_configuration_cannot_enable_writeback(self) -> None:
        resolved = resolve_entity_spec(self._source(), self._params(entity_path="/api/v2/quotes"))
        assert resolved.writeback_path is None
        assert resolved.supports_writeback is False

    def test_configuration_cannot_declare_a_writeback_path(self) -> None:
        with pytest.raises(ValidationError):
            RestSourceParams.model_validate(
                {"entity_id": "e", "entity_path": "/v1/e", "writeback_path": "/v1/e"}
            )

    @pytest.mark.parametrize("verb", ["DELETE", "PUT", "PATCH", "TRACE"])
    def test_configuration_cannot_issue_a_mutating_verb(self, verb: str) -> None:
        with pytest.raises(ValidationError):
            RestSourceParams.model_validate(
                {"entity_id": "e", "entity_path": "/v1/e", "entity_read_method": verb}
            )

    def test_a_config_declared_entity_still_obeys_the_host_allowlist(self) -> None:
        source = self._source()
        entity = resolve_entity_spec(source, self._params(entity_path="/api/v2/quotes"))
        session = RestHttpSession(
            source, {"access_token": "t"}, _CountingPolicy(), session=_RecordingSession()
        )
        with pytest.raises(OutboundHostNotAllowedError):
            session.get("https://evil.example.com/api/v2/quotes")
        assert entity.path == "/api/v2/quotes"

    def test_an_unknown_pagination_strategy_is_refused_at_resolve_time(self) -> None:
        entity = resolve_entity_spec(
            self._source(),
            self._params(entity_path="/v1/q", entity_pagination_strategy="make-it-up"),
        )
        with pytest.raises(KeyError):
            pagination_strategy_registry.resolve(
                str(entity.pagination_strategy), lambda _: SourcePage(records=[])
            )


class TestBareArrayResponses:
    """SeniorPlace returns the collection as the body itself, with no envelope."""

    def test_an_empty_json_path_reads_the_body_as_the_record_list(self) -> None:
        response = RestResponse(200, [{"id": "1"}, {"id": "2"}], {})
        assert response.records(()) == [{"id": "1"}, {"id": "2"}]

    def test_a_blank_response_yields_no_records_not_one_empty_one(self) -> None:
        assert RestResponse(200, {}, {}).records(()) == []

    def test_a_single_object_body_is_still_one_record(self) -> None:
        assert RestResponse(200, {"id": "1"}, {}).records(()) == [{"id": "1"}]


class TestPostSearchReads:
    """WellSky filters through a POST body, not a query string."""

    def _connector(self, transport: _RecordingSession) -> RestApiConnector:
        entity = RestEntitySpec(
            entity_id="patient",
            path="/v1/patients/_search/",
            read_method="POST",
            records_json_path=("entry",),
            record_unwrap_field="resource",
            watermark_field="updated",
            watermark_body_field="updated",
            watermark_comparator_prefix="ge",
            pagination_strategy="page_number",
            page_size=2,
            pagination_parameters=PaginationParameters(page="_page", limit="_count"),
        )
        spec = _spec((entity,))
        session = RestHttpSession(spec, {"access_token": "t"}, _CountingPolicy(), session=transport)
        return RestApiConnector(spec, "patient", session, _CountingPolicy())

    def test_the_watermark_is_bound_into_the_body_with_its_comparator(self) -> None:
        connector = self._connector(_RecordingSession())
        contract = connector.build_extraction_query(
            field_contract=connector.discover_queryable_fields(
                "substrate-ext", "patient", FieldMode.ALL, [], []
            ),
            load_type=LoadType.INCREMENTAL,
            watermark_field="updated",
            watermark_lower="2026-07-01T00:00:00",
            watermark_upper="2026-07-29T00:00:00",
            extraction_window_days=7,
        )
        assert contract.request_body == {"updated": "ge2026-07-01T00:00:00"}
        assert "updated_before" not in contract.query_parameters

    def test_the_read_is_issued_as_a_post_with_paging_in_the_query_string(self) -> None:
        transport = _RecordingSession(
            [_FakeHttpResponse(200, {"entry": [{"resource": {"id": "1"}}]})]
        )
        connector = self._connector(transport)
        contract = connector.build_extraction_query(
            field_contract=connector.discover_queryable_fields(
                "substrate-ext", "patient", FieldMode.ALL, [], []
            ),
            load_type=LoadType.INCREMENTAL,
            watermark_field="updated",
            watermark_lower="2026-07-01T00:00:00",
            watermark_upper=None,
            extraction_window_days=7,
        )
        records = list(connector.execute_extraction(contract, run_id="r"))
        read = [c for c in transport.calls if c["method"] == "POST"][-1]
        assert read["json"] == {"updated": "ge2026-07-01T00:00:00"}
        assert read["params"]["_page"] == 0
        assert read["params"]["_count"] == 2
        assert [r.payload for r in records] == [{"id": "1"}]

    def test_a_post_read_does_not_request_a_properties_projection(self) -> None:
        connector = self._connector(_RecordingSession())
        contract = connector.build_extraction_query(
            field_contract=connector.discover_queryable_fields(
                "substrate-ext", "patient", FieldMode.ALL, [], []
            ),
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=7,
        )
        assert "properties" not in contract.query_parameters

    def test_a_mutating_verb_is_refused_at_spec_time(self) -> None:
        with pytest.raises(ValueError, match="must be GET or POST"):
            RestEntitySpec(entity_id="e", path="/v1/e", read_method="DELETE")

    def test_a_body_watermark_on_a_get_entity_is_refused(self) -> None:
        with pytest.raises(ValueError, match="POST-search read"):
            RestEntitySpec(entity_id="e", path="/v1/e", watermark_body_field="updated")


class TestRequiredRunParameters:
    """BePro's match-scoped endpoints must fail closed, not reach the provider."""

    def _connector(self, parameters: dict[str, Any]) -> tuple[RestApiConnector, Any]:
        entity = RestEntitySpec(
            entity_id="tracking",
            path="/data-api/data/tracking",
            records_json_path=("data",),
            pagination_strategy="single_request",
            required_run_parameters=("match_id",),
        )
        spec = _spec((entity,))
        transport = _RecordingSession()
        session = RestHttpSession(spec, {"access_token": "t"}, _CountingPolicy(), session=transport)
        connector = RestApiConnector(spec, "tracking", session, _CountingPolicy())
        contract = connector.build_extraction_query(
            field_contract=connector.discover_queryable_fields(
                "substrate-ext", "tracking", FieldMode.INCLUDE_ONLY, ["id"], []
            ),
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=7,
        )
        contract.query_parameters.update(parameters)
        return connector, contract

    def test_a_missing_scope_is_a_configuration_error_not_a_transient_failure(self) -> None:
        connector, contract = self._connector({})
        with pytest.raises(SourceCapabilityUnavailableError, match="match_id"):
            list(connector.execute_extraction(contract, run_id="r"))

    def test_the_classification_is_deterministic_so_it_is_never_retried(self) -> None:
        connector, contract = self._connector({})
        try:
            list(connector.execute_extraction(contract, run_id="r"))
        except SourceCapabilityUnavailableError as exc:
            classification = connector.classify_extraction_error(exc)
        assert classification.name == "DETERMINISTIC_INVALID_CONFIGURATION"

    def test_a_supplied_scope_passes_the_guard(self) -> None:
        connector, contract = self._connector({"match_id": "1234"})
        assert list(connector.execute_extraction(contract, run_id="r")) == []


class TestSessionKeyQueryAuth:
    """ServiceBridge authenticates in the query string — and it must never be logged."""

    def _session(self) -> tuple[RestHttpSession, _RecordingSession]:
        spec = _spec(
            (RestEntitySpec(entity_id="c", path="/api/v2/customers"),),
            auth_kind=AuthKind.SESSION_KEY_QUERY,
            session_key_parameter="sessionKey",
            token_endpoint_path="/api/v1/login",
            token_grant_kind=TokenGrantKind.SESSION_LOGIN,
        )
        transport = _RecordingSession(
            [
                _FakeHttpResponse(200, {"sessionKey": "sk-live-abc", "expires_in": 1800}),
                _FakeHttpResponse(200, {"Results": []}),
            ]
        )
        return (
            RestHttpSession(
                spec,
                {"user_id": "api-user", "password": "hunter2"},
                _CountingPolicy(),
                session=transport,
            ),
            transport,
        )

    def test_the_session_key_travels_as_a_query_parameter(self) -> None:
        session, transport = self._session()
        session.get("/api/v2/customers")
        read = transport.calls[-1]
        assert read["params"]["sessionKey"] == "sk-live-abc"

    def test_the_session_key_is_never_sent_as_a_bearer_header(self) -> None:
        session, transport = self._session()
        session.get("/api/v2/customers")
        assert "Authorization" not in transport.calls[-1]["headers"]

    def test_the_session_key_never_reaches_a_log_line(self, caplog: Any) -> None:
        session, _transport = self._session()
        with caplog.at_level("INFO"):
            session.get("/api/v2/customers")
        assert "sk-live-abc" not in caplog.text
        assert "hunter2" not in caplog.text

    def test_a_bearer_source_does_not_gain_a_query_credential(self) -> None:
        spec = _spec((RestEntitySpec(entity_id="c", path="/v1/c"),))
        transport = _RecordingSession()
        RestHttpSession(spec, {"access_token": "t"}, _CountingPolicy(), session=transport).get(
            "/v1/c"
        )
        assert "sessionKey" not in transport.calls[-1]["params"]


class TestApiKeyValuePrefix:
    """SeniorPlace sends `Authorization: ApiKey <key>`."""

    def test_the_declared_prefix_is_applied(self) -> None:
        spec = _spec(
            (RestEntitySpec(entity_id="c", path="/api/v1/clients"),),
            auth_kind=AuthKind.API_KEY_HEADER,
            api_key_header_name="Authorization",
            api_key_value_prefix="ApiKey ",
        )
        transport = _RecordingSession()
        RestHttpSession(spec, {"api_key": "sk_live_1"}, _CountingPolicy(), session=transport).get(
            "/api/v1/clients"
        )
        assert transport.calls[-1]["headers"]["Authorization"] == "ApiKey sk_live_1"

    def test_no_prefix_is_added_when_none_is_declared(self) -> None:
        spec = _spec(
            (RestEntitySpec(entity_id="c", path="/v1/c"),),
            auth_kind=AuthKind.API_KEY_HEADER,
            api_key_header_name="X-Api-Key",
        )
        transport = _RecordingSession()
        RestHttpSession(spec, {"api_key": "k"}, _CountingPolicy(), session=transport).get("/v1/c")
        assert transport.calls[-1]["headers"]["X-Api-Key"] == "k"


class TestTokenExchange:
    """A one-hour token is shorter than a full sweep, so it must be re-issued mid-run."""

    def _spec_with_token(self, grant: TokenGrantKind) -> RestSourceSpec:
        return _spec(
            (RestEntitySpec(entity_id="c", path="/v1/c"),),
            auth_kind=AuthKind.OAUTH2_REFRESH,
            token_endpoint_path="/token",
            token_grant_kind=grant,
        )

    def test_the_password_grant_is_form_encoded(self) -> None:
        transport = _RecordingSession([_FakeHttpResponse(200, {"access_token": "at-1"})])
        exchange = RestTokenExchange(
            self._spec_with_token(TokenGrantKind.PASSWORD),
            {"username": "u", "password": "p"},
            session=transport,
        )
        assert exchange.token() == "at-1"
        call = transport.calls[0]
        assert call["data"] == {"grant_type": "password", "username": "u", "password": "p"}
        assert call["json"] is None
        assert call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"

    def test_a_refresh_token_is_preferred_over_replaying_the_password(self) -> None:
        transport = _RecordingSession([_FakeHttpResponse(200, {"access_token": "at-2"})])
        RestTokenExchange(
            self._spec_with_token(TokenGrantKind.PASSWORD),
            {"username": "u", "password": "p", "refresh_token": "rt"},
            session=transport,
        ).token()
        assert transport.calls[0]["data"]["grant_type"] == "refresh_token"

    def test_client_credentials_sends_the_client_pair(self) -> None:
        transport = _RecordingSession([_FakeHttpResponse(200, {"access_token": "at-3"})])
        RestTokenExchange(
            self._spec_with_token(TokenGrantKind.CLIENT_CREDENTIALS),
            {"client_id": "ci", "client_secret": "cs"},
            session=transport,
        ).token()
        assert transport.calls[0]["data"]["grant_type"] == "client_credentials"

    def test_a_session_login_posts_json_not_a_form(self) -> None:
        transport = _RecordingSession([_FakeHttpResponse(200, {"sessionKey": "sk"})])
        exchange = RestTokenExchange(
            self._spec_with_token(TokenGrantKind.SESSION_LOGIN),
            {"user_id": "u", "password": "p"},
            session=transport,
        )
        assert exchange.token() == "sk"
        assert transport.calls[0]["json"] == {"userId": "u", "password": "p"}

    def test_the_token_is_cached_until_the_renewal_margin(self) -> None:
        clock = [0.0]
        transport = _RecordingSession(
            [_FakeHttpResponse(200, {"access_token": "at", "expires_in": 3600})]
        )
        exchange = RestTokenExchange(
            self._spec_with_token(TokenGrantKind.CLIENT_CREDENTIALS),
            {"client_id": "ci", "client_secret": "cs"},
            session=transport,
            monotonic=lambda: clock[0],
        )
        exchange.token()
        clock[0] = 3_000.0
        exchange.token()
        assert exchange.exchanges_performed == 1

    def test_the_token_is_re_exchanged_inside_the_renewal_margin(self) -> None:
        clock = [0.0]
        transport = _RecordingSession(
            [_FakeHttpResponse(200, {"access_token": "at", "expires_in": 3600})]
        )
        exchange = RestTokenExchange(
            self._spec_with_token(TokenGrantKind.CLIENT_CREDENTIALS),
            {"client_id": "ci", "client_secret": "cs"},
            session=transport,
            monotonic=lambda: clock[0],
        )
        exchange.token()
        clock[0] = 3_590.0
        exchange.token()
        assert exchange.exchanges_performed == 2

    def test_a_missing_expiry_assumes_the_shortest_documented_lifetime(self) -> None:
        clock = [0.0]
        transport = _RecordingSession([_FakeHttpResponse(200, {"access_token": "at"})])
        exchange = RestTokenExchange(
            self._spec_with_token(TokenGrantKind.CLIENT_CREDENTIALS),
            {"client_id": "ci", "client_secret": "cs"},
            session=transport,
            monotonic=lambda: clock[0],
        )
        exchange.token()
        clock[0] = DEFAULT_TOKEN_LIFETIME_SECONDS
        exchange.token()
        assert exchange.exchanges_performed == 2

    @pytest.mark.parametrize("status", [400, 401, 403])
    def test_a_rejected_credential_is_deterministic(self, status: int) -> None:
        transport = _RecordingSession([_FakeHttpResponse(status, {})])
        exchange = RestTokenExchange(
            self._spec_with_token(TokenGrantKind.CLIENT_CREDENTIALS),
            {"client_id": "ci", "client_secret": "cs"},
            session=transport,
        )
        with pytest.raises(TokenExchangeFailedError):
            exchange.token()

    def test_a_failing_token_endpoint_is_transient(self) -> None:
        transport = _RecordingSession([_FakeHttpResponse(503, {})])
        exchange = RestTokenExchange(
            self._spec_with_token(TokenGrantKind.CLIENT_CREDENTIALS),
            {"client_id": "ci", "client_secret": "cs"},
            session=transport,
        )
        with pytest.raises(TokenEndpointUnavailableError):
            exchange.token()

    def test_a_missing_credential_key_names_the_key_and_not_its_value(self) -> None:
        exchange = RestTokenExchange(
            self._spec_with_token(TokenGrantKind.CLIENT_CREDENTIALS),
            {"client_id": "ci"},
            session=_RecordingSession(),
        )
        with pytest.raises(TokenExchangeFailedError, match="client_secret"):
            exchange.token()

    def test_the_credential_never_appears_in_the_failure_message(self) -> None:
        transport = _RecordingSession([_FakeHttpResponse(401, {"error": "bad cs-secret-value"})])
        exchange = RestTokenExchange(
            self._spec_with_token(TokenGrantKind.CLIENT_CREDENTIALS),
            {"client_id": "ci", "client_secret": "cs-secret-value"},
            session=transport,
        )
        with pytest.raises(TokenExchangeFailedError) as caught:
            exchange.token()
        assert "cs-secret-value" not in str(caught.value)

    def test_a_spec_declaring_a_grant_without_an_endpoint_is_refused(self) -> None:
        with pytest.raises(ValueError, match="only meaningful together"):
            _spec(
                (RestEntitySpec(entity_id="c", path="/v1/c"),),
                token_grant_kind=TokenGrantKind.PASSWORD,
            )

    def test_an_unsafe_token_endpoint_path_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not a safe endpoint path"):
            _spec(
                (RestEntitySpec(entity_id="c", path="/v1/c"),),
                token_endpoint_path="https://evil.example.com/token",
                token_grant_kind=TokenGrantKind.PASSWORD,
            )


class TestMidRunTokenExpiry:
    """A 401 on a token-exchanging source is retried once, and only once."""

    def _spec(self) -> RestSourceSpec:
        return _spec(
            (RestEntitySpec(entity_id="c", path="/v1/c"),),
            auth_kind=AuthKind.OAUTH2_REFRESH,
            token_endpoint_path="/token",
            token_grant_kind=TokenGrantKind.CLIENT_CREDENTIALS,
        )

    def test_an_expired_token_is_re_exchanged_and_the_read_succeeds(self) -> None:
        transport = _RecordingSession(
            [
                _FakeHttpResponse(200, {"access_token": "at-1", "expires_in": 3600}),
                _FakeHttpResponse(401, {}),
                _FakeHttpResponse(200, {"access_token": "at-2", "expires_in": 3600}),
                _FakeHttpResponse(200, {"results": [{"id": "1"}]}),
            ]
        )
        session = RestHttpSession(
            self._spec(),
            {"client_id": "ci", "client_secret": "cs"},
            _CountingPolicy(),
            session=transport,
        )
        response = session.get("/v1/c")
        assert response.records(("results",)) == [{"id": "1"}]

    def test_a_genuinely_revoked_credential_still_fails_deterministically(self) -> None:
        transport = _RecordingSession(
            [
                _FakeHttpResponse(200, {"access_token": "at-1", "expires_in": 3600}),
                _FakeHttpResponse(401, {}),
                _FakeHttpResponse(200, {"access_token": "at-2", "expires_in": 3600}),
                _FakeHttpResponse(401, {}),
            ]
        )
        session = RestHttpSession(
            self._spec(),
            {"client_id": "ci", "client_secret": "cs"},
            _CountingPolicy(),
            session=transport,
        )
        with pytest.raises(RestSourceCredentialError):
            session.get("/v1/c")

    def test_a_source_without_a_token_endpoint_does_not_retry_a_401(self) -> None:
        spec = _spec((RestEntitySpec(entity_id="c", path="/v1/c"),))
        transport = _RecordingSession([_FakeHttpResponse(401, {})])
        session = RestHttpSession(spec, {"access_token": "t"}, _CountingPolicy(), session=transport)
        with pytest.raises(RestSourceCredentialError):
            session.get("/v1/c")
        assert len(transport.calls) == 1


class TestErrorBodyHandling:
    """A 5xx HTML error page is an outage, not a broken JSON contract."""

    def test_a_non_json_error_body_stays_transient(self) -> None:
        class _HtmlResponse:
            status_code = 502
            text = "<html>bad gateway</html>"
            headers: dict[str, str] = {}

        spec = _spec((RestEntitySpec(entity_id="c", path="/v1/c"),))
        transport = _RecordingSession([_HtmlResponse()])
        session = RestHttpSession(spec, {"access_token": "t"}, _CountingPolicy(), session=transport)
        with pytest.raises(Exception) as caught:
            session.get("/v1/c")
        assert type(caught.value).__name__ == "RestSourceTransientError"
