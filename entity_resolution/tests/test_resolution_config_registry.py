"""
Tests for ResolutionConfigRegistry.

Covers:
- Load company match rules → correct MatchRuleSet (deterministic + probabilistic, blocking)
- Load person survivorship → output_fields and attribute_rules correct
- from_registry factory on GoldenRecordPublisher
- ResolutionConfigNotFoundError raised for missing entity type
- ResolutionConfigParseError raised for malformed JSON
- latest.json pointer resolves match_rules_version / survivorship_version
- publish() writes three objects (mr, sv, pointer) and invalidates cache
- Unsafe entity_type / version strings rejected before S3 is called
"""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from entity_resolution.canonical_record_publisher.canonical_record_publisher import (
    GoldenRecordPublisher,
)
from entity_resolution.matching_engine.match_rule_engine import (
    DeterministicMatchRule,
    ProbabilisticMatchRule,
)
from entity_resolution.matching_engine.record_blocker import BlockingKeyType
from entity_resolution.resolution_config.resolution_config_registry import (
    ResolutionConfigNotFoundError,
    ResolutionConfigParseError,
    ResolutionConfigRegistry,
)
from entity_resolution.survivorship_policy import SurvivorshipStrategy

_REGION = "us-east-1"
_BUCKET = "test-config-bucket"
_ANALYTICS_BUCKET = "test-analytics-bucket"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def s3_client():
    with mock_aws():
        client = boto3.client("s3", region_name=_REGION)
        client.create_bucket(Bucket=_BUCKET)
        yield client


def _put(client, key: str, body: object) -> None:
    client.put_object(
        Bucket=_BUCKET,
        Key=key,
        Body=json.dumps(body).encode("utf-8"),
        ContentType="application/json",
    )


_COMPANY_MATCH_RULES = {
    "entity_type": "company",
    "rule_set_version": "v1",
    "blocking": {
        "key_type": "email_domain",
        "source_field": "email_address",
        "max_block_size": 500,
    },
    "rules": [
        {
            "rule_id": "email-exact",
            "strategy": "deterministic",
            "fields": [{"field_name": "email_address", "normalise": True}],
        },
        {
            "rule_id": "name-country-fuzzy",
            "strategy": "probabilistic",
            "match_threshold": 0.85,
            "fields": [
                {"field_name": "full_name", "weight": 0.70, "similarity_kind": "jaro_winkler"},
                {"field_name": "billing_country", "weight": 0.30, "similarity_kind": "exact"},
            ],
        },
    ],
}

_COMPANY_SURVIVORSHIP = {
    "entity_type": "company",
    "policy_version": "v1",
    "output_fields": [
        "full_name",
        "email_address",
        "phone_number",
        "annual_revenue",
        "employee_count",
        "credit_limit",
        "outstanding_balance",
        "currency_code",
        "billing_country",
        "billing_state",
        "industry",
        "is_active",
        "created_date",
        "last_modified_date",
    ],
    "default_strategy": "first_non_null",
    "attribute_rules": [
        {"canonical_field": "full_name", "strategy": "source_priority", "source_priority": ["netsuite", "salesforce"]},
        {"canonical_field": "email_address", "strategy": "source_priority", "source_priority": ["netsuite", "salesforce"]},
        {"canonical_field": "annual_revenue", "strategy": "most_recent", "timestamp_field": "last_modified_date"},
        {"canonical_field": "credit_limit", "strategy": "source_priority", "source_priority": ["netsuite", "salesforce"]},
        {"canonical_field": "outstanding_balance", "strategy": "source_priority", "source_priority": ["netsuite", "salesforce"]},
        {"canonical_field": "currency_code", "strategy": "source_priority", "source_priority": ["netsuite", "salesforce"]},
        {"canonical_field": "billing_country", "strategy": "source_priority", "source_priority": ["netsuite", "salesforce"]},
        {"canonical_field": "billing_state", "strategy": "source_priority", "source_priority": ["netsuite", "salesforce"]},
        {"canonical_field": "industry", "strategy": "source_priority", "source_priority": ["salesforce", "netsuite"]},
        {"canonical_field": "is_active", "strategy": "source_priority", "source_priority": ["netsuite", "salesforce"]},
        {"canonical_field": "phone_number", "strategy": "source_priority", "source_priority": ["netsuite", "salesforce"]},
        {"canonical_field": "employee_count", "strategy": "source_priority", "source_priority": ["salesforce", "netsuite"]},
        {"canonical_field": "created_date", "strategy": "most_recent", "timestamp_field": "created_date"},
        {"canonical_field": "last_modified_date", "strategy": "most_recent", "timestamp_field": "last_modified_date"},
    ],
}

