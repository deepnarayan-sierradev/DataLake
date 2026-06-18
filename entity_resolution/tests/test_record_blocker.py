"""
Tests for entity_resolution/matching_engine/record_blocker.py.

Covers all BlockingKeyType strategies, oversized-block subdivision,
missing field handling, and the _normalise() helper.
"""

from __future__ import annotations

import pytest

from entity_resolution.matching_engine.record_blocker import (
    BlockingKeyType,
    BlockingStrategy,
    RecordBlocker,
    _normalise,
)


def _strategy(
    key_type: BlockingKeyType,
    source_field: str = "email",
    max_block_size: int = 1_000,
) -> BlockingStrategy:
    return BlockingStrategy(
        key_type=key_type, source_field=source_field, max_block_size=max_block_size
    )


# ---------------------------------------------------------------------------
# _normalise helper
# ---------------------------------------------------------------------------


class TestNormalise:
    def test_strips_whitespace(self) -> None:
        assert _normalise("  hello  ") == "hello"

    def test_lowercases(self) -> None:
        assert _normalise("Alice") == "alice"

    def test_unicode_nfkd(self) -> None:
        # NFKD normalisation decomposes accented chars
        result = _normalise("Ãlicé")
        assert "a" in result  # accents stripped

    def test_empty_string(self) -> None:
        assert _normalise("") == ""


# ---------------------------------------------------------------------------
# EMAIL_DOMAIN blocking
# ---------------------------------------------------------------------------


class TestEmailDomainBlocking:
    def test_same_domain_in_same_block(self) -> None:
        strat = _strategy(BlockingKeyType.EMAIL_DOMAIN, "email")
        blocker = RecordBlocker(strat)
        records = [
            {"id": "1", "email": "alice@example.com"},
            {"id": "2", "email": "bob@example.com"},
            {"id": "3", "email": "carol@other.org"},
        ]
        blocks = blocker.partition(records)
        # example.com records should be in one block, other.org in another
        all_ids_in_blocks = {r["id"] for block in blocks for r in block}
        assert all_ids_in_blocks == {"1", "2", "3"}
        example_block = next(
            b for b in blocks if any(r["email"].endswith("@example.com") for r in b)
        )
        assert len(example_block) == 2

    def test_missing_at_symbol_goes_to_no_domain_bucket(self) -> None:
        strat = _strategy(BlockingKeyType.EMAIL_DOMAIN, "email")
        blocker = RecordBlocker(strat)
        records = [{"id": "1", "email": "not-an-email"}]
        blocks = blocker.partition(records)
        assert len(blocks) == 1
        assert blocks[0][0]["id"] == "1"

    def test_missing_field_goes_to_unknown_bucket(self) -> None:
        strat = _strategy(BlockingKeyType.EMAIL_DOMAIN, "email")
        blocker = RecordBlocker(strat)
        records = [{"id": "1"}]
        blocks = blocker.partition(records)
        assert len(blocks) == 1


# ---------------------------------------------------------------------------
# PHONE_NORMALIZED blocking
# ---------------------------------------------------------------------------


class TestPhoneNormalizedBlocking:
    def test_same_prefix_in_same_block(self) -> None:
        strat = _strategy(BlockingKeyType.PHONE_NORMALIZED, "phone")
        blocker = RecordBlocker(strat)
        records = [
            {"id": "1", "phone": "+1 (415) 555-1234"},
            {"id": "2", "phone": "14155559999"},
            {"id": "3", "phone": "+44 20 7946 0958"},
        ]
        blocks = blocker.partition(records)
        assert sum(len(b) for b in blocks) == 3

    def test_short_phone_uses_full_digits(self) -> None:
        strat = _strategy(BlockingKeyType.PHONE_NORMALIZED, "phone")
        blocker = RecordBlocker(strat)
        records = [{"id": "1", "phone": "12345"}]  # < 7 digits
        blocks = blocker.partition(records)
        assert len(blocks) == 1

    def test_no_digits_goes_to_no_phone_bucket(self) -> None:
        strat = _strategy(BlockingKeyType.PHONE_NORMALIZED, "phone")
        blocker = RecordBlocker(strat)
        records = [{"id": "1", "phone": "N/A"}]
        blocks = blocker.partition(records)
        assert len(blocks) == 1


# ---------------------------------------------------------------------------
# NAME_FIRST3 blocking
# ---------------------------------------------------------------------------


