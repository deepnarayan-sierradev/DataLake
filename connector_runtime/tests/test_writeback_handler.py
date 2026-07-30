"""
Tests for the write-back stage (DL-CONN-02).

The security property under test is that writes are genuinely opt-in and separately
credentialled: an active read config must never imply write access, and the write path must
resolve a different secret than the read path.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from connector_runtime import writeback_handler
from connector_runtime.writeback_handler import WritebackNotEnabledError, lambda_handler


class _NullContext:
    def get_remaining_time_in_millis(self) -> int:
        return 300_000


def _event(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tenant_code": "demo",
        "source_id": "hubspot",
        "entity_id": "hubspot-contact",
        "records": [{"Id": "1"}],
    }
    payload.update(overrides)
    return payload


class TestEventValidation:
    def test_a_complete_event_is_accepted(self) -> None:
        writeback_handler._validate_event(_event())

    @pytest.mark.parametrize("field", ["tenant_code", "source_id", "entity_id", "records"])
    def test_every_required_field_is_required(self, field: str) -> None:
        event = _event()
        del event[field]
        with pytest.raises(ValueError, match="missing required fields"):
            writeback_handler._validate_event(event)

    def test_a_malformed_tenant_code_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="tenant code format"):
            writeback_handler._validate_event(_event(tenant_code="../etc"))

    def test_a_malformed_source_id_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            writeback_handler._validate_event(_event(source_id="../x"))

    def test_a_malformed_connection_id_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            writeback_handler._validate_event(_event(connection_id="../x"))

    def test_an_absent_connection_id_is_allowed(self) -> None:
        writeback_handler._validate_event(_event(connection_id=None))

    def test_an_empty_record_list_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty list"):
            writeback_handler._validate_event(_event(records=[]))

    def test_a_non_list_records_field_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty list"):
            writeback_handler._validate_event(_event(records={"Id": "1"}))

    def test_a_batch_above_the_per_invocation_cap_is_rejected(self) -> None:
        oversized = [{"Id": str(index)} for index in range(1_001)]
        with pytest.raises(ValueError, match="above the per-invocation cap"):
            writeback_handler._validate_event(_event(records=oversized))

    def test_a_batch_at_the_cap_is_accepted(self) -> None:
        writeback_handler._validate_event(
            _event(records=[{"Id": str(index)} for index in range(1_000)])
        )

    def test_a_non_object_record_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="JSON object"):
            writeback_handler._validate_event(_event(records=["not-an-object"]))


class _Config:
    def __init__(self, *, writeback_enabled: bool) -> None:
        self.writeback_enabled = writeback_enabled
        self.rate_limit_policy = None


class _ConfigClient:
    def __init__(self, config: _Config) -> None:
        self._config = config
        self.loaded: list[dict[str, Any]] = []

    def load_config(self, source_id: str, entity_id: str, tenant_code: str, **kwargs: Any) -> Any:
        self.loaded.append(
            {"source_id": source_id, "entity_id": entity_id, "tenant_code": tenant_code, **kwargs}
        )
        return self._config


class _Coordinator:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.stages: list[dict[str, Any]] = []

    def emit_stage(self, **kwargs: Any) -> None:
        self.stages.append(kwargs)


class _Connector:
    def __init__(self, written: int = 0, error: Exception | None = None, **kwargs: Any) -> None:
        self.written = written
        self.error = error
        self.kwargs = kwargs

    def write_back(self, records: list[dict[str, Any]], session: Any) -> int:
        if self.error is not None:
            raise self.error
        return self.written


class _Policy:
    connection_id = "hubspot"
    total_throttles = 2
    total_backoff_ms = 150.0


class _Registry:
    def resolve(self, name: str, connection_id: str) -> _Policy:
        return _Policy()

    def get(self, source_id: str) -> Any:
        class _Spec:
            default_rate_limit_policy = "rest-source-default"
            required_credential_keys = ("api_key",)
            display_name = "HubSpot"

        return _Spec()


@pytest.fixture
def _wired(monkeypatch: Any) -> dict[str, Any]:
    """Replace the handler's collaborators; the decision logic is what is under test."""
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("PLATFORM_ENVIRONMENT", "dev")
    state: dict[str, Any] = {
        "config": _Config(writeback_enabled=True),
        "connector": _Connector(written=3),
        "sessions": [],
    }

    def config_client(**kwargs: Any) -> _ConfigClient:
        client = _ConfigClient(state["config"])
        state["config_client"] = client
        return client

    def coordinator(**kwargs: Any) -> _Coordinator:
        created = _Coordinator(**kwargs)
        state["coordinator"] = created
        return created

    def session(*args: Any, **kwargs: Any) -> str:
        state["sessions"].append(args)
        return "writeback-session"

    monkeypatch.setattr(writeback_handler, "ConfigurationRepositoryClient", config_client)
    monkeypatch.setattr(writeback_handler, "RunCoordinator", coordinator)
    monkeypatch.setattr(writeback_handler, "_writeback_session", session)
    monkeypatch.setattr(writeback_handler, "rest_source_spec_registry", _Registry())
    monkeypatch.setattr(writeback_handler, "rate_limit_policy_registry", _Registry())
    monkeypatch.setattr(writeback_handler, "RestApiConnector", lambda **kwargs: state["connector"])
    return state