_PERSON_MATCH_RULES = {
    "entity_type": "person",
    "rule_set_version": "v1",
    "rules": [
        {
            "rule_id": "email-exact",
            "strategy": "deterministic",
            "fields": [{"field_name": "email_address", "normalise": True}],
        }
    ],
}

_PERSON_SURVIVORSHIP = {
    "entity_type": "person",
    "policy_version": "v1",
    "output_fields": [
        "full_name",
        "first_name",
        "last_name",
        "email_address",
        "phone_number",
        "job_title",
        "department",
        "account_id",
        "mailing_country",
        "is_active",
        "created_date",
        "last_modified_date",
    ],
    "default_strategy": "first_non_null",
    "attribute_rules": [
        {"canonical_field": "full_name", "strategy": "source_priority", "source_priority": ["salesforce"]},
        {"canonical_field": "email_address", "strategy": "most_recent", "timestamp_field": "last_modified_date"},
        {"canonical_field": "first_name", "strategy": "source_priority", "source_priority": ["salesforce"]},
        {"canonical_field": "last_name", "strategy": "source_priority", "source_priority": ["salesforce"]},
        {"canonical_field": "phone_number", "strategy": "most_recent", "timestamp_field": "last_modified_date"},
        {"canonical_field": "job_title", "strategy": "most_recent", "timestamp_field": "last_modified_date"},
        {"canonical_field": "department", "strategy": "source_priority", "source_priority": ["salesforce"]},
        {"canonical_field": "account_id", "strategy": "source_priority", "source_priority": ["salesforce"]},
        {"canonical_field": "mailing_country", "strategy": "source_priority", "source_priority": ["salesforce"]},
        {"canonical_field": "is_active", "strategy": "source_priority", "source_priority": ["salesforce"]},
        {"canonical_field": "created_date", "strategy": "most_recent", "timestamp_field": "created_date"},
        {"canonical_field": "last_modified_date", "strategy": "most_recent", "timestamp_field": "last_modified_date"},
    ],
}


# ---------------------------------------------------------------------------
# Helper: seed S3 objects for a single entity
# ---------------------------------------------------------------------------


def _seed_company(client) -> None:
    _put(client, "entity-resolution/company/match_rules_v1.json", _COMPANY_MATCH_RULES)
    _put(client, "entity-resolution/company/survivorship_v1.json", _COMPANY_SURVIVORSHIP)
    _put(client, "entity-resolution/company/latest.json", {"match_rules_version": "v1", "survivorship_version": "v1"})


def _seed_person(client) -> None:
    _put(client, "entity-resolution/person/match_rules_v1.json", _PERSON_MATCH_RULES)
    _put(client, "entity-resolution/person/survivorship_v1.json", _PERSON_SURVIVORSHIP)
    _put(client, "entity-resolution/person/latest.json", {"match_rules_version": "v1", "survivorship_version": "v1"})


# ---------------------------------------------------------------------------
# Tests: company match rules
# ---------------------------------------------------------------------------


def test_load_company_match_rules_structure(s3_client):
    _seed_company(s3_client)
    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    config = registry.load("company")

    rule_set = config.match_rule_set
    assert rule_set.entity_type == "company"
    assert rule_set.rule_set_version == "v1"
    assert len(rule_set.rules) == 2


def test_load_company_deterministic_rule(s3_client):
    _seed_company(s3_client)
    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    config = registry.load("company")

    det_rule = next(r for r in config.match_rule_set.rules if isinstance(r, DeterministicMatchRule))
    assert det_rule.rule_id == "email-exact"
    assert len(det_rule.fields) == 1
    assert det_rule.fields[0].field_name == "email_address"
    assert det_rule.fields[0].normalise is True


