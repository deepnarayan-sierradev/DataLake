"""
Contract tests for the configuration guarantees the runtime depends on
(DL-CFG-04, DL-CFG-05, DL-CFG-10, DL-CFG-12, DL-CFG-14, DL-CFG-15).

These are the tests that catch the whole class of defect DL-11 addresses: a writer that
overwrites a version in place, a cache with an undeclared invalidation basis, a dead
invalidation API, or a config shape the runtime cannot read.
"""

from __future__ import annotations

import importlib
import inspect
import json

import boto3
import pytest
from moto import mock_aws

from config_propagation.cache_invalidation import (
    CACHE_CONTRACTS,
    CacheContract,
    InvalidationBasis,
)
from config_propagation.capability import (
    CAPABILITY_POLICIES,
    ConfigCapability,
    RetentionReprocessingConflictError,
    policy_for,
    validate_retention_against_reprocessing,
)
from connector_runtime.configuration_repository.configuration_repository import (
    SUPPORTED_CONFIG_SCHEMA_VERSIONS,
    ConfigurationRepositoryClient,
    ConfigurationSchemaIncompatibleError,
)
from contracts.entity_configuration_contract import EntityExtractionConfig
from entity_resolution.resolution_config.resolution_config_registry import (
    ResolutionConfigRegistry,
    ResolutionConfigVersionPinError,
)

_REGION = "us-east-1"
_BUCKET = "edl-curated-test"


class TestReprocessingPolicyMatrix:
    def test_every_capability_declares_a_policy(self):
        missing = [c for c in ConfigCapability if c not in CAPABILITY_POLICIES]
        assert not missing, f"Capabilities with no declared reprocessing policy: {missing}"

    def test_reprocess_eligible_capabilities_declare_a_window_and_layer(self):
        for capability, declared in CAPABILITY_POLICIES.items():
            if not declared.is_reprocess_eligible:
                continue
            assert declared.minimum_reprocessing_window_days, (
                f"{capability.value} is reprocess-eligible but declares no minimum window; "
                "DL-CFG-12 cannot validate retention against it."
            )
            assert declared.source_layer.value != "none", (
                f"{capability.value} is reprocess-eligible but names no source layer."
            )

    def test_semantic_model_is_restatement_flagged(self):
        # Read-time definitions restate history silently unless announced (DL-CFG-13).
        assert policy_for(ConfigCapability.SEMANTIC_MODEL).restatement_flagged is True

    def test_serving_store_change_forces_a_full_reload(self):
        assert policy_for(ConfigCapability.SERVING_STORE).full_reload_on_change is True

    def test_retention_shorter_than_the_window_is_rejected(self):
        with pytest.raises(RetentionReprocessingConflictError, match="already expired"):
            validate_retention_against_reprocessing(ConfigCapability.SURVIVORSHIP, 30)

    def test_retention_at_least_the_window_is_accepted(self):
        validate_retention_against_reprocessing(ConfigCapability.SURVIVORSHIP, 400)

    def test_apply_forward_capability_has_no_retention_constraint(self):
        validate_retention_against_reprocessing(ConfigCapability.EXTRACTION_SCHEDULE, 1)

    def test_entity_config_rejects_retention_below_its_reprocessing_window(self):
        with pytest.raises(ValueError, match="shorter than"):
            EntityExtractionConfig(
                source_id="salesforce",
                entity_id="salesforce-account",
                config_version="1.0.0",
                target_raw_s3_prefix="s3://raw/salesforce/account/",
                schema_snapshot_s3_prefix="s3://snap/salesforce/account/",
                watermark_field="SystemModstamp",
                retention_days=30,
                minimum_reprocessing_window_days=395,
            )


