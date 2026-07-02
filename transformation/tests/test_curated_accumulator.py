"""
Unit tests for CuratedAccumulator and the pure merge_records() function.

Tests cover:
  - First run (empty previous state) — delta written as-is
  - New records inserted into existing state
  - Existing records updated (latest value wins — SCD Type 1)
  - Soft-deleted records removed from merged state
  - Records missing the primary key skipped with no crash
  - Idempotency — re-applying same delta produces same result
  - Empty delta with non-empty previous state — previous state unchanged
  - Empty delta AND empty previous state — empty result
  - Soft-delete field is None — all records upserted, no deletions
  - CuratedAccumulator.accumulate() integration test with mocked S3
"""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from transformation.curated_accumulator import CuratedAccumulator, merge_records


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_parquet_bytes(records: list[dict[str, Any]]) -> bytes:
    """Serialize a list of dicts to Parquet bytes for mocking S3 responses."""
    table = pa.Table.from_pylist(records)
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# merge_records() — pure function tests (no I/O, no mocks)
# ---------------------------------------------------------------------------

class TestMergeRecordsFirstRun:
    def test_empty_previous_returns_delta(self):
        delta = [{"Id": "1", "Name": "Alice"}, {"Id": "2", "Name": "Bob"}]
        result = merge_records({}, delta, pk_field="Id")
        assert len(result) == 2
        names = {r["Name"] for r in result}
        assert names == {"Alice", "Bob"}

    def test_empty_previous_and_empty_delta_returns_empty(self):
        result = merge_records({}, [], pk_field="Id")
        assert result == []


class TestMergeRecordsUpsert:
    def test_new_record_inserted(self):
        previous = {"1": {"Id": "1", "Name": "Alice"}}
        delta = [{"Id": "2", "Name": "Bob"}]
        result = merge_records(previous, delta, pk_field="Id")
        result_by_id = {r["Id"]: r for r in result}
        assert "1" in result_by_id
        assert "2" in result_by_id

    def test_existing_record_updated(self):
        previous = {"1": {"Id": "1", "Name": "Alice", "City": "NYC"}}
        delta = [{"Id": "1", "Name": "Alice Smith", "City": "LA"}]
        result = merge_records(previous, delta, pk_field="Id")
        assert len(result) == 1
        assert result[0]["Name"] == "Alice Smith"
        assert result[0]["City"] == "LA"

    def test_partial_update_only_touches_changed_records(self):
        previous = {
            "1": {"Id": "1", "Name": "Alice"},
            "2": {"Id": "2", "Name": "Bob"},
            "3": {"Id": "3", "Name": "Carol"},
        }
        delta = [{"Id": "2", "Name": "Bobby"}]
        result = merge_records(previous, delta, pk_field="Id")
        result_by_id = {r["Id"]: r for r in result}
        assert result_by_id["1"]["Name"] == "Alice"   # unchanged
        assert result_by_id["2"]["Name"] == "Bobby"   # updated
        assert result_by_id["3"]["Name"] == "Carol"   # unchanged
        assert len(result) == 3

    def test_empty_delta_previous_state_unchanged(self):
        previous = {
            "1": {"Id": "1", "Name": "Alice"},
            "2": {"Id": "2", "Name": "Bob"},
        }
        result = merge_records(previous, [], pk_field="Id")
        assert len(result) == 2