def test_load_company_probabilistic_rule(s3_client):
    _seed_company(s3_client)
    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    config = registry.load("company")

    prob_rule = next(r for r in config.match_rule_set.rules if isinstance(r, ProbabilisticMatchRule))
    assert prob_rule.rule_id == "name-country-fuzzy"
    assert prob_rule.match_threshold == pytest.approx(0.85)
    assert len(prob_rule.fields) == 2
    assert prob_rule.fields[0].field_name == "full_name"
    assert prob_rule.fields[0].weight == pytest.approx(0.70)


def test_load_company_blocking_strategy(s3_client):
    _seed_company(s3_client)
    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    config = registry.load("company")

    blocking = config.match_rule_set.blocking_strategy
    assert blocking is not None
    assert blocking.key_type == BlockingKeyType.EMAIL_DOMAIN
    assert blocking.source_field == "email_address"
    assert blocking.max_block_size == 500


# ---------------------------------------------------------------------------
# Tests: person survivorship
# ---------------------------------------------------------------------------


def test_load_person_survivorship_output_fields(s3_client):
    _seed_person(s3_client)
    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    config = registry.load("person")

    policy = config.survivorship_policy
    assert policy.entity_type == "person"
    assert policy.policy_version == "v1"
    assert "full_name" in policy.output_fields
    assert "email_address" in policy.output_fields
    assert "contact_id" not in policy.output_fields  # source ID excluded


def test_load_person_survivorship_attribute_rules(s3_client):
    _seed_person(s3_client)
    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    config = registry.load("person")

    rules_by_field = {r.canonical_field: r for r in config.survivorship_policy.attribute_rules}
    assert "email_address" in rules_by_field
    assert rules_by_field["email_address"].strategy == SurvivorshipStrategy.MOST_RECENT
    assert "full_name" in rules_by_field
    assert rules_by_field["full_name"].strategy == SurvivorshipStrategy.SOURCE_PRIORITY
    assert rules_by_field["full_name"].source_priority == ("salesforce",)


def test_load_person_default_strategy(s3_client):
    _seed_person(s3_client)
    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    config = registry.load("person")
    assert config.survivorship_policy.default_strategy == SurvivorshipStrategy.FIRST_NON_NULL


# ---------------------------------------------------------------------------
# Tests: latest.json pointer resolution
# ---------------------------------------------------------------------------


def test_latest_pointer_resolves_version(s3_client):
    """When latest.json exists it should be used to resolve version."""
    _put(s3_client, "entity-resolution/company/match_rules_v1.json", _COMPANY_MATCH_RULES)
    _put(s3_client, "entity-resolution/company/survivorship_v1.json", _COMPANY_SURVIVORSHIP)
    _put(s3_client, "entity-resolution/company/latest.json", {"match_rules_version": "v1", "survivorship_version": "v1"})

    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    config = registry.load("company")  # "latest" → "v1" via pointer
    assert config.match_rule_set.rule_set_version == "v1"


def test_explicit_version_bypasses_pointer(s3_client):
    """Explicit version strings skip the latest.json lookup."""
    _put(s3_client, "entity-resolution/company/match_rules_v1.json", _COMPANY_MATCH_RULES)
    _put(s3_client, "entity-resolution/company/survivorship_v1.json", _COMPANY_SURVIVORSHIP)

    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    config = registry.load("company", match_rules_version="v1", survivorship_version="v1")
    assert config.match_rule_set.rule_set_version == "v1"


# ---------------------------------------------------------------------------
# Tests: caching
# ---------------------------------------------------------------------------


def test_registry_caches_loaded_config(s3_client):
    """Second load() call returns cached object — S3 is not called again."""
    _seed_company(s3_client)
    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)

    first = registry.load("company")
    second = registry.load("company")

    assert first is second  # same object in cache


# ---------------------------------------------------------------------------
# Tests: error cases
# ---------------------------------------------------------------------------


def test_not_found_for_missing_entity_type(s3_client):
    """No S3 objects for 'unknown-entity' → ResolutionConfigNotFoundError."""
    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    with pytest.raises(ResolutionConfigNotFoundError, match="unknown-entity"):
        registry.load("unknown-entity")


