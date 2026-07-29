"""Run-level config pinning tests (DL-CFG-01, DL-CFG-02, DL-CFG-03)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from config_propagation.capability import ConfigCapability
from config_propagation.pinned_versions import (
    ConfigVersionMismatchError,
    ConfigVersionPinError,
    PinnedConfigVersions,
)
from config_propagation.pinning_service import ConfigPinningService

_NOW = datetime.now(UTC).isoformat()


def _pinned(**versions) -> PinnedConfigVersions:
    return PinnedConfigVersions(versions=versions, pinned_at=_NOW)


class TestPinnedConfigVersions:
    def test_require_returns_the_pinned_version(self):
        pinned = _pinned(entity_resolution="v3")
        assert pinned.require(ConfigCapability.ENTITY_RESOLUTION) == "v3"

    def test_require_fails_closed_on_a_missing_pin(self):
        with pytest.raises(ConfigVersionPinError, match="must not fall back to 'latest'"):
            _pinned().require(ConfigCapability.ENTITY_RESOLUTION)

    def test_latest_is_rejected_as_a_pinned_value(self):
        with pytest.raises(ValueError, match="not a version"):
            _pinned(entity_resolution="latest")

    def test_unknown_capability_is_rejected(self):
        with pytest.raises(ValueError, match="not a known ConfigCapability"):
            PinnedConfigVersions(versions={"not_a_capability": "v1"}, pinned_at=_NOW)

    def test_unsafe_version_string_is_rejected(self):
        with pytest.raises(ValueError, match="not a safe version identifier"):
            _pinned(entity_resolution="v1; DROP TABLE x")

    def test_mismatch_is_detected(self):
        pinned = _pinned(survivorship="v2")
        pinned.assert_matches(ConfigCapability.SURVIVORSHIP, "v2")
        with pytest.raises(ConfigVersionMismatchError, match="exactly one configuration"):
            pinned.assert_matches(ConfigCapability.SURVIVORSHIP, "v3")

    def test_unpinned_capability_never_reports_a_mismatch(self):
        _pinned().assert_matches(ConfigCapability.SURVIVORSHIP, "v9")

    def test_payload_round_trip(self):
        pinned = _pinned(field_mapping="v4", semantic_model="2026-07-01")
        restored = PinnedConfigVersions.from_payload(pinned.to_payload())
        assert restored == pinned

    def test_from_payload_tolerates_a_pre_pinning_run(self):
        assert PinnedConfigVersions.from_payload(None) is None
        assert PinnedConfigVersions.from_payload({}) is None

    def test_audit_fingerprint_is_order_independent(self):
        a = _pinned(field_mapping="v1", survivorship="v2")
        b = _pinned(survivorship="v2", field_mapping="v1")
        assert a.audit_fingerprint() == b.audit_fingerprint()

    def test_with_capability_is_additive(self):
        pinned = _pinned(field_mapping="v1").with_capability(ConfigCapability.SURVIVORSHIP, "v9")
        assert pinned.get(ConfigCapability.SURVIVORSHIP) == "v9"
        assert pinned.get(ConfigCapability.FIELD_MAPPING) == "v1"


class TestConfigPinningService:
    def test_resolves_every_pointer_once(self):
        calls: list[str] = []

        def resolver(version: str):
            def resolve(tenant_code: str, entity_key: str) -> str:
                calls.append(f"{tenant_code}/{entity_key}")
                return version

            return resolve

        service = ConfigPinningService(
            {
                ConfigCapability.FIELD_MAPPING: resolver("v2"),
                ConfigCapability.SURVIVORSHIP: resolver("v5"),
            }
        )
        pinned = service.pin("demo", "salesforce-account")
        assert pinned.versions == {"field_mapping": "v2", "survivorship": "v5"}
        assert len(calls) == 2

    def test_a_failing_resolver_does_not_block_the_run(self):
        def boom(tenant_code: str, entity_key: str) -> str:
            raise RuntimeError("S3 unavailable")

        service = ConfigPinningService(
            {
                ConfigCapability.FIELD_MAPPING: boom,
                ConfigCapability.SURVIVORSHIP: lambda t, e: "v1",
            }
        )
        pinned = service.pin("demo", "salesforce-account")
        assert pinned.versions == {"survivorship": "v1"}

    def test_a_resolver_returning_none_contributes_nothing(self):
        service = ConfigPinningService({ConfigCapability.FIELD_MAPPING: lambda t, e: None})
        assert service.pin("demo", "x-entity").versions == {}

    def test_capability_subset_can_be_requested(self):
        service = ConfigPinningService(
            {
                ConfigCapability.FIELD_MAPPING: lambda t, e: "v1",
                ConfigCapability.SURVIVORSHIP: lambda t, e: "v2",
            }
        )
        pinned = service.pin("demo", "x-entity", capabilities=(ConfigCapability.SURVIVORSHIP,))
        assert pinned.versions == {"survivorship": "v2"}
