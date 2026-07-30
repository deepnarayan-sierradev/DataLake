"""
Relationship-rules registry (FR-1.2).

S3-backed, per-tenant store of versioned RelationshipRuleSet configs, keyed by
the primary entity type whose twin they describe — mirroring the
entity-resolution config registry. A "latest" pointer names the active version.
Rules are authored as JSON and validated by the RelationshipRuleSet model on
load (OWASP A03 — every field name is allowlisted before it reaches a query).
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

import boto3

from contracts.identifier_policy import ENTITY_TYPE_PATTERN, validate_tenant_code
from knowledge.relationship_rules import RelationshipRuleSet
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)
_SAFE_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^v[0-9]{1,4}$")


class RelationshipRulesNotFoundError(Exception):
    """Raised when no relationship-rules config exists for the tenant/entity type."""


class RelationshipRulesRegistry:
    def __init__(self, s3_bucket: str, region_name: str) -> None:
        self._bucket = s3_bucket
        self._s3: Any = boto3.client("s3", region_name=region_name)

    def load(
        self, tenant_code: str, entity_type: str, version: str = "latest"
    ) -> RelationshipRuleSet:
        tenant_code = validate_tenant_code(tenant_code)
        _validate_entity_type(entity_type)
        if version == "latest":
            version = self._latest_version(tenant_code, entity_type)
        _validate_version(version)
        raw = self._load_json(self._rules_key(tenant_code, entity_type, version))
        return RelationshipRuleSet.model_validate(raw)

    def publish(self, entity_type: str, rule_set: RelationshipRuleSet) -> str:
        tenant_code = validate_tenant_code(rule_set.tenant_code)
        _validate_entity_type(entity_type)
        _validate_version(rule_set.rule_set_version)
        rules_key = self._rules_key(tenant_code, entity_type, rule_set.rule_set_version)
        self._s3.put_object(
            Bucket=self._bucket,
            Key=rules_key,
            Body=rule_set.model_dump_json(indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        self._s3.put_object(
            Bucket=self._bucket,
            Key=self._pointer_key(tenant_code, entity_type),
            Body=json.dumps({"rule_set_version": rule_set.rule_set_version}).encode("utf-8"),
            ContentType="application/json",
        )
        _logger.info(
            "relationship_rules_published",
            tenant_code=tenant_code,
            entity_type=entity_type,
            version=rule_set.rule_set_version,
        )
        return rules_key

    def _latest_version(self, tenant_code: str, entity_type: str) -> str:
        try:
            pointer = self._load_json(self._pointer_key(tenant_code, entity_type))
        except RelationshipRulesNotFoundError:
            return "v1"
        return str(pointer.get("rule_set_version", "v1"))

    @staticmethod
    def _rules_key(tenant_code: str, entity_type: str, version: str) -> str:
        return f"{tenant_code}/relationship-rules/{entity_type}/{version}.json"

    @staticmethod
    def _pointer_key(tenant_code: str, entity_type: str) -> str:
        return f"{tenant_code}/relationship-rules/{entity_type}/latest.json"

    def _load_json(self, key: str) -> dict[str, Any]:
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=key)
            return json.loads(response["Body"].read().decode("utf-8"))  # type: ignore[no-any-return]
        except self._s3.exceptions.NoSuchKey as exc:
            raise RelationshipRulesNotFoundError(
                f"No relationship rules at s3://{self._bucket}/{key}"
            ) from exc


def _validate_entity_type(entity_type: str) -> None:
    if not ENTITY_TYPE_PATTERN.match(entity_type):
        raise ValueError(f"entity_type {entity_type!r} is not a valid entity type.")


def _validate_version(version: str) -> None:
    if not _SAFE_VERSION_PATTERN.match(version):
        raise ValueError(f"version {version!r} must match 'v<N>' (e.g. 'v1').")