def test_parse_error_for_malformed_match_rules(s3_client):
    _put(s3_client, "entity-resolution/bad-entity/match_rules_v1.json", {"not_valid": True})
    _put(s3_client, "entity-resolution/bad-entity/survivorship_v1.json", _PERSON_SURVIVORSHIP)

    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    with pytest.raises(ResolutionConfigParseError):
        registry.load("bad-entity", match_rules_version="v1", survivorship_version="v1")


def test_parse_error_for_unknown_match_strategy(s3_client):
    bad_rules = {
        "entity_type": "company",
        "rule_set_version": "v1",
        "rules": [
            {
                "rule_id": "bad-rule",
                "strategy": "magic",  # unknown strategy
                "fields": [{"field_name": "email_address"}],
            }
        ],
    }
    _put(s3_client, "entity-resolution/company/match_rules_v1.json", bad_rules)
    _put(s3_client, "entity-resolution/company/survivorship_v1.json", _COMPANY_SURVIVORSHIP)

    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    with pytest.raises(ResolutionConfigParseError, match="magic"):
        registry.load("company", match_rules_version="v1", survivorship_version="v1")


def test_parse_error_for_out_of_range_threshold(s3_client):
    bad_rules = {
        "entity_type": "company",
        "rule_set_version": "v1",
        "rules": [
            {
                "rule_id": "prob-rule",
                "strategy": "probabilistic",
                "match_threshold": 1.5,  # > 1.0
                "fields": [{"field_name": "full_name", "weight": 1.0, "similarity_kind": "exact"}],
            }
        ],
    }
    _put(s3_client, "entity-resolution/company/match_rules_v1.json", bad_rules)
    _put(s3_client, "entity-resolution/company/survivorship_v1.json", _COMPANY_SURVIVORSHIP)

    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    with pytest.raises(ResolutionConfigParseError, match="match_threshold"):
        registry.load("company", match_rules_version="v1", survivorship_version="v1")


def test_parse_error_when_attribute_rule_field_not_in_output_fields(s3_client):
    bad_sv = {
        "entity_type": "company",
        "policy_version": "v1",
        "output_fields": ["full_name"],  # email_address not in output_fields
        "default_strategy": "first_non_null",
        "attribute_rules": [
            # This field is not in output_fields — should raise
            {"canonical_field": "email_address", "strategy": "source_priority", "source_priority": ["netsuite"]},
        ],
    }
    _put(s3_client, "entity-resolution/company/match_rules_v1.json", _COMPANY_MATCH_RULES)
    _put(s3_client, "entity-resolution/company/survivorship_v1.json", bad_sv)

    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    with pytest.raises(ResolutionConfigParseError, match="email_address"):
        registry.load("company", match_rules_version="v1", survivorship_version="v1")


def test_unsafe_entity_type_rejected(s3_client):
    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    with pytest.raises(ValueError, match="entity_type"):
        registry.load("../etc/passwd")


def test_unsafe_version_rejected(s3_client):
    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    with pytest.raises(ValueError, match="version"):
        registry.load("company", match_rules_version="../../v1")


# ---------------------------------------------------------------------------
# Tests: publish()
# ---------------------------------------------------------------------------


def test_publish_writes_three_s3_objects(s3_client):
    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    mr_key, sv_key = registry.publish(
        entity_type="company",
        match_rules_raw=_COMPANY_MATCH_RULES,
        survivorship_raw=_COMPANY_SURVIVORSHIP,
    )

    assert mr_key == "entity-resolution/company/match_rules_v1.json"
    assert sv_key == "entity-resolution/company/survivorship_v1.json"

    # Verify all three objects exist in S3
    keys_in_bucket = {
        obj["Key"]
        for obj in s3_client.list_objects_v2(Bucket=_BUCKET, Prefix="entity-resolution/company/")["Contents"]
    }
    assert mr_key in keys_in_bucket
    assert sv_key in keys_in_bucket
    assert "entity-resolution/company/latest.json" in keys_in_bucket


def test_publish_latest_pointer_contains_version(s3_client):
    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    registry.publish(
        entity_type="company",
        match_rules_raw=_COMPANY_MATCH_RULES,
        survivorship_raw=_COMPANY_SURVIVORSHIP,
    )

    pointer_body = s3_client.get_object(Bucket=_BUCKET, Key="entity-resolution/company/latest.json")["Body"].read()
    pointer = json.loads(pointer_body)
    assert pointer["match_rules_version"] == "v1"
    assert pointer["survivorship_version"] == "v1"


