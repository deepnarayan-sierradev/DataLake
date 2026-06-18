"""Tests for DataClassificationPolicy and FieldMaskingApplier — Phase 9."""

from __future__ import annotations

import pytest

from governance.data_classification_policy import (
    DataClassificationLevel,
    EntityClassificationPolicy,
    FieldClassification,
    FieldMaskingApplier,
    MaskingStrategy,
    TokenisationKeyMissingError,
    auto_classify_field,
)


def _policy(*field_classifications, version="1.0.0"):
    return EntityClassificationPolicy(
        source_id="salesforce",
        entity_id="salesforce-contact",
        policy_version=version,
        field_classifications=tuple(field_classifications),
    )


def _fc(field, classification, strategy, visible=4):
    return FieldClassification(
        field_name=field,
        classification=classification,
        masking_strategy=strategy,
        visible_chars=visible,
    )


class TestAutoClassifyField:
    def test_email_field_is_pii(self):
        assert auto_classify_field("email") == DataClassificationLevel.PII
        assert auto_classify_field("Email") == DataClassificationLevel.PII

    def test_phone_field_is_pii(self):
        assert auto_classify_field("phone_number") == DataClassificationLevel.PII

    def test_ssn_is_sensitive_pii(self):
        assert auto_classify_field("ssn") == DataClassificationLevel.SENSITIVE_PII

    def test_credit_card_is_sensitive_pii(self):
        assert auto_classify_field("credit_card_number") == DataClassificationLevel.SENSITIVE_PII

    def test_regular_field_is_internal(self):
        assert auto_classify_field("account_name") == DataClassificationLevel.INTERNAL
        assert auto_classify_field("created_at") == DataClassificationLevel.INTERNAL

    def test_ip_address_is_pii(self):
        assert auto_classify_field("ip_address") == DataClassificationLevel.PII


class TestEntityClassificationPolicyPiiFields:
    def test_pii_field_names_property(self):
        policy = _policy(
            _fc("email", DataClassificationLevel.PII, MaskingStrategy.HASH),
            _fc("name", DataClassificationLevel.INTERNAL, MaskingStrategy.NONE),
            _fc("ssn", DataClassificationLevel.SENSITIVE_PII, MaskingStrategy.REDACT),
        )
        assert policy.pii_field_names == frozenset({"email", "ssn"})


class TestFieldMaskingApplier:
    _SECRET = b"test-secret-key-32bytes-padding!!"

    def setup_method(self, method=None):
        self.applier = FieldMaskingApplier(tokenisation_secret=self._SECRET)

    def test_no_masking_passes_value_through(self):
        policy = _policy(_fc("name", DataClassificationLevel.INTERNAL, MaskingStrategy.NONE))
        records = [{"name": "Alice"}]
        result = self.applier.apply(records, policy)
        assert result[0]["name"] == "Alice"

    def test_full_mask_replaces_with_asterisks(self):
        policy = _policy(
            _fc("ssn", DataClassificationLevel.SENSITIVE_PII, MaskingStrategy.FULL_MASK)
        )
        records = [{"ssn": "123-45-6789"}]
        result = self.applier.apply(records, policy)
        assert result[0]["ssn"] == "***"

    def test_redact_replaces_with_redacted(self):
        policy = _policy(
            _fc("passport", DataClassificationLevel.SENSITIVE_PII, MaskingStrategy.REDACT)
        )
        records = [{"passport": "X1234567"}]
        result = self.applier.apply(records, policy)
        assert result[0]["passport"] == "REDACTED"

    def test_partial_mask_keeps_last_n_chars(self):
        policy = _policy(
            _fc("email", DataClassificationLevel.PII, MaskingStrategy.PARTIAL_MASK, visible=4)
        )
        records = [{"email": "alice@example.com"}]
        result = self.applier.apply(records, policy)
        masked = result[0]["email"]
        assert masked.endswith(".com")
        assert masked.startswith("*")

    def test_partial_mask_short_value(self):
        policy = _policy(
            _fc("code", DataClassificationLevel.PII, MaskingStrategy.PARTIAL_MASK, visible=4)
        )
        records = [{"code": "AB"}]
        result = self.applier.apply(records, policy)
        assert result[0]["code"] == "**"

    def test_hash_is_deterministic(self):
        policy = _policy(_fc("email", DataClassificationLevel.PII, MaskingStrategy.HASH))
        records = [{"email": "alice@example.com"}]
        r1 = self.applier.apply(records, policy)
        r2 = self.applier.apply(records, policy)
        assert r1[0]["email"] == r2[0]["email"]
        assert r1[0]["email"] != "alice@example.com"

    def test_tokenise_produces_stable_pseudonym(self):
        policy = _policy(_fc("email", DataClassificationLevel.PII, MaskingStrategy.TOKENISE))
        records = [{"email": "alice@example.com"}]
        r1 = self.applier.apply(records, policy)
        r2 = self.applier.apply(records, policy)
        assert r1[0]["email"].startswith("TOKEN-")
        assert r1[0]["email"] == r2[0]["email"]

    def test_original_records_not_mutated(self):
        policy = _policy(_fc("email", DataClassificationLevel.PII, MaskingStrategy.FULL_MASK))
        original = {"email": "alice@example.com", "name": "Alice"}
        records = [original]
        self.applier.apply(records, policy)
        assert records[0]["email"] == "alice@example.com"  # unchanged

    def test_unclassified_fields_passed_through(self):
        policy = _policy(_fc("email", DataClassificationLevel.PII, MaskingStrategy.HASH))
        records = [{"email": "alice@example.com", "account_id": "001"}]
        result = self.applier.apply(records, policy)
        assert result[0]["account_id"] == "001"

    def test_null_value_not_masked(self):
        policy = _policy(_fc("email", DataClassificationLevel.PII, MaskingStrategy.FULL_MASK))
        records = [{"email": None}]
        result = self.applier.apply(records, policy)
        assert result[0]["email"] is None

    def test_multiple_records_all_masked(self):
        policy = _policy(_fc("ssn", DataClassificationLevel.SENSITIVE_PII, MaskingStrategy.REDACT))
        records = [{"ssn": "111-11-1111"}, {"ssn": "222-22-2222"}]
        result = self.applier.apply(records, policy)
        assert all(r["ssn"] == "REDACTED" for r in result)

    def test_tokenise_raises_if_no_secret(self):
        """FieldMaskingApplier without secret should raise TokenisationKeyMissingError."""
        applier_no_secret = FieldMaskingApplier()
        policy = _policy(_fc("email", DataClassificationLevel.PII, MaskingStrategy.TOKENISE))
        records = [{"email": "alice@example.com"}]
        with pytest.raises(TokenisationKeyMissingError):
            applier_no_secret.apply(records, policy)