class TestDeclaredCacheInvalidation:
    def test_every_capability_has_at_least_one_declared_cache_contract(self):
        declared = {c.capability for c in CACHE_CONTRACTS}
        missing = [c for c in ConfigCapability if c not in declared]
        assert not missing, (
            f"Capabilities with no declared cache-invalidation basis: {missing}. "
            "Every registry must state its contract (DL-CFG-04)."
        )

    def test_ttl_bounded_caches_declare_their_bound(self):
        with pytest.raises(ValueError, match="must declare its bound"):
            CacheContract(
                capability=ConfigCapability.CREDENTIALS,
                registry="x",
                basis=InvalidationBasis.TTL_BOUNDED,
            )

    def test_signal_driven_caches_declare_the_signal_that_calls_them(self):
        with pytest.raises(ValueError, match="otherwise the API is dead"):
            CacheContract(
                capability=ConfigCapability.ENTITY_RESOLUTION,
                registry="x",
                basis=InvalidationBasis.SIGNAL_DRIVEN,
                invalidation_method="invalidate_entity_type",
            )

    def test_credential_cache_bound_matches_the_runtime_constant(self):
        from connector_runtime.credential_client import DEFAULT_CREDENTIAL_CACHE_TTL_SECONDS

        declared = next(c for c in CACHE_CONTRACTS if c.basis is InvalidationBasis.TTL_BOUNDED)
        assert declared.ttl_seconds == DEFAULT_CREDENTIAL_CACHE_TTL_SECONDS

    def test_declared_invalidation_methods_exist_and_are_wired(self):
        # DL-CFG-04 acceptance: a declared invalidation API must exist AND be called by the
        # named signal — a dead API implies a guarantee that does not exist.
        for contract in CACHE_CONTRACTS:
            if contract.basis is not InvalidationBasis.SIGNAL_DRIVEN:
                continue
            module_path, class_name = contract.registry.rsplit(".", 1)
            module = importlib.import_module(module_path)
            registry_cls = getattr(module, class_name)
            assert hasattr(registry_cls, contract.invalidation_method), (
                f"{contract.registry} declares {contract.invalidation_method!r} but does not "
                "define it."
            )
            _, signal_method = contract.signal_source.rsplit(".", 1)
            signal_source = inspect.getsource(getattr(registry_cls, signal_method))
            assert contract.invalidation_method in signal_source, (
                f"{contract.signal_source} does not call {contract.invalidation_method!r}; "
                "the invalidation API would be dead."
            )


@mock_aws
class TestPublishesAlwaysBumpVersions:
    """DL-CFG-05: an in-place overwrite would be served stale from warm containers."""

    def _registry(self) -> ResolutionConfigRegistry:
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_BUCKET)
        return ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)

    @staticmethod
    def _bodies(version: str) -> tuple[dict, dict]:
        match_rules = {
            "entity_type": "company",
            "rule_set_version": version,
            "rules": [
                {
                    "rule_id": "exact-account",
                    "strategy": "deterministic",
                    "fields": [{"field_name": "account_id"}],
                }
            ],
        }
        survivorship = {
            "entity_type": "company",
            "policy_version": version,
            "output_fields": ["account_id"],
        }
        return match_rules, survivorship

    def test_a_publish_writes_a_new_version_object(self):
        registry = self._registry()
        match_v1, surv_v1 = self._bodies("v1")
        registry.publish("company", "demo", match_v1, surv_v1)
        match_v2, surv_v2 = self._bodies("v2")
        registry.publish("company", "demo", match_v2, surv_v2)

        s3 = boto3.client("s3", region_name=_REGION)
        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=_BUCKET, Prefix="demo/entity-resolution/company/")[
                "Contents"
            ]
        }
        assert "demo/entity-resolution/company/match_rules_v1.json" in keys
        assert "demo/entity-resolution/company/match_rules_v2.json" in keys

    def test_the_latest_pointer_moves_rather_than_the_version_body(self):
        registry = self._registry()
        registry.publish("company", "demo", *self._bodies("v1"))
        registry.publish("company", "demo", *self._bodies("v2"))
        s3 = boto3.client("s3", region_name=_REGION)
        pointer = json.loads(
            s3.get_object(Bucket=_BUCKET, Key="demo/entity-resolution/company/latest.json")[
                "Body"
            ].read()
        )
        assert pointer == {"match_rules_version": "v2", "survivorship_version": "v2"}
        v1_body = json.loads(
            s3.get_object(Bucket=_BUCKET, Key="demo/entity-resolution/company/match_rules_v1.json")[
                "Body"
            ].read()
        )
        assert v1_body["rule_set_version"] == "v1"

    def test_publish_invalidates_the_in_process_cache(self):
        registry = self._registry()
        registry.publish("company", "demo", *self._bodies("v1"))
        registry.load("company", "demo")
        assert registry._cache
        registry.publish("company", "demo", *self._bodies("v2"))
        assert not registry._cache

    def test_pinned_load_never_resolves_latest(self):
        registry = self._registry()
        registry.publish("company", "demo", *self._bodies("v1"))
        with pytest.raises(ResolutionConfigVersionPinError, match="never resolve 'latest'"):
            registry.load("company", "demo", pinned=True)

    def test_pinned_load_of_a_deleted_version_fails_closed(self):
        registry = self._registry()
        registry.publish("company", "demo", *self._bodies("v1"))
        with pytest.raises(ResolutionConfigVersionPinError, match="no longer resolves"):
            registry.load("company", "demo", "v9", "v9", pinned=True)

    def test_invalidate_entity_type_is_prefix_exact(self):
        registry = self._registry()
        registry._cache["demo/company/v1/v1"] = object()
        registry._cache["demo/company-extra/v1/v1"] = object()
        assert registry.invalidate_entity_type("company", "demo") == 1
        assert "demo/company-extra/v1/v1" in registry._cache


