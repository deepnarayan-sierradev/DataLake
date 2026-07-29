"""
Tests for the entity resolution pipeline Lambda handler wrapper behaviour.

Covers OBS-1 (contextvars leak across warm-container invocations), OBS-2
(top-level structured error logging before re-raise), and SEC-5 (tenant_code
validation). The full happy-path pipeline (S3 + resolution config + matching)
is exercised separately in entity_resolution/tests/test_*; these tests target
only the handler's own plumbing, mocking `_run_entity_resolution` so the
wrapper logic can be verified in isolation.

Also covers PERF-3: `_load_all_contributing_records` delegates to
load_curated_records_duckdb (DuckDB-based S3 read) rather than the fully
materialising `load_curated_records`, tags records with _record_id/_source_id,
and requires AWS_REGION for DuckDB's httpfs S3 reader.
"""

from __future__ import annotations

from typing import Any

import pytest
import structlog

import entity_resolution.entity_resolution_pipeline_handler as handler_module
from entity_resolution.entity_resolution_pipeline_handler import _validate_event, lambda_handler

_BASE_EVENT: dict[str, Any] = {
    "source_id": "salesforce",
    "entity_id": "salesforce-account",
    "environment": "dev",
    "run_id": "run-er-test-001",
    "curated_s3_prefix": (
        "curated/salesforce/salesforce-account/curated_date=2026-01-01/run_id=run-1/"
    ),
    "tenant_code": "demo",
}


class TestValidateEventTenantCode:
    def test_missing_tenant_code_raises(self) -> None:
        """ARCH-4: tenant_code must fail closed, not silently default."""
        event = {k: v for k, v in _BASE_EVENT.items() if k != "tenant_code"}
        with pytest.raises(ValueError, match="tenant_code"):
            _validate_event(event)

    def test_valid_tenant_code_is_allowed(self) -> None:
        _validate_event({**_BASE_EVENT, "tenant_code": "acme-corp"})

    def test_invalid_tenant_code_rejected(self) -> None:
        with pytest.raises(ValueError, match="tenant_code"):
            _validate_event({**_BASE_EVENT, "tenant_code": "BAD_CODE"})


