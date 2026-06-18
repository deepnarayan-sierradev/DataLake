"""
Tests for the Watermark Repository (2.2).

Covers:
  - get_watermark returns None on first run
  - initialise_watermark creates record with version=0 and EPOCH as last_successful
  - advance_watermark advances record and bumps version
  - advance_watermark raises WatermarkConcurrencyError on version mismatch
  - Watermark is NOT advanced on failed run (advance not called)
  - compute_extraction_window: INCREMENTAL with watermark
  - compute_extraction_window: INCREMENTAL first run (no watermark)
  - compute_extraction_window: FULL load
  - compute_replay_window: returns window unchanged
  - compute_replay_window: raises on inverted window
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from contracts.entity_configuration_contract import EntityExtractionConfig, LoadType
from watermark_management.watermark_repository.watermark_repository import (
    WatermarkConcurrencyError,
    WatermarkRecord,
    WatermarkRepository,
    _serialise_watermark,
)

_REGION = "us-east-1"
_ENV = "dev"
_TABLE = f"{_ENV}-watermark-repository"

_NOW = datetime(2026, 6, 11, 14, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_table() -> None:
    ddb = boto3.resource("dynamodb", region_name=_REGION)
    ddb.create_table(
        TableName=_TABLE,
        KeySchema=[
            {"AttributeName": "source_id", "KeyType": "HASH"},
            {"AttributeName": "entity_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "source_id", "AttributeType": "S"},
            {"AttributeName": "entity_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _incremental_config(overlap_hours: int = 2) -> EntityExtractionConfig:
    return EntityExtractionConfig(
        source_id="salesforce",
        entity_id="salesforce-account",
        config_version="1.0.0",
        load_type=LoadType.INCREMENTAL,
        watermark_field="SystemModstamp",
        watermark_overlap_hours=overlap_hours,
        extraction_window_days=1,
        target_raw_s3_prefix="s3://raw/salesforce/account/",
        schema_snapshot_s3_prefix="s3://schema-snapshots/salesforce/account/",
    )


def _full_config() -> EntityExtractionConfig:
    return EntityExtractionConfig(
        source_id="netsuite",
        entity_id="netsuite-customer",
        config_version="1.0.0",
        load_type=LoadType.FULL,
        target_raw_s3_prefix="s3://raw/netsuite/customer/",
        schema_snapshot_s3_prefix="s3://schema-snapshots/netsuite/customer/",
    )


def _repo() -> WatermarkRepository:
    return WatermarkRepository(environment=_ENV, region_name=_REGION)


# ---------------------------------------------------------------------------
# get_watermark
# ---------------------------------------------------------------------------


class TestGetWatermark:
    @mock_aws
    def test_returns_none_when_no_record(self) -> None:
        _create_table()
        result = _repo().get_watermark("salesforce", "salesforce-account")
        assert result is None

    @mock_aws
    def test_returns_record_when_exists(self) -> None:
        _create_table()
        repo = _repo()
        repo.initialise_watermark(
            source_id="salesforce",
            entity_id="salesforce-account",
            upper_watermark=_NOW,
            run_id="run-20260611-140000000000-abcd1234",
        )
        record = repo.get_watermark("salesforce", "salesforce-account")
        assert record is not None
        assert record.source_id == "salesforce"
        assert record.version == 0


# ---------------------------------------------------------------------------
# initialise_watermark
# ---------------------------------------------------------------------------


class TestInitialiseWatermark:
    @mock_aws
    def test_creates_record_with_epoch_last_watermark(self) -> None:
        _create_table()
        repo = _repo()
        run_id = "run-20260611-140000000000-abcd1234"
        record = repo.initialise_watermark(
            source_id="salesforce",
            entity_id="salesforce-account",
            upper_watermark=_NOW,
            run_id=run_id,
        )
        assert record.version == 0
        assert record.run_id == run_id
        assert record.last_successful_watermark.year == 1970
        assert record.upper_watermark == _NOW
        assert record.environment == _ENV

    @mock_aws
    def test_idempotent_when_record_already_exists(self) -> None:
        _create_table()
        repo = _repo()
        run_id_a = "run-20260611-140000000000-aaaaaaaa"
        run_id_b = "run-20260611-140000000000-bbbbbbbb"

        repo.initialise_watermark("salesforce", "salesforce-account", _NOW, run_id_a)
        # Second initialise should return the existing record, not overwrite
        result = repo.initialise_watermark("salesforce", "salesforce-account", _NOW, run_id_b)
        assert result.run_id == run_id_a  # Original run_id preserved


# ---------------------------------------------------------------------------
# advance_watermark
# ---------------------------------------------------------------------------


class TestAdvanceWatermark:
    @mock_aws
    def test_advance_bumps_version_and_last_successful(self) -> None:
        _create_table()
        repo = _repo()
        initial = repo.initialise_watermark(
            "salesforce", "salesforce-account", _NOW, "run-20260611-140000000000-aaaa0001"
        )
        new_upper = _NOW + timedelta(hours=24)
        advanced = repo.advance_watermark(
            current=initial,
            new_upper_watermark=new_upper,
            run_id="run-20260611-140000000000-aaaa0002",
        )
        assert advanced.version == 1
        assert advanced.last_successful_watermark == initial.upper_watermark
        assert advanced.upper_watermark == new_upper

    @mock_aws
    def test_watermark_does_not_advance_if_advance_not_called(self) -> None:
        _create_table()
        repo = _repo()
        initial = repo.initialise_watermark(
            "salesforce", "salesforce-account", _NOW, "run-20260611-140000000000-aaaa0001"
        )
        # Simulate a failed run: reload without advancing
        reloaded = repo.get_watermark("salesforce", "salesforce-account")
        assert reloaded is not None
        assert reloaded.version == initial.version
        assert reloaded.last_successful_watermark == initial.last_successful_watermark

    @mock_aws
    def test_concurrency_error_on_stale_version(self) -> None:
        _create_table()
        repo = _repo()
        record_v0 = repo.initialise_watermark(
            "salesforce", "salesforce-account", _NOW, "run-20260611-140000000000-aaaa0001"
        )
        # Advance once (version → 1)
        repo.advance_watermark(
            current=record_v0,
            new_upper_watermark=_NOW + timedelta(hours=1),
            run_id="run-20260611-140000000000-aaaa0002",
        )
        # Attempt second advance with the stale v0 record → must fail
        with pytest.raises(WatermarkConcurrencyError, match="version"):
            repo.advance_watermark(
                current=record_v0,
                new_upper_watermark=_NOW + timedelta(hours=2),
                run_id="run-20260611-140000000000-aaaa0003",
            )


# ---------------------------------------------------------------------------
# compute_extraction_window
# ---------------------------------------------------------------------------


class TestComputeExtractionWindow:
    def test_incremental_with_prior_watermark_subtracts_overlap(self) -> None:
        repo = WatermarkRepository.__new__(WatermarkRepository)
        config = _incremental_config(overlap_hours=2)
        last_success = _NOW - timedelta(hours=24)
        watermark = WatermarkRecord(
            source_id="salesforce",
            entity_id="salesforce-account",
            environment=_ENV,
            last_successful_watermark=last_success,
            upper_watermark=_NOW - timedelta(hours=1),
            run_id="run-20260611-140000000000-aaaa0001",
            updated_at=_NOW,
            version=1,
        )
        lower, upper = repo.compute_extraction_window(watermark, config, _NOW)
        assert upper == _NOW
        assert lower == last_success - timedelta(hours=2)

    def test_incremental_first_run_uses_extraction_window_days(self) -> None:
        repo = WatermarkRepository.__new__(WatermarkRepository)
        config = _incremental_config()
        lower, upper = repo.compute_extraction_window(None, config, _NOW)
        assert upper == _NOW
        assert lower == _NOW - timedelta(days=config.extraction_window_days)

    def test_full_load_uses_extraction_window_days(self) -> None:
        repo = WatermarkRepository.__new__(WatermarkRepository)
        config = _full_config()
        lower, upper = repo.compute_extraction_window(None, config, _NOW)
        assert upper == _NOW
        assert lower == _NOW - timedelta(days=config.extraction_window_days)


# ---------------------------------------------------------------------------
# compute_replay_window
# ---------------------------------------------------------------------------


class TestComputeReplayWindow:
    def test_returns_window_unchanged(self) -> None:
        start = _NOW - timedelta(days=7)
        end = _NOW - timedelta(days=6)
        lo, hi = WatermarkRepository.compute_replay_window(start, end)
        assert lo == start
        assert hi == end

    def test_raises_when_start_equals_end(self) -> None:
        with pytest.raises(ValueError, match="before"):
            WatermarkRepository.compute_replay_window(_NOW, _NOW)

    def test_raises_when_start_after_end(self) -> None:
        with pytest.raises(ValueError, match="before"):
            WatermarkRepository.compute_replay_window(_NOW, _NOW - timedelta(hours=1))


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


class TestSerialisedWatermark:
    def test_all_datetimes_stored_as_isoformat_strings(self) -> None:
        record = WatermarkRecord(
            source_id="salesforce",
            entity_id="salesforce-account",
            environment="dev",
            last_successful_watermark=_NOW,
            upper_watermark=_NOW + timedelta(hours=1),
            run_id="run-20260611-140000000000-aaaa0001",
            updated_at=_NOW,
            version=0,
        )
        item = _serialise_watermark(record)
        assert isinstance(item["last_successful_watermark"], str)
        assert isinstance(item["upper_watermark"], str)
        assert isinstance(item["updated_at"], str)
        assert item["version"] == 0


# ---------------------------------------------------------------------------
# Regression tests for fixed bugs
# ---------------------------------------------------------------------------


class TestAdvanceWatermarkGuard:
    """
    Regression test for Bug #4: advance_watermark accepted backward movement.

    Advancing the watermark to an earlier time creates silent extraction gaps
    in subsequent incremental runs.  A ValueError must be raised before any
    DynamoDB write is attempted.
    """

    @mock_aws
    def test_backward_advance_raises_value_error(self) -> None:
        _create_table()
        repo = _repo()
        record = repo.initialise_watermark(
            "salesforce",
            "salesforce-account",
            _NOW,
            "run-20260611-140000000000-aaaa0001",
        )
        past_upper = _NOW - timedelta(hours=6)
        with pytest.raises(ValueError, match="must be >="):
            repo.advance_watermark(
                current=record,
                new_upper_watermark=past_upper,
                run_id="run-20260611-140000000000-aaaa0002",
            )

    @mock_aws
    def test_equal_upper_watermark_is_allowed(self) -> None:
        """Advancing to the same upper_watermark is idempotent and must not raise."""
        _create_table()
        repo = _repo()
        record = repo.initialise_watermark(
            "salesforce",
            "salesforce-account",
            _NOW,
            "run-20260611-140000000000-aaaa0001",
        )
        # Advancing to the same upper_watermark (re-run of same window) is allowed.
        advanced = repo.advance_watermark(
            current=record,
            new_upper_watermark=_NOW,
            run_id="run-20260611-140000000000-aaaa0002",
        )
        assert advanced.upper_watermark == _NOW


class TestComputeExtractionWindowIsStatic:
    """compute_extraction_window must be callable without a repository instance."""

    def test_callable_as_static_method(self) -> None:
        config = _incremental_config()
        lower, upper = WatermarkRepository.compute_extraction_window(None, config, _NOW)
        assert upper == _NOW
        assert lower == _NOW - timedelta(days=config.extraction_window_days)


# ---------------------------------------------------------------------------
# Additional error-path coverage: get_watermark ClientError re-raises,
# _coerce_datetime validator branches, empty environment raises,
# initialise_watermark race-condition branch (ConditionalCheckFailed + None)
# ---------------------------------------------------------------------------


@mock_aws
class TestWatermarkRepositoryErrorPaths:
    def setup_method(self, method: object = None) -> None:
        _create_table()
        self.repo = WatermarkRepository(environment=_ENV, region_name=_REGION)

    def test_empty_environment_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="environment must not be empty"):
            WatermarkRepository(environment="", region_name=_REGION)

    def test_get_watermark_client_error_reraises(self) -> None:
        from unittest.mock import MagicMock

        from botocore.exceptions import ClientError

        self.repo._table.get_item = MagicMock(  # type: ignore[method-assign]
            side_effect=ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "Table gone"}},
                "GetItem",
            )
        )
        with pytest.raises(ClientError):
            self.repo.get_watermark("salesforce", "salesforce-account")

    def test_coerce_datetime_with_naive_string(self) -> None:
        """A naive ISO-8601 string is coerced to UTC-aware datetime."""
        rec = WatermarkRecord(
            source_id="salesforce",
            entity_id="salesforce-account",
            environment=_ENV,
            run_id="run-001",
            last_successful_watermark="2024-01-01T00:00:00",  # naive string
            upper_watermark="2024-01-15T00:00:00",
            updated_at="2024-01-15T00:00:00",
            version=0,
        )
        assert rec.last_successful_watermark.tzinfo is not None

    def test_coerce_datetime_with_aware_datetime(self) -> None:
        """An already-aware datetime passes through unchanged."""
        aware = datetime(2024, 1, 1, tzinfo=UTC)
        rec = WatermarkRecord(
            source_id="salesforce",
            entity_id="salesforce-account",
            environment=_ENV,
            run_id="run-001",
            last_successful_watermark=aware,
            upper_watermark=aware,
            updated_at=aware,
            version=0,
        )
        assert rec.last_successful_watermark == aware

    def test_coerce_datetime_invalid_type_raises(self) -> None:
        """Passing an unexpected type to the validator raises ValueError."""
        with pytest.raises(ValueError, match="Expected a datetime"):
            WatermarkRecord(
                source_id="s",
                entity_id="e",
                environment=_ENV,
                run_id="run-001",
                last_successful_watermark=12345,  # type: ignore[arg-type]
                upper_watermark=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
                version=0,
            )

    def test_advance_watermark_non_concurrency_error_reraises(self) -> None:
        """advance_watermark re-raises non-concurrency ClientErrors."""
        from unittest.mock import MagicMock

        from botocore.exceptions import ClientError

        # Initialise first
        rec = self.repo.initialise_watermark(
            "sf", "sf-account", upper_watermark=_NOW, run_id="run-init-001"
        )
        self.repo._table.put_item = MagicMock(  # type: ignore[method-assign]
            side_effect=ClientError(
                {"Error": {"Code": "InternalServerError", "Message": ""}},
                "PutItem",
            )
        )
        with pytest.raises(ClientError):
            self.repo.advance_watermark(rec, _NOW, "run-test-001")