@mock_aws
class TestConfigSchemaCompatibility:
    """DL-CFG-14: a config outside the supported range fails closed with an actionable error."""

    def _client(self) -> ConfigurationRepositoryClient:
        boto3.client("dynamodb", region_name=_REGION).create_table(
            TableName="EdlEntityExtractionConfig",
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
        return ConfigurationRepositoryClient(environment="dev", region_name=_REGION)

    @staticmethod
    def _config(**overrides) -> EntityExtractionConfig:
        base = {
            "source_id": "hubspot",
            "entity_id": "hubspot-company",
            "config_version": "1.0.0",
            "tenant_code": "evive",
            "target_raw_s3_prefix": "s3://raw/hubspot/company/",
            "schema_snapshot_s3_prefix": "s3://snap/hubspot/company/",
            "watermark_field": "hs_lastmodifieddate",
        }
        return EntityExtractionConfig(**{**base, **overrides})

    def test_supported_version_loads(self):
        client = self._client()
        client.save_config(self._config())
        loaded = client.load_config("hubspot", "hubspot-company", "evive")
        assert loaded.config_schema_version in SUPPORTED_CONFIG_SCHEMA_VERSIONS

    def test_unsupported_version_fails_closed(self):
        client = self._client()
        client.save_config(self._config())
        client._table.update_item(
            Key={"source_id": "evive#hubspot", "entity_id": "hubspot-company"},
            UpdateExpression="SET config_schema_version = :v",
            ExpressionAttributeValues={":v": 99},
        )
        with pytest.raises(ConfigurationSchemaIncompatibleError, match="supported range"):
            client.load_config("hubspot", "hubspot-company", "evive")

    def test_connection_scoped_keys_do_not_collide(self):
        client = self._client()
        for franchisee in ("grasons", "brothers-gutters"):
            client.save_config(self._config(connection_id=f"hubspot-{franchisee}"))
        first = client.load_config(
            "hubspot", "hubspot-company", "evive", connection_id="hubspot-grasons"
        )
        second = client.load_config(
            "hubspot", "hubspot-company", "evive", connection_id="hubspot-brothers-gutters"
        )
        assert first.effective_connection_id == "hubspot-grasons"
        assert second.effective_connection_id == "hubspot-brothers-gutters"

    def test_source_id_survives_the_round_trip(self):
        client = self._client()
        client.save_config(self._config(connection_id="hubspot-grasons"))
        listed = client.list_configs_for_tenant("evive")
        assert [c.source_id for c in listed] == ["hubspot"]
        assert [c.connection_id for c in listed] == ["hubspot-grasons"]
