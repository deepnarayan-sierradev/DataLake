"""Tests for FieldMappingRegistry — Phase 6."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from transformation.field_mapping.field_mapping_registry import (
    FieldMappingApplicator,
    FieldMappingRegistryClient,
    FieldMappingRule,
    FieldMappingRuleSet,
    MappingRuleSetNotFoundError,
    MappingTransformation,
    MissingFieldBehavior,
)

_REGION = "us-east-1"
_BUCKET = "test-mapping-bucket"


def _make_rule(
    source_fields,
    canonical,
    transformation=MappingTransformation.RENAME,
    params=None,
    missing=MissingFieldBehavior.DROP_FIELD,
    default=None,
):
    return FieldMappingRule(
        source_fields=tuple(source_fields),
        canonical_field=canonical,
        transformation=transformation,
        transformation_params=params or {},
        missing_field_behavior=missing,
        default_value=default,
    )


def _make_rule_set(version="1.0.0", rules=None):
    return FieldMappingRuleSet(
        source_id="salesforce",
        entity_id="salesforce-account",
        mapping_version=version,
        rules=tuple(rules or []),
    )


# ---------------------------------------------------------------------------
# FieldMappingRule validation
# ---------------------------------------------------------------------------


class TestFieldMappingRuleValidation:
    def test_invalid_source_field_name_raises(self):
        with pytest.raises(ValueError, match="Invalid source field name"):
            _make_rule(["123invalid"], "canonical_name")

    def test_invalid_canonical_field_name_raises(self):
        with pytest.raises(ValueError, match="Invalid canonical field name"):
            _make_rule(["SourceField"], "InvalidCanonical")

    def test_empty_source_fields_raises(self):
        with pytest.raises(ValueError):
            FieldMappingRule(
                source_fields=(),
                canonical_field="name",
                transformation=MappingTransformation.RENAME,
                transformation_params={},
            )

    def test_valid_rule_with_dotted_source_field(self):
        rule = _make_rule(["Owner.Name"], "owner_name")
        assert rule.source_fields == ("Owner.Name",)


# ---------------------------------------------------------------------------
# FieldMappingApplicator — transformation types
# ---------------------------------------------------------------------------


class TestFieldMappingApplicatorTransformations:
    def setup_method(self, method=None):
        self.applicator = FieldMappingApplicator()

    def _apply_single(self, record, rule):
        rs = _make_rule_set(rules=[rule])
        return self.applicator.apply(record, rs)

    def test_rename(self):
        rule = _make_rule(["FirstName"], "first_name")
        result = self._apply_single({"FirstName": "Alice"}, rule)
        assert result == {"first_name": "Alice"}

    def test_concat_two_fields(self):
        rule = _make_rule(
            ["FirstName", "LastName"],
            "full_name",
            transformation=MappingTransformation.CONCAT,
            params={"separator": " "},
        )
        result = self._apply_single({"FirstName": "Alice", "LastName": "Smith"}, rule)
        assert result == {"full_name": "Alice Smith"}

    def test_concat_custom_separator(self):
        rule = _make_rule(
            ["area_code", "number"],
            "phone",
            transformation=MappingTransformation.CONCAT,
            params={"separator": "-"},
        )
        result = self._apply_single({"area_code": "416", "number": "555-1234"}, rule)
        assert result == {"phone": "416-555-1234"}

    def test_date_format(self):
        rule = _make_rule(
            ["SystemModstamp"],
            "modified_date",
            transformation=MappingTransformation.DATE_FORMAT,
            params={"input_format": "%Y-%m-%dT%H:%M:%SZ", "output_format": "%Y-%m-%d"},
        )
        result = self._apply_single({"SystemModstamp": "2024-01-15T10:30:00Z"}, rule)
        assert result == {"modified_date": "2024-01-15"}

    def test_cast_to_integer(self):
        rule = _make_rule(
            ["NumberOfEmployees"],
            "employee_count",
            transformation=MappingTransformation.CAST,
            params={"type": "integer"},
        )
        result = self._apply_single({"NumberOfEmployees": "500"}, rule)
        assert result == {"employee_count": 500}

    def test_cast_to_float(self):
        rule = _make_rule(
            ["AnnualRevenue"],
            "annual_revenue",
            transformation=MappingTransformation.CAST,
            params={"type": "float"},
        )
        result = self._apply_single({"AnnualRevenue": "1234567.89"}, rule)
        assert result["annual_revenue"] == pytest.approx(1234567.89)

    def test_cast_to_boolean_string_true(self):
        rule = _make_rule(
            ["IsActive"],
            "is_active",
            transformation=MappingTransformation.CAST,
            params={"type": "boolean"},
        )
        result = self._apply_single({"IsActive": "true"}, rule)
        assert result == {"is_active": True}

    def test_mask_email(self):
        rule = _make_rule(
            ["Email"],
            "email_masked",
            transformation=MappingTransformation.MASK,
            params={"visible_chars": "4"},
        )
        result = self._apply_single({"Email": "alice@example.com"}, rule)
        assert result["email_masked"].endswith(".com")
        assert "*" in result["email_masked"]

    def test_mask_short_value(self):
        rule = _make_rule(
            ["Code"],
            "code_masked",
            transformation=MappingTransformation.MASK,
            params={"visible_chars": "4"},
        )
        result = self._apply_single({"Code": "AB"}, rule)
        assert result["code_masked"] == "**"


# ---------------------------------------------------------------------------
# Missing field behaviours
# ---------------------------------------------------------------------------


class TestMissingFieldBehavior:
    def setup_method(self, method=None):
        self.applicator = FieldMappingApplicator()

    def test_drop_field_on_missing(self):
        rule = _make_rule(["OptionalField"], "optional", missing=MissingFieldBehavior.DROP_FIELD)
        rs = _make_rule_set(rules=[rule])
        result = self.applicator.apply({"OtherField": "x"}, rs)
        assert result == {}

    def test_use_default_on_missing(self):
        rule = _make_rule(
            ["Region"],
            "region",
            missing=MissingFieldBehavior.USE_DEFAULT,
            default="UNKNOWN",
        )
        rs = _make_rule_set(rules=[rule])
        result = self.applicator.apply({}, rs)
        assert result == {"region": "UNKNOWN"}

    def test_raise_error_on_missing_returns_none(self):
        rule = _make_rule(
            ["RequiredField"],
            "required",
            missing=MissingFieldBehavior.RAISE_ERROR,
        )
        rs = _make_rule_set(rules=[rule])
        result = self.applicator.apply({}, rs)
        assert result is None

    def test_drop_null_value(self):
        rule = _make_rule(["Field"], "field", missing=MissingFieldBehavior.DROP_FIELD)
        rs = _make_rule_set(rules=[rule])
        result = self.applicator.apply({"Field": None}, rs)
        assert result == {}


# ---------------------------------------------------------------------------
# S3-backed registry client
# ---------------------------------------------------------------------------


@mock_aws
class TestFieldMappingRegistryClient:
    def setup_method(self, method=None):
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_BUCKET)
        self.client = FieldMappingRegistryClient(_BUCKET, _REGION)

    def _sample_rule_set(self, version="1.0.0"):
        return _make_rule_set(
            version=version,
            rules=[_make_rule(["FirstName"], "first_name")],
        )

    def test_publish_and_load_by_version(self):
        rs = self._sample_rule_set("2.0.0")
        self.client.publish_rule_set(rs)
        loaded = self.client.load_rule_set("salesforce", "salesforce-account", "2.0.0")
        assert loaded.mapping_version == "2.0.0"
        assert len(loaded.rules) == 1

    def test_publish_updates_latest_pointer(self):
        rs = self._sample_rule_set("3.0.0")
        self.client.publish_rule_set(rs)
        loaded = self.client.load_rule_set("salesforce", "salesforce-account", "latest")
        assert loaded.mapping_version == "3.0.0"

    def test_load_nonexistent_raises(self):
        with pytest.raises(MappingRuleSetNotFoundError):
            self.client.load_rule_set("salesforce", "salesforce-account", "99.0.0")

    def test_load_latest_without_pointer_raises(self):
        with pytest.raises(MappingRuleSetNotFoundError):
            self.client.load_rule_set("unknown-source", "unknown-entity", "latest")

    def test_roundtrip_all_transformations(self):
        rules = [
            _make_rule(["F1", "F2"], "concat_f", MappingTransformation.CONCAT, {"separator": "-"}),
            _make_rule(
                ["DateField"],
                "date_f",
                MappingTransformation.DATE_FORMAT,
                {"output_format": "%Y-%m-%d"},
            ),
            _make_rule(["NumField"], "num_f", MappingTransformation.CAST, {"type": "integer"}),
            _make_rule(
                ["EmailField"], "email_f", MappingTransformation.MASK, {"visible_chars": "4"}
            ),
        ]
        rs = _make_rule_set(version="roundtrip", rules=rules)
        self.client.publish_rule_set(rs)
        loaded = self.client.load_rule_set("salesforce", "salesforce-account", "roundtrip")
        assert len(loaded.rules) == 4
        assert loaded.rules[0].transformation == MappingTransformation.CONCAT