class TestMergeRecordsSoftDelete:
    def test_soft_deleted_record_removed(self):
        previous = {
            "1": {"Id": "1", "Name": "Alice", "IsDelete": False},
            "2": {"Id": "2", "Name": "Bob", "IsDelete": False},
        }
        delta = [{"Id": "2", "Name": "Bob", "IsDelete": True}]
        result = merge_records(previous, delta, pk_field="Id", soft_delete_field="IsDelete")
        result_by_id = {r["Id"]: r for r in result}
        assert "1" in result_by_id
        assert "2" not in result_by_id

    def test_soft_delete_field_none_no_deletions(self):
        """When soft_delete_field is None, records with a delete flag are upserted."""
        previous = {"1": {"Id": "1", "Name": "Alice"}}
        delta = [{"Id": "1", "Name": "Alice", "IsDelete": True}]
        result = merge_records(previous, delta, pk_field="Id", soft_delete_field=None)
        assert len(result) == 1
        assert result[0]["IsDelete"] is True  # treated as normal upsert

    def test_soft_delete_truthy_values(self):
        """Various truthy representations all trigger deletion."""
        for truthy_value in (True, 1, "true", "1", "yes"):
            previous = {"x": {"Id": "x", "Name": "Test"}}
            delta = [{"Id": "x", "Name": "Test", "deleted": truthy_value}]
            result = merge_records(previous, delta, pk_field="Id", soft_delete_field="deleted")
            assert result == [], f"Expected deletion for delete_field={truthy_value!r}"

    def test_soft_delete_falsy_values_upsert(self):
        """Falsy delete field values result in upsert, not deletion."""
        for falsy_value in (False, 0, None):
            previous = {}
            delta = [{"Id": "x", "Name": "Test", "deleted": falsy_value}]
            result = merge_records(previous, delta, pk_field="Id", soft_delete_field="deleted")
            assert len(result) == 1, f"Expected upsert for delete_field={falsy_value!r}"

    def test_delete_non_existent_record_is_no_op(self):
        """Deleting a record not in previous state does not raise."""
        previous = {"1": {"Id": "1", "Name": "Alice"}}
        delta = [{"Id": "99", "Name": "Ghost", "IsDelete": True}]
        result = merge_records(previous, delta, pk_field="Id", soft_delete_field="IsDelete")
        assert len(result) == 1
        assert result[0]["Id"] == "1"


class TestMergeRecordsMissingPK:
    def test_records_without_pk_skipped(self):
        previous = {}
        delta = [
            {"Id": "1", "Name": "Alice"},
            {"Name": "NoPK"},              # missing Id
            {"Id": None, "Name": "NullPK"},  # None Id
            {"Id": "", "Name": "EmptyPK"},   # empty string Id
        ]
        result = merge_records(previous, delta, pk_field="Id")
        assert len(result) == 1
        assert result[0]["Id"] == "1"

    def test_empty_previous_with_only_missing_pk_returns_empty(self):
        delta = [{"Name": "NoPK"}, {"Name": "AlsoNoPK"}]
        result = merge_records({}, delta, pk_field="Id")
        assert result == []


class TestMergeRecordsIdempotency:
    def test_applying_same_delta_twice_is_idempotent(self):
        previous = {"1": {"Id": "1", "Name": "Alice"}}
        delta = [{"Id": "1", "Name": "Alice Updated"}, {"Id": "2", "Name": "Bob"}]
        result_first = merge_records(previous, delta, pk_field="Id")
        # Build state from first result and apply delta again
        state_after_first = {str(r["Id"]): r for r in result_first}
        result_second = merge_records(state_after_first, delta, pk_field="Id")
        # Same number of records
        assert len(result_first) == len(result_second)
        # Same values
        first_by_id = {r["Id"]: r for r in result_first}
        second_by_id = {r["Id"]: r for r in result_second}
        assert first_by_id == second_by_id

    def test_does_not_mutate_previous_state(self):
        """merge_records must not modify the caller's previous_state dict."""
        previous = {"1": {"Id": "1", "Name": "Original"}}
        previous_copy = dict(previous)
        merge_records(previous, [{"Id": "1", "Name": "Updated"}], pk_field="Id")
        assert previous == previous_copy


# ---------------------------------------------------------------------------
# CuratedAccumulator.accumulate() — integration tests with mocked S3
# ---------------------------------------------------------------------------

