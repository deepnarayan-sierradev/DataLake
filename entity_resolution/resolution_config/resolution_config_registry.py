"""
Resolution config registry.

Loads two versioned JSON config files per entity type from S3 and constructs
the runtime objects consumed by GoldenRecordPublisher:

  match_rules_v1.json    → MatchRuleSet   (who is the same entity?)
  survivorship_v1.json   → SurvivorshipPolicy  (what does the output look like?)

Config S3 paths:
  s3://{bucket}/entity-resolution/{entity_type}/match_rules_{version}.json
  s3://{bucket}/entity-resolution/{entity_type}/survivorship_{version}.json

A "latest" pointer file per entity type:
  s3://{bucket}/entity-resolution/{entity_type}/latest.json
  → { "match_rules_version": "v1", "survivorship_version": "v1" }

This registry is the single source of truth for entity resolution configuration.
Adding or changing entity resolution behaviour requires only a new JSON file —
no code changes.

Security (OWASP A03, A05):
  - Entity type and version strings validated against safe-identifier regex.
  - No eval() or dynamic code execution.
  - All S3 keys constructed from validated components, never from raw input.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Final

import boto3

from entity_resolution.matching_engine.match_rule_engine import (
    DeterministicMatchField,
    DeterministicMatchRule,
    MatchRule,
    MatchRuleSet,
    ProbabilisticMatchField,
    ProbabilisticMatchRule,
)
from entity_resolution.matching_engine.record_blocker import (
    BlockingKeyType,
    BlockingStrategy,
)
from entity_resolution.survivorship_policy import (
    AttributeSurvivorshipRule,
    SurvivorshipPolicy,
    SurvivorshipStrategy,
)
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_SAFE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9\-]{0,63}$")
_SAFE_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^v[0-9]{1,4}$")


@dataclass(frozen=True)
class ResolutionConfig:
    """Pair of runtime objects for one entity type."""

    entity_type: str
    match_rule_set: MatchRuleSet
    survivorship_policy: SurvivorshipPolicy


class ResolutionConfigNotFoundError(Exception):
    """Raised when no config exists for the requested entity type / version."""


class ResolutionConfigParseError(Exception):
    """Raised when a config JSON file is malformed or contains invalid values."""


class ResolutionConfigRegistry:
    """
    S3-backed registry that loads and caches entity resolution configs.

    One instance is created per Lambda warm invocation and shared across
    calls.  Configs are cached in-process after the first load — they change
    only on deliberate version bumps, not on every pipeline run.

    Usage::

        registry = ResolutionConfigRegistry(s3_bucket="dev-edl-curated", region_name="us-east-1")
        config = registry.load("company")           # loads latest version
        config = registry.load("company", "v2")     # loads explicit version
    """

    def __init__(self, s3_bucket: str, region_name: str) -> None:
        self._bucket = s3_bucket
        self._region_name = region_name
        self._s3: Any = boto3.client("s3", region_name=region_name)
        self._cache: dict[str, ResolutionConfig] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(
        self,
        entity_type: str,
        match_rules_version: str = "latest",
        survivorship_version: str = "latest",
    ) -> ResolutionConfig:
        """
        Load match rules + survivorship config for the given entity type.

        Versions default to "latest".  Pass explicit version strings (e.g.
        "v2") to pin to a specific config.

        Raises ResolutionConfigNotFoundError when the S3 object is absent.
        Raises ResolutionConfigParseError when the JSON is malformed.
        """
        _validate_entity_type(entity_type)

        # Resolve "latest" pointers
        if match_rules_version == "latest" or survivorship_version == "latest":
            pointer = self._load_latest_pointer(entity_type)
            if match_rules_version == "latest":
                match_rules_version = pointer.get("match_rules_version", "v1")
            if survivorship_version == "latest":
                survivorship_version = pointer.get("survivorship_version", "v1")

        cache_key = f"{entity_type}/{match_rules_version}/{survivorship_version}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        _validate_version(match_rules_version)
        _validate_version(survivorship_version)

        match_raw = self._load_json(
            f"entity-resolution/{entity_type}/match_rules_{match_rules_version}.json"
        )
        survivorship_raw = self._load_json(
            f"entity-resolution/{entity_type}/survivorship_{survivorship_version}.json"
        )

        rule_set = _parse_match_rule_set(match_raw)
        policy = _parse_survivorship_policy(survivorship_raw)

        config = ResolutionConfig(
            entity_type=entity_type,
            match_rule_set=rule_set,
            survivorship_policy=policy,
        )
        self._cache[cache_key] = config
        _logger.info(
            "resolution_config_loaded",
            entity_type=entity_type,
            match_rules_version=match_rules_version,
            survivorship_version=survivorship_version,
            output_fields=len(policy.output_fields),
            match_rules=len(rule_set.rules),
        )
        return config

    def publish(
        self,
        entity_type: str,
        match_rules_raw: dict[str, Any],
        survivorship_raw: dict[str, Any],
    ) -> tuple[str, str]:
        """
        Publish new config versions to S3 and update the latest pointer.

        Returns (match_rules_s3_key, survivorship_s3_key).
        Used by onboarding scripts and CI pipelines.
        """
        _validate_entity_type(entity_type)

        mr_version = match_rules_raw.get("rule_set_version", "v1")
        sv_version = survivorship_raw.get("policy_version", "v1")

        _validate_version(mr_version)
        _validate_version(sv_version)

        mr_key = f"entity-resolution/{entity_type}/match_rules_{mr_version}.json"
        sv_key = f"entity-resolution/{entity_type}/survivorship_{sv_version}.json"
        ptr_key = f"entity-resolution/{entity_type}/latest.json"

        for key, body in [
            (mr_key, match_rules_raw),
            (sv_key, survivorship_raw),
            (ptr_key, {"match_rules_version": mr_version, "survivorship_version": sv_version}),
        ]:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=json.dumps(body, indent=2).encode("utf-8"),
                ContentType="application/json",
            )

        # Invalidate cache for this entity type
        self._cache = {k: v for k, v in self._cache.items() if not k.startswith(entity_type)}

        _logger.info(
            "resolution_config_published",
            entity_type=entity_type,
            match_rules_version=mr_version,
            survivorship_version=sv_version,
        )
        return mr_key, sv_key

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_json(self, key: str) -> dict[str, Any]:
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=key)
            return json.loads(response["Body"].read().decode("utf-8"))  # type: ignore[no-any-return]
        except self._s3.exceptions.NoSuchKey as exc:
            raise ResolutionConfigNotFoundError(
                f"Resolution config not found at s3://{self._bucket}/{key}"
            ) from exc
        except (json.JSONDecodeError, KeyError) as exc:
            raise ResolutionConfigParseError(
                f"Failed to parse resolution config at {key!r}: {exc}"
            ) from exc

    def _load_latest_pointer(self, entity_type: str) -> dict[str, str]:
        key = f"entity-resolution/{entity_type}/latest.json"
        try:
            return self._load_json(key)
        except ResolutionConfigNotFoundError:
            # No pointer yet — default to v1
            return {"match_rules_version": "v1", "survivorship_version": "v1"}


# ---------------------------------------------------------------------------
# Parsers — JSON dict → typed dataclass objects
# ---------------------------------------------------------------------------


def _parse_match_rule_set(raw: dict[str, Any]) -> MatchRuleSet:
    try:
        entity_type = raw["entity_type"]
        rule_set_version = raw["rule_set_version"]
        rules: list[MatchRule] = []

        for r in raw.get("rules", []):
            strategy = r.get("strategy", "deterministic")
            if strategy == "deterministic":
                rules.append(_parse_deterministic_rule(r))
            elif strategy == "probabilistic":
                rules.append(_parse_probabilistic_rule(r))
            else:
                raise ResolutionConfigParseError(
                    f"Unknown match strategy {strategy!r} in rule {r.get('rule_id')!r}"
                )

        blocking_strategy: BlockingStrategy | None = None
        if "blocking" in raw:
            blocking_strategy = _parse_blocking_strategy(raw["blocking"])

        return MatchRuleSet(
            entity_type=entity_type,
            rule_set_version=rule_set_version,
            rules=tuple(rules),
            blocking_strategy=blocking_strategy,
        )
    except KeyError as exc:
        raise ResolutionConfigParseError(f"Missing required field in match_rules config: {exc}") from exc


def _parse_deterministic_rule(r: dict[str, Any]) -> DeterministicMatchRule:
    fields = tuple(
        DeterministicMatchField(
            field_name=f["field_name"],
            normalise=f.get("normalise", True),
        )
        for f in r.get("fields", [])
    )
    if not fields:
        raise ResolutionConfigParseError(
            f"Deterministic rule {r.get('rule_id')!r} must have at least one field"
        )
    return DeterministicMatchRule(rule_id=r["rule_id"], fields=fields)


def _parse_probabilistic_rule(r: dict[str, Any]) -> ProbabilisticMatchRule:
    fields = tuple(
        ProbabilisticMatchField(
            field_name=f["field_name"],
            weight=float(f["weight"]),
            similarity_kind=f.get("similarity_kind", "exact"),
        )
        for f in r.get("fields", [])
    )
    if not fields:
        raise ResolutionConfigParseError(
            f"Probabilistic rule {r.get('rule_id')!r} must have at least one field"
        )
    threshold = float(r.get("match_threshold", 0.8))
    if not 0.0 < threshold <= 1.0:
        raise ResolutionConfigParseError(
            f"match_threshold must be in (0.0, 1.0], got {threshold}"
        )
    return ProbabilisticMatchRule(
        rule_id=r["rule_id"],
        fields=fields,
        match_threshold=threshold,
    )


def _parse_blocking_strategy(b: dict[str, Any]) -> BlockingStrategy:
    try:
        key_type = BlockingKeyType(b["key_type"])
    except ValueError as exc:
        raise ResolutionConfigParseError(f"Unknown blocking key_type: {b.get('key_type')!r}") from exc
    return BlockingStrategy(
        key_type=key_type,
        source_field=b["source_field"],
        max_block_size=int(b.get("max_block_size", 1000)),
    )


def _parse_survivorship_policy(raw: dict[str, Any]) -> SurvivorshipPolicy:
    try:
        entity_type = raw["entity_type"]
        policy_version = raw["policy_version"]
        output_fields = tuple(raw.get("output_fields", []))

        default_strategy = SurvivorshipStrategy(
            raw.get("default_strategy", SurvivorshipStrategy.FIRST_NON_NULL)
        )

        attribute_rules: list[AttributeSurvivorshipRule] = []
        for rule in raw.get("attribute_rules", []):
            strategy = SurvivorshipStrategy(rule["strategy"])
            attribute_rules.append(
                AttributeSurvivorshipRule(
                    canonical_field=rule["canonical_field"],
                    strategy=strategy,
                    source_priority=tuple(rule.get("source_priority", [])),
                    timestamp_field=rule.get("timestamp_field"),
                )
            )

        # Validate: every attribute_rule's canonical_field must be in output_fields
        if output_fields:
            for rule in attribute_rules:
                if rule.canonical_field not in output_fields:
                    raise ResolutionConfigParseError(
                        f"attribute_rule field {rule.canonical_field!r} is not in output_fields. "
                        "Add it to output_fields or remove the attribute_rule."
                    )

        return SurvivorshipPolicy(
            entity_type=entity_type,
            policy_version=policy_version,
            attribute_rules=tuple(attribute_rules),
            default_strategy=default_strategy,
            output_fields=output_fields,
        )
    except KeyError as exc:
        raise ResolutionConfigParseError(
            f"Missing required field in survivorship config: {exc}"
        ) from exc
    except ValueError as exc:
        raise ResolutionConfigParseError(f"Invalid enum value in survivorship config: {exc}") from exc


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_entity_type(entity_type: str) -> None:
    if not _SAFE_ID_PATTERN.match(entity_type):
        raise ValueError(
            f"entity_type {entity_type!r} must match '^[a-z][a-z0-9\\-]{{0,63}}$'"
        )


def _validate_version(version: str) -> None:
    if not _SAFE_VERSION_PATTERN.match(version):
        raise ValueError(
            f"version {version!r} must match '^v[0-9]{{1,4}}$' (e.g. 'v1', 'v12')"
        )