def test_publish_invalidates_cache(s3_client):
    _seed_company(s3_client)
    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)

    first = registry.load("company")
    assert first is registry.load("company")  # cached

    # Publish new version (still v1 content, but triggers cache invalidation)
    registry.publish(
        entity_type="company",
        match_rules_raw=_COMPANY_MATCH_RULES,
        survivorship_raw=_COMPANY_SURVIVORSHIP,
    )

    # Cache cleared — next load goes back to S3
    assert "company/" not in registry._cache  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Tests: GoldenRecordPublisher.from_registry factory
# ---------------------------------------------------------------------------


def test_from_registry_constructs_publisher(s3_client):
    with mock_aws():
        analytics_client = boto3.client("s3", region_name=_REGION)
        analytics_client.create_bucket(Bucket=_ANALYTICS_BUCKET)
        _seed_company(s3_client)

        registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
        publisher = GoldenRecordPublisher.from_registry(
            registry=registry,
            entity_type="company",
            analytics_s3_bucket=_ANALYTICS_BUCKET,
            region_name=_REGION,
        )

        assert isinstance(publisher, GoldenRecordPublisher)
        assert publisher._match_engine is not None
        assert publisher._survivorship._policy.entity_type == "company"


# ---------------------------------------------------------------------------
# Tests: uncovered branches — targeted gap-fill
# ---------------------------------------------------------------------------


def test_load_without_latest_pointer_defaults_to_v1(s3_client):
    """_load_latest_pointer fallback: no latest.json → default to v1."""
    # Seed the actual v1 files but deliberately omit latest.json
    _put(s3_client, "entity-resolution/company/match_rules_v1.json", _COMPANY_MATCH_RULES)
    _put(s3_client, "entity-resolution/company/survivorship_v1.json", _COMPANY_SURVIVORSHIP)
    # No latest.json uploaded

    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    # load("latest") should fall back to v1 automatically
    config = registry.load("company")
    assert config.match_rule_set.rule_set_version == "v1"


def test_parse_error_for_unknown_blocking_key_type(s3_client):
    """_parse_blocking_strategy raises ResolutionConfigParseError for unknown key_type."""
    bad_rules = {
        "entity_type": "company",
        "rule_set_version": "v1",
        "blocking": {
            "key_type": "alien_key",  # not a valid BlockingKeyType
            "source_field": "email_address",
        },
        "rules": [
            {
                "rule_id": "email-exact",
                "strategy": "deterministic",
                "fields": [{"field_name": "email_address"}],
            }
        ],
    }
    _put(s3_client, "entity-resolution/company/match_rules_v1.json", bad_rules)
    _put(s3_client, "entity-resolution/company/survivorship_v1.json", _COMPANY_SURVIVORSHIP)

    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    with pytest.raises(ResolutionConfigParseError, match="alien_key"):
        registry.load("company", match_rules_version="v1", survivorship_version="v1")


def test_parse_error_for_missing_survivorship_required_field(s3_client):
    """_parse_survivorship_policy raises ResolutionConfigParseError for missing 'policy_version'."""
    bad_sv = {
        "entity_type": "company",
        # policy_version intentionally missing
        "output_fields": [],
        "default_strategy": "first_non_null",
        "attribute_rules": [],
    }
    _put(s3_client, "entity-resolution/company/match_rules_v1.json", _COMPANY_MATCH_RULES)
    _put(s3_client, "entity-resolution/company/survivorship_v1.json", bad_sv)

    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    with pytest.raises(ResolutionConfigParseError, match="policy_version"):
        registry.load("company", match_rules_version="v1", survivorship_version="v1")


def test_parse_error_for_invalid_survivorship_strategy_enum(s3_client):
    """_parse_survivorship_policy raises ResolutionConfigParseError for invalid strategy."""
    bad_sv = {
        "entity_type": "company",
        "policy_version": "v1",
        "output_fields": ["full_name"],
        "default_strategy": "not_a_real_strategy",  # invalid enum value
        "attribute_rules": [],
    }
    _put(s3_client, "entity-resolution/company/match_rules_v1.json", _COMPANY_MATCH_RULES)
    _put(s3_client, "entity-resolution/company/survivorship_v1.json", bad_sv)

    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    with pytest.raises(ResolutionConfigParseError, match="Invalid enum"):
        registry.load("company", match_rules_version="v1", survivorship_version="v1")


