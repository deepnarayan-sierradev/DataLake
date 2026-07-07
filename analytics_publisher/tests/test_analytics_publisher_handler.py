"""
Tests for the analytics publisher Lambda handler wrapper behaviour.

Covers OBS-1 (contextvars leak), OBS-2 (structured error logging before
re-raise), OBS-4 (end-to-end SLA metric), and SEC-5 (tenant_code validation).
The full happy-path pipeline (S3 read + Parquet write + Glue registration) is
exercised via integration/manual testing; these tests target only the
handler's own plumbing by mocking `_run_analytics_publication`.

TestPerf3SchemaReuse is the one exception: it drives `_run_analytics_publication`
end-to-end (with S3/Glue/EntityTypeRegistry/metrics faked out) specifically to
guard PERF-3 — the handler must reuse S3ParquetWriter.last_written_schema for
Glue column registration instead of re-materialising the full analytics_records
list into a second pa.Table just to recompute the same schema.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest
import structlog
from moto import mock_aws

import analytics_publisher.analytics_publisher_handler as handler_module
from analytics_publisher.analytics_publisher_handler import _validate_event, lambda_handler

_BASE_EVENT: dict[str, Any] = {
    "source_id": "salesforce",
    "entity_id": "salesforce-account",
    "environment": "dev",
    "run_id": "run-ap-test-001",
    "canonical_prefix": "entity-resolution/company/golden_date=2026-01-01/",
    "curated_s3_prefix": "curated/salesforce/salesforce-account/curated_date=2026-01-01/run_id=run-1/",
}


class TestValidateEventTenantCode:
    def test_missing_tenant_code_is_allowed(self) -> None:
        _validate_event(dict(_BASE_EVENT))

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
        monkeypatch.setattr(
            handler_module,
            "_run_analytics_publication",
            lambda **_kwargs: {"analytics_s3_prefix": "ok"},
        )
        lambda_handler(dict(_BASE_EVENT), context=None)
        assert structlog.contextvars.get_contextvars() == {}

    def test_contextvars_cleared_after_failure(self, monkeypatch) -> None:
        def _boom(**_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("simulated pipeline failure")

        monkeypatch.setattr(handler_module, "_run_analytics_publication", _boom)

        with pytest.raises(RuntimeError, match="simulated pipeline failure"):
            lambda_handler(dict(_BASE_EVENT), context=None)

        assert structlog.contextvars.get_contextvars() == {}


class TestEndToEndSlaMetric:
    """OBS-4: emit_stage_duration(stage='e2e_pipeline') when run_started_at is present."""

    def test_e2e_metric_emitted_when_run_started_at_present(self, monkeypatch) -> None:
        captured_stages: list[str] = []

        class _FakeMetricsEmitter:
            def __init__(self, region_name: str) -> None:
                pass

            def set_tenant_context(self, tenant_code: str) -> None:
                pass

            def emit_stage_duration(self, **kwargs: Any) -> None:
                captured_stages.append(kwargs["stage"])

            def emit_records_extracted(self, **kwargs: Any) -> None:
                pass

            def flush(self) -> None:
                pass

        monkeypatch.setattr(handler_module, "CloudWatchMetricsEmitter", _FakeMetricsEmitter)
        monkeypatch.setattr(handler_module, "require_env", lambda name: f"fake-{name.lower()}")

        started_at = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()

        # Directly exercise the metrics block via the private helper's inputs
        # by calling it with pre-baked outputs — avoids mocking S3/Glue.
        handler_module._emit_metrics_and_e2e_sla(
            region_name="us-east-1",
            tenant_code="demo",
            source_id="salesforce",
            entity_id="salesforce-account",
            environment="dev",
            stage_start_ms=0.0,
            record_count=10,
            run_started_at=started_at,
        )

        assert "analytics_publication" in captured_stages
        assert "e2e_pipeline" in captured_stages

    def test_e2e_metric_skipped_when_run_started_at_absent(self, monkeypatch) -> None:
        captured_stages: list[str] = []

        class _FakeMetricsEmitter:
            def __init__(self, region_name: str) -> None:
                pass

            def set_tenant_context(self, tenant_code: str) -> None:
                pass

            def emit_stage_duration(self, **kwargs: Any) -> None:
                captured_stages.append(kwargs["stage"])

            def emit_records_extracted(self, **kwargs: Any) -> None:
                pass

            def flush(self) -> None:
                pass

        monkeypatch.setattr(handler_module, "CloudWatchMetricsEmitter", _FakeMetricsEmitter)

        handler_module._emit_metrics_and_e2e_sla(
            region_name="us-east-1",
            tenant_code="demo",
            source_id="salesforce",
            entity_id="salesforce-account",
            environment="dev",
            stage_start_ms=0.0,
            record_count=10,
            run_started_at=None,
        )

        assert captured_stages == ["analytics_publication"]


# ---------------------------------------------------------------------------
# PERF-3 regression-guard fakes (module level to keep the test itself simple)
# ---------------------------------------------------------------------------


class _NoFromPylistTable:
    """Stand-in for pa.Table that fails the test if from_pylist is called
    directly from analytics_publisher_handler's own scope — the handler must
    reuse S3ParquetWriter.last_written_schema instead (PERF-3)."""

    @staticmethod
    def from_pylist(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(
            "analytics_publisher_handler must not call pa.Table.from_pylist "
            "directly — reuse S3ParquetWriter.last_written_schema (PERF-3)."
        )


class _PyarrowProxy:
    """Delegates everything except Table.from_pylist to the real pyarrow module."""

    Table = _NoFromPylistTable

    def __getattr__(self, name: str) -> Any:
        import pyarrow as real_pyarrow

        return getattr(real_pyarrow, name)


class _FakeEntityTypeRegistry:
    def __init__(self, environment: str, region_name: str) -> None:
        pass

    def get_entity_type(self, entity_id: str, tenant_code: str) -> str:
        return "company"


class _FakeGlueExceptions:
    # Named to match the real boto3 Glue client's exceptions.AlreadyExistsException
    # for duck-typed except-clause compatibility with the handler under test.
    class AlreadyExistsException(Exception):  # noqa: N818
        pass


class _FakeGlueClient:
    exceptions = _FakeGlueExceptions

    def get_table(self, DatabaseName: str, Name: str) -> dict[str, Any]:  # noqa: N803
        return {"Table": {"StorageDescriptor": {"Columns": [], "Location": "s3://x/"}}}

    def create_partition(self, **_kwargs: Any) -> dict[str, Any]:
        return {}


class _FakeCatalogResult:
    database_name = "edl_analytics"
    table_name = "company"
    operation = "created"


class _FakeCatalogClient:
    def __init__(self, region_name: str, captured_specs: list[Any]) -> None:
        self._captured_specs = captured_specs

    def register_dataset(self, spec: Any) -> Any:
        self._captured_specs.append(spec)
        return _FakeCatalogResult()


class _FakeMetricsEmitter:
    def __init__(self, region_name: str) -> None:
        pass

    def set_tenant_context(self, tenant_code: str) -> None:
        pass

    def emit_stage_duration(self, **kwargs: Any) -> None:
        pass

    def emit_records_extracted(self, **kwargs: Any) -> None:
        pass

    def flush(self) -> None:
        pass


class TestPerf3SchemaReuse:
    """
    PERF-3: `_run_analytics_publication` must reuse S3ParquetWriter's
    inferred schema (last_written_schema) for Glue column registration,
    instead of a second full pa.Table.from_pylist(analytics_records)
    materialisation. `pa.Table.from_pylist` is patched to raise if invoked
    from the handler's own module scope — a hard regression guard.
    """

    def test_run_analytics_publication_reuses_writer_schema(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(handler_module, "pa", _PyarrowProxy())

        env_values = {
            "AWS_REGION": "us-east-1",
            "ANALYTICS_S3_BUCKET": "analytics-bucket",
            "GLUE_CATALOG_DATABASE": "edl_analytics",
        }
        monkeypatch.setattr(handler_module, "require_env", lambda name: env_values[name])
        monkeypatch.setattr(handler_module, "EntityTypeRegistryClient", _FakeEntityTypeRegistry)

        golden_records = [
            {"golden_id": "g1", "name": "Acme", "_record_id": "sf:1", "_source_id": "salesforce"},
            {"golden_id": "g2", "name": "Globex", "_record_id": "sf:2", "_source_id": "salesforce"},
        ]
        monkeypatch.setattr(
            handler_module,
            "_load_parquet_records",
            lambda s3, bucket, prefix: golden_records,
        )

        captured_specs: list[Any] = []
        monkeypatch.setattr(
            handler_module,
            "DataCatalogRegistrationClient",
            lambda region_name: _FakeCatalogClient(region_name, captured_specs),
        )
        monkeypatch.setattr(handler_module, "CloudWatchMetricsEmitter", _FakeMetricsEmitter)

        fake_glue = _FakeGlueClient()
        with mock_aws():
            s3_client = boto3.client("s3", region_name="us-east-1")
            s3_client.create_bucket(Bucket="analytics-bucket")
            monkeypatch.setattr(
                handler_module.boto3,
                "client",
                lambda service_name, region_name=None: (
                    s3_client if service_name == "s3" else fake_glue
                ),
            )

            result = handler_module._run_analytics_publication(
                source_id="salesforce",
                entity_id="salesforce-account",
                environment="dev",
                run_id="run-001",
                canonical_prefix="entity-resolution/company/golden_date=2026-01-01/",
                tenant_code="demo",
                run_started_at=None,
                stage_start_ms=0.0,
            )

        assert result["record_count"] == 2
        assert len(captured_specs) == 1
        column_names = {c["Name"] for c in captured_specs[0].schema}
        # _record_id/_source_id are internal ER fields and must be stripped
        # before the schema reaches Glue registration.
        assert column_names == {"golden_id", "name"}
