"""Tests for the set-based engine interface + registry (FR-F0.1)."""

from __future__ import annotations

import pytest

from processing_engine.interfaces.set_based_engine_interface import (
    QueryOutput,
    SetBasedQueryEngine,
    SetBasedQueryError,
    validate_inputs,
    validate_output_target,
)
from processing_engine.registry import SetBasedEngineRegistry


class _StubEngine(SetBasedQueryEngine):
    def stream(self, *, sql, inputs, batch_size=50_000):
        yield []

    def materialize(self, *, sql, inputs, output_bucket, output_prefix):
        return QueryOutput(output_uri="s3://b/p/data.parquet", row_count=0)


class TestValidateInputs:
    def test_valid_inputs_pass(self):
        validate_inputs({"curated": "s3://datalake-curated-1/demo/curated/crm/acct"})

    def test_empty_inputs_rejected(self):
        with pytest.raises(SetBasedQueryError):
            validate_inputs({})

    def test_unsafe_view_name_rejected(self):
        with pytest.raises(SetBasedQueryError):
            validate_inputs({"Bad Name": "s3://b/p"})

    def test_non_s3_uri_rejected(self):
        with pytest.raises(SetBasedQueryError):
            validate_inputs({"v": "/local/path"})

    def test_traversal_prefix_rejected(self):
        with pytest.raises(ValueError):
            validate_inputs({"v": "s3://datalake-curated-1/../etc"})


class TestValidateOutputTarget:
    def test_valid_target_returns_object_uri(self):
        uri = validate_output_target("datalake-analytics-1", "demo/analytics/company")
        assert uri == "s3://datalake-analytics-1/demo/analytics/company/data.parquet"

    def test_unsafe_bucket_rejected(self):
        with pytest.raises(SetBasedQueryError):
            validate_output_target("Bad_Bucket", "demo/x")

    def test_traversal_prefix_rejected(self):
        with pytest.raises(ValueError):
            validate_output_target("datalake-analytics-1", "../etc")


class TestRegistry:
    def test_register_and_build(self):
        reg = SetBasedEngineRegistry()
        reg.register("stub")(_StubEngine)
        assert isinstance(reg.build("stub"), _StubEngine)

    def test_duplicate_registration_rejected(self):
        reg = SetBasedEngineRegistry()
        reg.register("stub")(_StubEngine)
        with pytest.raises(ValueError):
            reg.register("stub")(_StubEngine)

    def test_unknown_engine_rejected(self):
        reg = SetBasedEngineRegistry()
        with pytest.raises(ValueError):
            reg.build("nope")

    def test_known_engines(self):
        reg = SetBasedEngineRegistry()
        reg.register("stub")(_StubEngine)
        assert reg.known_engines() == frozenset({"stub"})
