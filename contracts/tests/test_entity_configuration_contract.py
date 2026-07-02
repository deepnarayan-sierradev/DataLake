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


class TestEntityExtractionConfigMergeFields:
    """Tests for the new primary_key_field and soft_delete_field (incremental merge)."""

    def _base(self) -> dict[str, object]:
        return {
            "source_id": "salesforce",
            "entity_id": "salesforce-contact",
            "config_version": "1.0.0",
            "load_type": LoadType.INCREMENTAL,
            "watermark_field": "SystemModstamp",
            "target_raw_s3_prefix": "s3://raw/sf/contact/",
            "schema_snapshot_s3_prefix": "s3://snaps/sf/contact/",
        }

    def test_primary_key_field_none_by_default(self):
        config = EntityExtractionConfig(**self._base())
        assert config.primary_key_field is None
        assert config.soft_delete_field is None

    def test_valid_primary_key_field_accepted(self):
        config = EntityExtractionConfig(**self._base(), primary_key_field="Id")
        assert config.primary_key_field == "Id"

    def test_valid_underscore_field_name_accepted(self):
        config = EntityExtractionConfig(**self._base(), primary_key_field="contact_id")
        assert config.primary_key_field == "contact_id"

    def test_both_fields_set_together(self):
        config = EntityExtractionConfig(
            **self._base(),
            primary_key_field="Id",
            soft_delete_field="IsDelete",
        )
        assert config.primary_key_field == "Id"
        assert config.soft_delete_field == "IsDelete"

    def test_soft_delete_without_primary_key_raises(self):
        """soft_delete_field requires primary_key_field."""
        with pytest.raises(ValidationError, match="soft_delete_field requires primary_key_field"):
            EntityExtractionConfig(**self._base(), soft_delete_field="IsDelete")

    def test_dotted_field_name_rejected(self):
        """Dotted paths not allowed — record.get() only does top-level lookup."""
        with pytest.raises(ValidationError, match="invalid"):
            EntityExtractionConfig(**self._base(), primary_key_field="nested.id")

    def test_empty_string_field_name_rejected(self):
        with pytest.raises(ValidationError, match="invalid"):
            EntityExtractionConfig(**self._base(), primary_key_field="")

    def test_field_name_with_spaces_rejected(self):
        with pytest.raises(ValidationError, match="invalid"):
            EntityExtractionConfig(**self._base(), primary_key_field="My Field")

    def test_field_name_starting_with_digit_rejected(self):
        with pytest.raises(ValidationError, match="invalid"):
            EntityExtractionConfig(**self._base(), primary_key_field="1Id")

    def test_primary_key_field_none_explicit(self):
        """Explicitly setting None is valid (same as default)."""
        config = EntityExtractionConfig(**self._base(), primary_key_field=None)
        assert config.primary_key_field is None

    def test_full_load_entity_with_primary_key_accepted(self):
        """Full-load entities can optionally set primary_key_field for future use."""
        config = EntityExtractionConfig(
            source_id="salesforce",
            entity_id="salesforce-account",
            config_version="1.0.0",
            load_type=LoadType.FULL,
            target_raw_s3_prefix="s3://raw/sf/account/",
            schema_snapshot_s3_prefix="s3://snaps/sf/account/",
            primary_key_field="Id",
        )
        assert config.primary_key_field == "Id"
