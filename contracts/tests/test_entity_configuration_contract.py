"""
Tests for the EntityExtractionConfig configuration contract.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from contracts.entity_configuration_contract import (
    EntityExtractionConfig,
    FieldMode,
    LoadType,
    OutputFormat,
)


class TestEntityExtractionConfigValidConstruction:
    def _base_incremental(self) -> dict[str, object]:
        return {
            "source_id": "salesforce",
            "entity_id": "salesforce-account",
            "config_version": "1.0.0",
            "load_type": LoadType.INCREMENTAL,
            "watermark_field": "SystemModstamp",
            "target_raw_s3_prefix": "s3://raw/salesforce/account/",
            "schema_snapshot_s3_prefix": "s3://schema-snapshots/salesforce/account/",
        }

    def test_valid_incremental_config(self) -> None:
        config = EntityExtractionConfig(**self._base_incremental())
        assert config.load_type == LoadType.INCREMENTAL
        assert config.watermark_field == "SystemModstamp"
        assert config.active is True

    def test_valid_full_load_config_without_watermark(self) -> None:
        config = EntityExtractionConfig(
            source_id="netsuite",
            entity_id="netsuite-customer",
            config_version="1.0.0",
            load_type=LoadType.FULL,
            watermark_field=None,
            target_raw_s3_prefix="s3://raw/netsuite/customer/",
            schema_snapshot_s3_prefix="s3://schema-snapshots/netsuite/customer/",
        )
        assert config.load_type == LoadType.FULL

    def test_defaults_applied(self) -> None:
        config = EntityExtractionConfig(**self._base_incremental())
        assert config.field_mode == FieldMode.ALL
        assert config.output_format == OutputFormat.PARQUET
        assert config.extraction_window_days == 1
        assert config.watermark_overlap_hours == 2
        assert config.include_fields == []
        assert config.exclude_fields == []

    def test_include_only_mode_with_fields(self) -> None:
        config = EntityExtractionConfig(
            **{
                **self._base_incremental(),
                "field_mode": FieldMode.INCLUDE_ONLY,
                "include_fields": ["Id", "Name", "SystemModstamp"],
            }
        )
        assert config.field_mode == FieldMode.INCLUDE_ONLY
        assert "Id" in config.include_fields

    def test_config_is_immutable(self) -> None:
        config = EntityExtractionConfig(**self._base_incremental())
        with pytest.raises((ValidationError, TypeError)):
            config.watermark_field = "LastModifiedDate"  # pydantic frozen: raises at runtime


class TestEntityExtractionConfigValidationErrors:
    def test_incremental_without_watermark_field_rejected(self) -> None:
        with pytest.raises(ValidationError, match="watermark_field is required"):
            EntityExtractionConfig(
                source_id="salesforce",
                entity_id="salesforce-account",
                config_version="1.0.0",
                load_type=LoadType.INCREMENTAL,
                watermark_field=None,  # missing — must raise
                target_raw_s3_prefix="s3://raw/salesforce/account/",
                schema_snapshot_s3_prefix="s3://schema-snapshots/salesforce/account/",
            )

    def test_include_only_without_include_fields_rejected(self) -> None:
        with pytest.raises(ValidationError, match="include_fields must not be empty"):
            EntityExtractionConfig(
                source_id="salesforce",
                entity_id="salesforce-account",
                config_version="1.0.0",
                load_type=LoadType.INCREMENTAL,
                watermark_field="SystemModstamp",
                field_mode=FieldMode.INCLUDE_ONLY,
                include_fields=[],  # empty — must raise
                target_raw_s3_prefix="s3://raw/salesforce/account/",
                schema_snapshot_s3_prefix="s3://schema-snapshots/salesforce/account/",
            )

    def test_conflicting_include_exclude_fields_rejected(self) -> None:
        with pytest.raises(ValidationError, match="both include_fields and exclude_fields"):
            EntityExtractionConfig(
                source_id="salesforce",
                entity_id="salesforce-account",
                config_version="1.0.0",
                load_type=LoadType.INCREMENTAL,
                watermark_field="SystemModstamp",
                field_mode=FieldMode.INCLUDE_ONLY,
                include_fields=["Id", "Name", "SystemModstamp"],
                exclude_fields=["Name"],  # conflict with include — must raise
                target_raw_s3_prefix="s3://raw/salesforce/account/",
                schema_snapshot_s3_prefix="s3://schema-snapshots/salesforce/account/",
            )

    def test_unknown_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EntityExtractionConfig(
                source_id="salesforce",
                entity_id="salesforce-account",
                config_version="1.0.0",
                load_type=LoadType.FULL,
                target_raw_s3_prefix="s3://raw/salesforce/account/",
                schema_snapshot_s3_prefix="s3://schema-snapshots/salesforce/account/",
                unknown_field="should_fail",  # type: ignore[call-arg]  # extra='forbid' test
            )

    @pytest.mark.parametrize(
        "bad_id",
        [
            "Salesforce",  # uppercase
            "salesforce_account",  # underscore
            "PHASE1",  # uppercase + prohibited
            "1salesforce",  # starts with digit
            "",  # empty
            "a" * 65,  # too long
        ],
    )
    def test_invalid_source_id_rejected(self, bad_id: str) -> None:
        with pytest.raises(ValidationError, match="stable ID format"):
            EntityExtractionConfig(
                source_id=bad_id,
                entity_id="salesforce-account",
                config_version="1.0.0",
                load_type=LoadType.FULL,
                target_raw_s3_prefix="s3://raw/salesforce/account/",
                schema_snapshot_s3_prefix="s3://schema-snapshots/salesforce/account/",
            )

    @pytest.mark.parametrize(
        "bad_prefix",
        [
            "raw/salesforce/account/",  # missing s3://
            "/mnt/raw/salesforce/account/",  # local path
            "S3://raw/salesforce/account/",  # wrong case
            "https://s3.amazonaws.com/raw/",  # full URL not accepted
        ],
    )
    def test_invalid_s3_prefix_rejected(self, bad_prefix: str) -> None:
        with pytest.raises(ValidationError, match="s3://"):
            EntityExtractionConfig(
                source_id="salesforce",
                entity_id="salesforce-account",
                config_version="1.0.0",
                load_type=LoadType.FULL,
                target_raw_s3_prefix=bad_prefix,
                schema_snapshot_s3_prefix="s3://schema-snapshots/salesforce/account/",
            )
