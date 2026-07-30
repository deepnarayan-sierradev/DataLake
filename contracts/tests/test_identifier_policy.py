"""
Tests for contracts/identifier_policy.py — covers validate_stable_id and validate_run_id.
"""

from __future__ import annotations

import pytest

from contracts.identifier_policy import (
    DEFAULT_TENANT_CODE,
    PROHIBITED_IDENTIFIERS,
    SAFE_S3_PREFIX_PATTERN,
    SEQUENTIAL_INTEGER_PATTERN,
    STABLE_ID_PATTERN,
    TENANT_CODE_PATTERN,
    strip_tenant_prefix,
    tenant_scoped_key,
    validate_run_id,
    validate_s3_prefix,
    validate_stable_id,
    validate_tenant_code,
)


class TestStableIdPattern:
    @pytest.mark.parametrize(
        "value",
        [
            "salesforce",
            "salesforce-account",
            "mysql-rds",
            "netsuite-customer",
            "ab",  # minimum 2 chars
            "a" + "b" * 62,  # maximum 63 extra chars = 64 total
        ],
    )
    def test_valid_ids_match(self, value: str) -> None:
        assert STABLE_ID_PATTERN.match(value) is not None

    @pytest.mark.parametrize(
        "value",
        [
            "A",  # uppercase
            "1invalid",  # starts with digit
            "-invalid",  # starts with hyphen
            "a",  # too short (only 1 char)
            "",  # empty
            "has space",
            "has_underscore",
            "has.dot",
        ],
    )
    def test_invalid_ids_do_not_match(self, value: str) -> None:
        assert STABLE_ID_PATTERN.match(value) is None


class TestSequentialIntegerPattern:
    def test_pure_digits_match(self) -> None:
        assert SEQUENTIAL_INTEGER_PATTERN.match("12345") is not None
        assert SEQUENTIAL_INTEGER_PATTERN.match("0") is not None

    def test_non_pure_digits_do_not_match(self) -> None:
        assert SEQUENTIAL_INTEGER_PATTERN.match("run-001") is None
        assert SEQUENTIAL_INTEGER_PATTERN.match("123abc") is None


class TestProhibitedIdentifiers:
    def test_known_prohibited_names_are_in_set(self) -> None:
        for name in ("helper", "util", "common", "manager"):
            assert name in PROHIBITED_IDENTIFIERS

    def test_meaningful_names_are_not_prohibited(self) -> None:
        assert "salesforce" not in PROHIBITED_IDENTIFIERS
        assert "netsuite-customer" not in PROHIBITED_IDENTIFIERS


class TestValidateStableId:
    def test_valid_id_returns_value(self) -> None:
        assert validate_stable_id("salesforce") == "salesforce"
        assert validate_stable_id("mysql-rds-orders") == "mysql-rds-orders"

    def test_invalid_pattern_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="stable identifier format"):
            validate_stable_id("InvalidID")

    def test_starts_with_digit_raises(self) -> None:
        with pytest.raises(ValueError, match="stable identifier format"):
            validate_stable_id("1bad")

    def test_prohibited_name_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="prohibited generic name"):
            validate_stable_id("helper")

    def test_prohibited_name_manager_raises(self) -> None:
        with pytest.raises(ValueError, match="prohibited generic name"):
            validate_stable_id("manager")

    def test_custom_field_name_in_error_message(self) -> None:
        with pytest.raises(ValueError, match="source_id"):
            validate_stable_id("1bad", field_name="source_id")

    def test_prohibited_error_includes_sorted_names(self) -> None:
        with pytest.raises(ValueError, match="Prohibited names"):
            validate_stable_id("util")


class TestValidateRunId:
    def test_valid_run_id_passes(self) -> None:
        assert validate_run_id("run-20260612-143022-a3f9c1d2") == "run-20260612-143022-a3f9c1d2"

    def test_sequential_integer_rejected(self) -> None:
        with pytest.raises(ValueError, match="bare sequential integer"):
            validate_run_id("12345")

    def test_single_digit_rejected(self) -> None:
        with pytest.raises(ValueError, match="bare sequential integer"):
            validate_run_id("0")

    def test_alphanumeric_run_id_accepted(self) -> None:
        assert validate_run_id("run-001-abc") == "run-001-abc"


