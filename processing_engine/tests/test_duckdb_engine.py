"""Tests for the DuckDB set-based engine (FR-F0.1). DuckDB is mocked via sys.modules."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from processing_engine.engines.duckdb_engine import DuckDbSetBasedEngine
from processing_engine.interfaces.set_based_engine_interface import SetBasedQueryError
from processing_engine.registry import set_based_engine_registry

_INPUTS = {"curated": "s3://datalake-curated-1/demo/curated/crm/acct"}


def _install_mock_duckdb(monkeypatch, con):
    mock_duckdb = MagicMock()
    mock_duckdb.connect.return_value = con
    monkeypatch.setitem(sys.modules, "duckdb", mock_duckdb)
    return mock_duckdb


class TestRegistration:
    def test_duckdb_registered_and_buildable(self):
        assert "duckdb" in set_based_engine_registry.known_engines()
        engine = set_based_engine_registry.build("duckdb", region_name="us-east-1")
        assert isinstance(engine, DuckDbSetBasedEngine)


class TestStream:
    def test_stream_yields_batches(self, monkeypatch):
        con = MagicMock()
        batch = MagicMock()
        batch.to_pylist.return_value = [{"golden_id": "g1"}]
        con.execute.return_value.fetch_record_batch.return_value = iter([batch])
        _install_mock_duckdb(monkeypatch, con)

        engine = DuckDbSetBasedEngine(region_name="us-east-1")
        out = list(engine.stream(sql="SELECT * FROM curated", inputs=_INPUTS, batch_size=10))
        assert out == [[{"golden_id": "g1"}]]

    def test_connect_loads_aws_credentials_and_region(self, monkeypatch):
        con = MagicMock()
        batch = MagicMock()
        batch.to_pylist.return_value = []
        con.execute.return_value.fetch_record_batch.return_value = iter([batch])
        _install_mock_duckdb(monkeypatch, con)

        engine = DuckDbSetBasedEngine(region_name="eu-west-1")
        list(engine.stream(sql="SELECT 1", inputs=_INPUTS))
        calls = [str(c) for c in con.execute.call_args_list]
        assert any("load_aws_credentials" in c for c in calls)
        assert any("s3_region='eu-west-1'" in c for c in calls)

    def test_unsafe_inputs_rejected_before_connect(self, monkeypatch):
        con = MagicMock()
        mock_duckdb = _install_mock_duckdb(monkeypatch, con)
        engine = DuckDbSetBasedEngine(region_name="us-east-1")
        with pytest.raises(SetBasedQueryError):
            list(engine.stream(sql="SELECT 1", inputs={"bad name": "s3://b/p"}))
        mock_duckdb.connect.assert_not_called()

    def test_duckdb_unavailable_raises(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "duckdb", None)
        engine = DuckDbSetBasedEngine(region_name="us-east-1")
        with pytest.raises(SetBasedQueryError):
            list(engine.stream(sql="SELECT 1", inputs=_INPUTS))

    def test_execute_failure_wrapped(self, monkeypatch):
        con = MagicMock()
        con.execute.return_value.fetch_record_batch.side_effect = RuntimeError("boom")
        _install_mock_duckdb(monkeypatch, con)
        engine = DuckDbSetBasedEngine(region_name="us-east-1")
        with pytest.raises(SetBasedQueryError):
            list(engine.stream(sql="SELECT 1", inputs=_INPUTS))


class TestMaterialize:
    def test_materialize_writes_and_counts(self, monkeypatch):
        con = MagicMock()
        con.execute.return_value.fetchone.return_value = (3,)
        _install_mock_duckdb(monkeypatch, con)

        engine = DuckDbSetBasedEngine(region_name="us-east-1")
        result = engine.materialize(
            sql="SELECT * FROM curated",
            inputs=_INPUTS,
            output_bucket="datalake-analytics-1",
            output_prefix="demo/analytics/company",
        )
        assert result.row_count == 3
        assert result.output_uri == "s3://datalake-analytics-1/demo/analytics/company/data.parquet"
        assert any("COPY" in str(c) for c in con.execute.call_args_list)

    def test_unsafe_output_rejected_before_connect(self, monkeypatch):
        con = MagicMock()
        mock_duckdb = _install_mock_duckdb(monkeypatch, con)
        engine = DuckDbSetBasedEngine(region_name="us-east-1")
        with pytest.raises(SetBasedQueryError):
            engine.materialize(
                sql="SELECT 1", inputs=_INPUTS, output_bucket="Bad_Bucket", output_prefix="x"
            )
        mock_duckdb.connect.assert_not_called()