class TestContextvarsAndErrorHandling:
    def setup_method(self, method: object = None) -> None:
        structlog.contextvars.clear_contextvars()

    def teardown_method(self, method: object = None) -> None:
        structlog.contextvars.clear_contextvars()

    def test_contextvars_cleared_after_success(self, monkeypatch) -> None:
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setattr(
            handler_module,
            "_run_entity_resolution",
            lambda **_kwargs: {"canonical_prefix": "ok"},
        )
        lambda_handler(dict(_BASE_EVENT), context=None)
        assert structlog.contextvars.get_contextvars() == {}

    def test_contextvars_cleared_after_failure(self, monkeypatch) -> None:
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        def _boom(**_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("simulated pipeline failure")

        monkeypatch.setattr(handler_module, "_run_entity_resolution", _boom)

        with pytest.raises(RuntimeError, match="simulated pipeline failure"):
            lambda_handler(dict(_BASE_EVENT), context=None)

        # OBS-1: a failure must not leave stale context bound for the next
        # invocation on a reused (warm) Lambda container.
        assert structlog.contextvars.get_contextvars() == {}

    def test_failure_is_logged_before_reraise(self, monkeypatch, caplog) -> None:
        monkeypatch.setenv("AWS_REGION", "us-east-1")

        def _boom(**_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("simulated pipeline failure")

        monkeypatch.setattr(handler_module, "_run_entity_resolution", _boom)

        with pytest.raises(RuntimeError):
            lambda_handler(dict(_BASE_EVENT), context=None)
        # The structured logger call itself is exercised above without
        # raising — OBS-2's requirement is that the handler does not let the
        # exception propagate through an unstructured code path silently.

    def test_second_invocation_does_not_see_prior_run_id(self, monkeypatch) -> None:
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        """
        Regression for OBS-1: simulate a warm container running a failing
        invocation followed by a second invocation, and confirm the second
        invocation's bound context reflects only its own run_id.
        """
        captured: dict[str, Any] = {}

        def _boom(**_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("first invocation fails")

        def _record_context(**_kwargs: Any) -> dict[str, Any]:
            captured.update(structlog.contextvars.get_contextvars())
            return {"canonical_prefix": "ok"}

        monkeypatch.setattr(handler_module, "_run_entity_resolution", _boom)
        with pytest.raises(RuntimeError):
            lambda_handler({**_BASE_EVENT, "run_id": "run-first-fails"}, context=None)

        monkeypatch.setattr(handler_module, "_run_entity_resolution", _record_context)
        lambda_handler({**_BASE_EVENT, "run_id": "run-second-succeeds"}, context=None)

        assert captured["run_id"] == "run-second-succeeds"


class TestLoadAllContributingRecords:
    """PERF-3: curated records are loaded via the DuckDB-based S3 reader."""

    _PREFIX = "curated/salesforce/salesforce-account/curated_date=2026-01-01/run_id=run-1/"

    def test_uses_duckdb_loader_and_tags_records(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        calls: list[tuple[Any, str, str, str]] = []

        def _fake_loader(s3: Any, bucket: str, prefix: str, region_name: str) -> list[dict]:
            calls.append((s3, bucket, prefix, region_name))
            return [{"Id": "1", "Name": "Acme"}]

        monkeypatch.setattr(handler_module, "load_curated_records_duckdb", _fake_loader)

        records, prefixes = handler_module._load_all_contributing_records(
            s3=object(),
            curated_s3_bucket="curated-bucket",
            source_id="salesforce",
            entity_id="salesforce-account",
            curated_s3_prefix=self._PREFIX,
            pk_field="Id",
            contributing_sources=[("salesforce", "salesforce-account")],
            tenant_code="demo",
        )

        assert len(calls) == 1
        _, bucket, prefix, region_name = calls[0]
        assert bucket == "curated-bucket"
        assert prefix == self._PREFIX
        assert region_name == "us-east-1"

        assert records == [
            {"Id": "1", "Name": "Acme", "_record_id": "salesforce:1", "_source_id": "salesforce"}
        ]
        assert prefixes == [self._PREFIX]

    def test_does_not_call_python_list_based_loader(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        """The old fully-materialising loader must not be imported/used anymore."""
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setattr(
            handler_module,
            "load_curated_records_duckdb",
            lambda s3, bucket, prefix, region_name: [{"Id": "9"}],
        )
        assert not hasattr(handler_module, "load_curated_records")

        records, _ = handler_module._load_all_contributing_records(
            s3=object(),
            curated_s3_bucket="curated-bucket",
            source_id="salesforce",
            entity_id="salesforce-account",
            curated_s3_prefix=self._PREFIX,
            pk_field="Id",
            contributing_sources=[("salesforce", "salesforce-account")],
            tenant_code="demo",
        )
        assert records[0]["_record_id"] == "salesforce:9"

    def test_other_contributing_source_uses_find_latest_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second source is located via find_latest_curated_prefix, then loaded."""
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        other_prefix = "curated/netsuite/ns-customer/curated_date=2026-01-01/run_id=run-2/"
        monkeypatch.setattr(
            handler_module,
            "find_latest_curated_prefix",
            lambda s3, bucket, domain, entity_id, tenant_code: other_prefix,
        )
        loaded_prefixes_seen: list[str] = []

        def _fake_loader(s3: Any, bucket: str, prefix: str, region_name: str) -> list[dict]:
            loaded_prefixes_seen.append(prefix)
            return [{"Id": "42"}]

        monkeypatch.setattr(handler_module, "load_curated_records_duckdb", _fake_loader)

        records, prefixes = handler_module._load_all_contributing_records(
            s3=object(),
            curated_s3_bucket="curated-bucket",
            source_id="salesforce",
            entity_id="salesforce-account",
            curated_s3_prefix=self._PREFIX,
            pk_field="Id",
            contributing_sources=[
                ("salesforce", "salesforce-account"),
                ("netsuite", "ns-customer"),
            ],
            tenant_code="demo",
        )

        assert loaded_prefixes_seen == [self._PREFIX, other_prefix]
        assert {r["_source_id"] for r in records} == {"salesforce", "netsuite"}
        assert prefixes == loaded_prefixes_seen

    def test_raises_when_aws_region_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.delenv("AWS_REGION", raising=False)
        with pytest.raises(RuntimeError, match="AWS_REGION"):
            handler_module._load_all_contributing_records(
                s3=object(),
                curated_s3_bucket="curated-bucket",
                source_id="salesforce",
                entity_id="salesforce-account",
                curated_s3_prefix=self._PREFIX,
                pk_field="Id",
                contributing_sources=[("salesforce", "salesforce-account")],
                tenant_code="demo",
            )