class TestTenantCodePattern:
    @pytest.mark.parametrize(
        "value",
        [
            "demo",
            "acme-corp",
            "globex-eu",
            "initech",
            "tenant1",
            "ab",  # minimum 2 chars
            "a" + "x" * 47,  # maximum 48 chars
        ],
    )
    def test_valid_tenant_codes_match(self, value: str) -> None:
        assert TENANT_CODE_PATTERN.match(value) is not None

    @pytest.mark.parametrize(
        "value",
        [
            "A",  # uppercase
            "1bad",  # starts with digit
            "-bad",  # starts with hyphen
            "a",  # too short (1 char)
            "",  # empty
            "has space",  # space
            "has_underscore",  # underscore not allowed
            "has.dot",  # dot not allowed
            "UPPER",  # uppercase
            "a" * 49,  # too long (49 chars)
        ],
    )
    def test_invalid_tenant_codes_do_not_match(self, value: str) -> None:
        assert TENANT_CODE_PATTERN.match(value) is None

    def test_demo_tenant_code_is_valid(self) -> None:
        """The default test tenant_code 'demo' must always be valid."""
        assert TENANT_CODE_PATTERN.match("demo") is not None

    def test_tenant_code_48_chars_valid(self) -> None:
        """48-character tenant codes are at the boundary and must be accepted."""
        value = "a" + "b" * 47  # 48 chars total
        assert TENANT_CODE_PATTERN.match(value) is not None


class TestValidateTenantCode:
    def test_valid_tenant_code_returned_unchanged(self) -> None:
        assert validate_tenant_code("acme-corp") == "acme-corp"

    def test_invalid_tenant_code_raises(self) -> None:
        with pytest.raises(ValueError, match="tenant_code"):
            validate_tenant_code("BAD_CODE")

    def test_custom_field_name_in_error_message(self) -> None:
        with pytest.raises(ValueError, match="tenant"):
            validate_tenant_code("BAD", field_name="tenant")


class TestTenantScopedKey:
    def test_default_tenant_is_prefixed_like_any_other(self) -> None:
        """Matches curated_layer_writer.py's S3 convention: no special-casing."""
        result = tenant_scoped_key(DEFAULT_TENANT_CODE, "salesforce-account")
        assert result == "demo#salesforce-account"

    def test_non_default_tenant_prefixes_key(self) -> None:
        result = tenant_scoped_key("acme-corp", "salesforce-account")
        assert result == "acme-corp#salesforce-account"

    def test_different_tenants_never_collide(self) -> None:
        key_a = tenant_scoped_key("acme-corp", "salesforce-account")
        key_b = tenant_scoped_key("globex-eu", "salesforce-account")
        key_c = tenant_scoped_key(DEFAULT_TENANT_CODE, "salesforce-account")
        assert len({key_a, key_b, key_c}) == 3


class TestStripTenantPrefix:
    def test_round_trips_scoped_key(self) -> None:
        scoped = tenant_scoped_key(DEFAULT_TENANT_CODE, "salesforce")
        assert strip_tenant_prefix(DEFAULT_TENANT_CODE, scoped) == "salesforce"

    def test_non_default_tenant_round_trips(self) -> None:
        scoped = tenant_scoped_key("acme-corp", "salesforce")
        assert strip_tenant_prefix("acme-corp", scoped) == "salesforce"

    def test_unprefixed_value_returned_unchanged(self) -> None:
        assert strip_tenant_prefix(DEFAULT_TENANT_CODE, "salesforce") == "salesforce"

    def test_only_strips_matching_tenant_prefix(self) -> None:
        assert strip_tenant_prefix("acme-corp", "demo#salesforce") == "demo#salesforce"


class TestSafeS3PrefixPattern:
    def test_valid_prefixes_match(self) -> None:
        assert SAFE_S3_PREFIX_PATTERN.match("demo/analytics/company/analytics_date=2026-07-02")
        assert SAFE_S3_PREFIX_PATTERN.match("acme-corp/curated/crm/entity/run_id=run-1")

    def test_traversal_and_leading_slash_rejected(self) -> None:
        assert not SAFE_S3_PREFIX_PATTERN.match("../etc/passwd")
        assert not SAFE_S3_PREFIX_PATTERN.match("/etc/passwd")
        assert not SAFE_S3_PREFIX_PATTERN.match("a/../b")
        assert not SAFE_S3_PREFIX_PATTERN.match("data.parquet")

    def test_empty_and_bad_first_char_rejected(self) -> None:
        assert not SAFE_S3_PREFIX_PATTERN.match("")
        assert not SAFE_S3_PREFIX_PATTERN.match("-leading-hyphen")


class TestValidateS3Prefix:
    def test_valid_prefix_returned_without_trailing_slash(self) -> None:
        assert validate_s3_prefix("demo/analytics/company/") == "demo/analytics/company"

    def test_traversal_rejected(self) -> None:
        with pytest.raises(ValueError, match="s3_prefix"):
            validate_s3_prefix("../etc/passwd")

    def test_leading_slash_rejected(self) -> None:
        with pytest.raises(ValueError):
            validate_s3_prefix("/etc/passwd")

    def test_custom_field_name_in_error(self) -> None:
        with pytest.raises(ValueError, match="canonical_prefix"):
            validate_s3_prefix("../x", field_name="canonical_prefix")