class TestOptInGate:
    def test_write_back_is_refused_when_the_entity_has_not_opted_in(
        self, _wired: dict[str, Any]
    ) -> None:
        _wired["config"] = _Config(writeback_enabled=False)
        with pytest.raises(WritebackNotEnabledError, match="opt-in"):
            lambda_handler(_event(), _NullContext())

    def test_nothing_is_written_when_the_gate_refuses(self, _wired: dict[str, Any]) -> None:
        _wired["config"] = _Config(writeback_enabled=False)
        _wired["connector"] = _Connector(written=99)
        with pytest.raises(WritebackNotEnabledError):
            lambda_handler(_event(), _NullContext())
        assert _wired["sessions"] == []


class TestSuccessPath:
    def test_the_written_count_is_returned(self, _wired: dict[str, Any]) -> None:
        result = lambda_handler(_event(), _NullContext())
        assert result["records_written"] == 3
        assert result["rate_limit_hits"] == 2

    def test_a_run_id_is_generated_when_the_event_omits_one(self, _wired: dict[str, Any]) -> None:
        result = lambda_handler(_event(), _NullContext())
        assert result["run_id"]

    def test_an_explicit_run_id_is_preserved(self, _wired: dict[str, Any]) -> None:
        result = lambda_handler(_event(run_id="run-supplied"), _NullContext())
        assert result["run_id"] == "run-supplied"

    def test_the_config_is_loaded_for_the_resolved_connection(self, _wired: dict[str, Any]) -> None:
        lambda_handler(_event(connection_id="hubspot-west"), _NullContext())
        assert _wired["config_client"].loaded[0]["connection_id"] == "hubspot-west"

    def test_the_default_connection_resolves_to_the_source_id(self, _wired: dict[str, Any]) -> None:
        lambda_handler(_event(), _NullContext())
        assert _wired["config_client"].loaded[0]["connection_id"] == "hubspot"

    def test_a_successful_write_is_audited(self, _wired: dict[str, Any]) -> None:
        lambda_handler(_event(), _NullContext())
        assert _wired["coordinator"].stages[0]["status"].value == "success"
        assert _wired["coordinator"].stages[0]["record_count"] == 3

    def test_the_audit_record_is_tenant_scoped(self, _wired: dict[str, Any]) -> None:
        lambda_handler(_event(), _NullContext())
        assert _wired["coordinator"].kwargs["tenant_code"] == "demo"


class TestFailurePath:
    def test_a_write_failure_is_audited_and_re_raised(self, _wired: dict[str, Any]) -> None:
        _wired["connector"] = _Connector(error=RuntimeError("source rejected the upsert"))
        with pytest.raises(RuntimeError, match="source rejected"):
            lambda_handler(_event(), _NullContext())
        failure = _wired["coordinator"].stages[0]
        assert failure["status"].value == "failed"
        assert failure["error_code"] == "writeback_failed"

    def test_an_invalid_event_is_rejected_before_any_credential_is_read(
        self, _wired: dict[str, Any]
    ) -> None:
        with pytest.raises(ValueError):
            lambda_handler(_event(tenant_code="../etc"), _NullContext())
        assert _wired["sessions"] == []


class TestCredentialSeparation:
    def test_the_write_path_asks_for_a_write_back_secret(self) -> None:
        source = inspect.getsource(writeback_handler._writeback_session)
        assert "write_back=True" in source
        assert "allow_legacy_fallback=False" in source
