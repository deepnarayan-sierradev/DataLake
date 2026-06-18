"""
Tests for contracts/identifier_policy.py — covers validate_stable_id and validate_run_id.
"""

from __future__ import annotations

import pytest

from contracts.identifier_policy import (
    PROHIBITED_IDENTIFIERS,
    SEQUENTIAL_INTEGER_PATTERN,
    STABLE_ID_PATTERN,
    validate_run_id,
    validate_stable_id,
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
        # A run_id that contains digits but also letters is fine
        assert validate_run_id("run-001-abc") == "run-001-abc"