class TestNameFirst3Blocking:
    def test_same_first3_in_same_block(self) -> None:
        strat = _strategy(BlockingKeyType.NAME_FIRST3, "name")
        blocker = RecordBlocker(strat)
        records = [
            {"id": "1", "name": "Alice Smith"},
            {"id": "2", "name": "Alice Jones"},
            {"id": "3", "name": "Bob Green"},
        ]
        blocks = blocker.partition(records)
        alice_block = next(
            b for b in blocks if any(r["name"].startswith("Alice") for r in b)
        )
        assert len(alice_block) == 2

    def test_short_name_uses_full_alpha(self) -> None:
        strat = _strategy(BlockingKeyType.NAME_FIRST3, "name")
        blocker = RecordBlocker(strat)
        records = [{"id": "1", "name": "AB"}]  # fewer than 3 alpha chars
        blocks = blocker.partition(records)
        assert len(blocks) == 1

    def test_empty_name_goes_to_empty_bucket(self) -> None:
        strat = _strategy(BlockingKeyType.NAME_FIRST3, "name")
        blocker = RecordBlocker(strat)
        records = [{"id": "1", "name": "123"}]  # no alpha chars
        blocks = blocker.partition(records)
        assert len(blocks) == 1


# ---------------------------------------------------------------------------
# RECORD_ID_PREFIX blocking
# ---------------------------------------------------------------------------


class TestRecordIdPrefixBlocking:
    def test_same_prefix_grouped(self) -> None:
        strat = _strategy(BlockingKeyType.RECORD_ID_PREFIX, "record_id")
        blocker = RecordBlocker(strat)
        records = [
            {"id": "1", "record_id": "ABCDEFGH-001"},
            {"id": "2", "record_id": "ABCDEFGH-002"},
            {"id": "3", "record_id": "ZZZZXXX0-999"},
        ]
        blocks = blocker.partition(records)
        abc_block = next(
            b for b in blocks if any(r["record_id"].startswith("ABCDEFGH") for r in b)
        )
        assert len(abc_block) == 2

    def test_short_id_uses_full_value(self) -> None:
        strat = _strategy(BlockingKeyType.RECORD_ID_PREFIX, "record_id")
        blocker = RecordBlocker(strat)
        records = [{"id": "1", "record_id": "abc"}]  # < 8 chars
        blocks = blocker.partition(records)
        assert len(blocks) == 1


# ---------------------------------------------------------------------------
# Oversized block subdivision
# ---------------------------------------------------------------------------


class TestOversizedBlockSubdivision:
    def test_oversized_block_split_into_chunks(self) -> None:
        strat = _strategy(BlockingKeyType.EMAIL_DOMAIN, "email", max_block_size=3)
        blocker = RecordBlocker(strat)
        # 7 records with same domain → should be split into [3, 3, 1]
        records = [{"id": str(i), "email": f"user{i}@same.com"} for i in range(7)]
        blocks = blocker.partition(records)
        assert all(len(b) <= 3 for b in blocks)
        assert sum(len(b) for b in blocks) == 7

    def test_no_records_lost_in_subdivision(self) -> None:
        strat = _strategy(BlockingKeyType.EMAIL_DOMAIN, "email", max_block_size=2)
        blocker = RecordBlocker(strat)
        records = [{"id": str(i), "email": f"user{i}@x.com"} for i in range(10)]
        blocks = blocker.partition(records)
        ids = {r["id"] for b in blocks for r in b}
        assert ids == {str(i) for i in range(10)}


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------


class TestEmptyInput:
    def test_empty_records_returns_empty_blocks(self) -> None:
        strat = _strategy(BlockingKeyType.EMAIL_DOMAIN, "email")
        blocker = RecordBlocker(strat)
        blocks = blocker.partition([])
        assert blocks == []


# ---------------------------------------------------------------------------
# BlockingStrategy dataclass
# ---------------------------------------------------------------------------


class TestBlockingStrategy:
    def test_default_max_block_size(self) -> None:
        strat = BlockingStrategy(
            key_type=BlockingKeyType.EMAIL_DOMAIN,
            source_field="email",
        )
        assert strat.max_block_size == 1_000

    def test_immutable(self) -> None:
        strat = BlockingStrategy(
            key_type=BlockingKeyType.EMAIL_DOMAIN,
            source_field="email",
        )
        with pytest.raises((AttributeError, TypeError)):
            strat.source_field = "other"  # type: ignore[misc]