class TestCuratedAccumulatorAccumulate:
    """Integration tests for CuratedAccumulator using a mocked S3 client."""

    def _make_s3_mock(
        self,
        previous_records: list[dict[str, Any]] | None,
        domain: str = "salesforce",
        entity_id: str = "salesforce-contact",
        bucket: str = "test-curated",
    ) -> MagicMock:
        """Return a mock S3 client that serves the given previous records."""
        mock_s3 = MagicMock()

        if previous_records is None:
            # No previous partition — simulate empty S3
            mock_s3.get_paginator.return_value.paginate.return_value = iter([
                {"CommonPrefixes": [], "Contents": []}
            ])
        else:
            parquet_bytes = _make_parquet_bytes(previous_records)

            def _paginate_side_effect(Bucket, Prefix, **kwargs):  # noqa: N803
                delimiter = kwargs.get("Delimiter")
                if delimiter == "/" and "curated_date=" not in Prefix:
                    # List date partitions
                    return iter([{"CommonPrefixes": [
                        {"Prefix": f"curated/{domain}/{entity_id}/curated_date=2026-07-01/"}
                    ]}])
                elif delimiter == "/" and "curated_date=" in Prefix:
                    # List run_id sub-prefixes
                    return iter([{"CommonPrefixes": [
                        {"Prefix": f"curated/{domain}/{entity_id}/curated_date=2026-07-01/run_id=run-001/"}
                    ]}])
                else:
                    # List Parquet files
                    return iter([{"Contents": [
                        {"Key": f"curated/{domain}/{entity_id}/curated_date=2026-07-01/run_id=run-001/data.parquet"}
                    ], "CommonPrefixes": []}])

            mock_s3.get_paginator.return_value.paginate.side_effect = _paginate_side_effect
            mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: parquet_bytes)}

        return mock_s3

    def test_first_run_no_previous_state(self):
        mock_s3 = self._make_s3_mock(previous_records=None)
        acc = CuratedAccumulator(mock_s3, "test-curated", primary_key_field="Id")
        delta = [{"Id": "1", "Name": "Alice"}, {"Id": "2", "Name": "Bob"}]

        result = acc.accumulate(delta, domain="salesforce", entity_id="salesforce-contact", run_id="run-001")

        assert result.previous_record_count == 0
        assert result.delta_record_count == 2
        assert result.merged_record_count == 2
        assert result.deleted_record_count == 0
        assert len(result.merged_records) == 2

    def test_incremental_run_merges_with_previous(self):
        previous = [
            {"Id": "1", "Name": "Alice"},
            {"Id": "2", "Name": "Bob"},
            {"Id": "3", "Name": "Carol"},
        ]
        mock_s3 = self._make_s3_mock(previous_records=previous)
        acc = CuratedAccumulator(mock_s3, "test-curated", primary_key_field="Id")
        delta = [{"Id": "2", "Name": "Bobby"}, {"Id": "4", "Name": "Dave"}]

        result = acc.accumulate(delta, domain="salesforce", entity_id="salesforce-contact", run_id="run-002")

        assert result.previous_record_count == 3
        assert result.delta_record_count == 2
        assert result.merged_record_count == 4  # 3 previous + 1 new - 0 deleted
        result_by_id = {r["Id"]: r for r in result.merged_records}
        assert result_by_id["2"]["Name"] == "Bobby"  # updated
        assert "4" in result_by_id                   # inserted
        assert "1" in result_by_id                   # unchanged

    def test_soft_delete_removes_record(self):
        previous = [
            {"Id": "1", "Name": "Alice", "is_delete": False},
            {"Id": "2", "Name": "Bob", "is_delete": False},
        ]
        mock_s3 = self._make_s3_mock(previous_records=previous)
        acc = CuratedAccumulator(
            mock_s3, "test-curated",
            primary_key_field="Id",
            soft_delete_field="is_delete",
        )
        delta = [{"Id": "2", "Name": "Bob", "is_delete": True}]

        result = acc.accumulate(delta, domain="salesforce", entity_id="salesforce-contact", run_id="run-003")

        assert result.deleted_record_count == 1
        assert result.merged_record_count == 1
        ids = {r["Id"] for r in result.merged_records}
        assert ids == {"1"}
