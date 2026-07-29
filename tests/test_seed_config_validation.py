"""
Seeded configs must be extractable as written (F9).

`netsuite-customer` shipped with `connector_params: {}` while
`NetSuiteConnectorParams.record_type` is required under `extra="forbid"`, so extraction for that
entity raised ValidationError the moment it ran. No unit test could see it: the defect was in the
seed *data*, not in code. `docs/KNOWN_GAPS_AND_ROADMAP.md` recorded it as item 9 and it stayed
open across two audits.

The seed script now validates against the same registry the extraction handler uses, so the two
cannot disagree about what a valid config is.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from seed_entity_config import (
    SeedValidationError,
    _build_records,
    _validate_connector_params,
)


class TestSeededConfigsAreExtractable:
    def test_every_seeded_record_passes_its_params_model(self) -> None:
        # The regression assertion: this failed before `record_type` was added.
        _validate_connector_params(_build_records("123456789012", tenant_code="demo"))

    def test_netsuite_customer_declares_a_record_type(self) -> None:
        records = _build_records("123456789012", tenant_code="demo")
        netsuite = [r for r in records if r["entity_id"] == "netsuite-customer"]
        assert netsuite, "the netsuite-customer seed record disappeared"
        assert netsuite[0]["connector_params"].get("record_type")  # type: ignore[union-attr]


class TestValidatorRejectsWhatShipped:
    def test_empty_params_for_netsuite_is_rejected(self) -> None:
        # The exact record that was live: reproduce it and assert the gate now refuses it.
        records = _build_records("123456789012", tenant_code="demo")
        for record in records:
            if record["entity_id"] == "netsuite-customer":
                record["connector_params"] = {}
        with pytest.raises(SeedValidationError, match="netsuite-customer"):
            _validate_connector_params(records)

    def test_unknown_param_is_rejected(self) -> None:
        # extra="forbid" is the other half; a typo'd key must not pass silently.
        records = _build_records("123456789012", tenant_code="demo")
        for record in records:
            if record["entity_id"] == "netsuite-customer":
                record["connector_params"] = {"record_type": "customer", "recordtype": "customer"}
        with pytest.raises(SeedValidationError):
            _validate_connector_params(records)

    def test_a_valid_record_set_raises_nothing(self) -> None:
        # Positive control: a validator that always raised would pass both tests above.
        _validate_connector_params(_build_records("123456789012", tenant_code="evive"))