def test_parse_error_for_deterministic_rule_with_no_fields(s3_client):
    """Deterministic rule with empty fields list raises ResolutionConfigParseError."""
    bad_rules = {
        "entity_type": "company",
        "rule_set_version": "v1",
        "rules": [
            {
                "rule_id": "empty-rule",
                "strategy": "deterministic",
                "fields": [],  # no fields
            }
        ],
    }
    _put(s3_client, "entity-resolution/company/match_rules_v1.json", bad_rules)
    _put(s3_client, "entity-resolution/company/survivorship_v1.json", _COMPANY_SURVIVORSHIP)

    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    with pytest.raises(ResolutionConfigParseError, match="at least one field"):
        registry.load("company", match_rules_version="v1", survivorship_version="v1")


def test_parse_error_for_probabilistic_rule_with_no_fields(s3_client):
    """Probabilistic rule with empty fields list raises ResolutionConfigParseError."""
    bad_rules = {
        "entity_type": "company",
        "rule_set_version": "v1",
        "rules": [
            {
                "rule_id": "empty-prob",
                "strategy": "probabilistic",
                "match_threshold": 0.8,
                "fields": [],
            }
        ],
    }
    _put(s3_client, "entity-resolution/company/match_rules_v1.json", bad_rules)
    _put(s3_client, "entity-resolution/company/survivorship_v1.json", _COMPANY_SURVIVORSHIP)

    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    with pytest.raises(ResolutionConfigParseError, match="at least one field"):
        registry.load("company", match_rules_version="v1", survivorship_version="v1")


def test_load_json_parse_error_on_invalid_bytes(s3_client):
    """_load_json raises ResolutionConfigParseError when S3 object is not valid JSON."""
    s3_client.put_object(
        Bucket=_BUCKET,
        Key="entity-resolution/company/match_rules_v1.json",
        Body=b"{{not valid json",
        ContentType="application/json",
    )
    _put(s3_client, "entity-resolution/company/survivorship_v1.json", _COMPANY_SURVIVORSHIP)

    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    with pytest.raises(ResolutionConfigParseError):
        registry.load("company", match_rules_version="v1", survivorship_version="v1")


def test_partial_latest_version_only_survivorship_is_latest(s3_client):
    """
    Branch 126->129: match_rules_version is explicit but survivorship_version is 'latest'.
    Only the survivorship version should be resolved from the pointer.
    """
    _seed_company(s3_client)
    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    # Explicit match_rules_version, 'latest' survivorship_version (default)
    config = registry.load("company", match_rules_version="v1")
    assert config.match_rule_set.rule_set_version == "v1"
    assert config.survivorship_policy.policy_version == "v1"


def test_partial_latest_version_only_match_rules_is_latest(s3_client):
    """
    Branch 126->129 False path: survivorship_version is explicit, match_rules is 'latest'.
    The `if survivorship_version == 'latest':` evaluates to False → skips line 127.
    """
    _seed_company(s3_client)
    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    # 'latest' match_rules_version (default), explicit survivorship_version
    config = registry.load("company", survivorship_version="v1")
    assert config.match_rule_set.rule_set_version == "v1"
    assert config.survivorship_policy.policy_version == "v1"


def test_survivorship_with_no_output_fields_skips_validation(s3_client):
    """
    Branch 346->354: if output_fields is empty, validation block is skipped.
    Survivorship with no output_fields (pass-through) should load without error.
    """
    passthrough_sv = {
        "entity_type": "company",
        "policy_version": "v1",
        # No output_fields key → defaults to [] → validation block skipped
        "default_strategy": "first_non_null",
        "attribute_rules": [],
    }
    _put(s3_client, "entity-resolution/company/match_rules_v1.json", _COMPANY_MATCH_RULES)
    _put(s3_client, "entity-resolution/company/survivorship_v1.json", passthrough_sv)

    registry = ResolutionConfigRegistry(s3_bucket=_BUCKET, region_name=_REGION)
    config = registry.load("company", match_rules_version="v1", survivorship_version="v1")
    assert config.survivorship_policy.output_fields == ()  # empty → pass-through
