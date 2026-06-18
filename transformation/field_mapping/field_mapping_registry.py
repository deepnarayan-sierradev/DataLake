"""
Cross-system field mapping registry.

Declarative mapping rules define how source fields are mapped to canonical
domain model fields.  The registry is loaded from S3-backed configuration
(JSON) and is versioned for backward-compatible evolution.

Each rule specifies:
  source_fields  — one or more source field names (tuple for concat/composite)
  canonical_field — target field name in the canonical domain model
  transformation  — rename | concat | date_format | cast | mask
  transformation_params — transformation-specific parameters
  missing_field_behavior — drop_field | raise_error | use_default

Security (OWASP A03, A05):
  - Rule source is S3-backed config, not runtime user input.
  - Field names validated against safe-identifier regex before use.
  - No eval() or dynamic code execution.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

import boto3

from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_FIELD_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]{0,127}$")
_CANONICAL_FIELD_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


class MappingTransformation(StrEnum):
    RENAME = "rename"
    CONCAT = "concat"
    DATE_FORMAT = "date_format"
    CAST = "cast"
    MASK = "mask"


class MissingFieldBehavior(StrEnum):
    DROP_FIELD = "drop_field"
    RAISE_ERROR = "raise_error"
    USE_DEFAULT = "use_default"


@dataclass(frozen=True)
class _DroppedField:
    """Sentinel: rule produced no value; skip this field in output."""


@dataclass(frozen=True)
class _MappingFailure:
    """Sentinel: hard mapping failure; discard the entire record."""


_DROPPED_FIELD: Final[_DroppedField] = _DroppedField()
_MAPPING_FAILURE: Final[_MappingFailure] = _MappingFailure()


@dataclass(frozen=True)
class FieldMappingRule:
    """
    Single mapping rule from source field(s) to one canonical field.

    source_fields supports multiple fields for concat/composite transformations.
    """

    source_fields: tuple[str, ...]
    canonical_field: str
    transformation: MappingTransformation
    transformation_params: dict[str, str]
    missing_field_behavior: MissingFieldBehavior = MissingFieldBehavior.DROP_FIELD
    default_value: str | None = None

    def __post_init__(self) -> None:
        if not self.source_fields:
            raise ValueError("source_fields must not be empty")
        for sf in self.source_fields:
            if not _FIELD_NAME_PATTERN.match(sf):
                raise ValueError(f"Invalid source field name: {sf!r}")
        if not _CANONICAL_FIELD_PATTERN.match(self.canonical_field):
            raise ValueError(f"Invalid canonical field name: {self.canonical_field!r}")


@dataclass(frozen=True)
class FieldMappingRuleSet:
    """Versioned set of mapping rules for a source+entity combination."""

    source_id: str
    entity_id: str
    mapping_version: str
    rules: tuple[FieldMappingRule, ...]


class FieldMappingApplicator:
    """
    Applies a FieldMappingRuleSet to a raw source record.

    Returns a canonical dict, or None when a RAISE_ERROR rule fires
    (the record must be discarded).
    """

    def apply(
        self,
        record: dict[str, Any],
        rule_set: FieldMappingRuleSet,
    ) -> dict[str, Any] | None:
        canonical: dict[str, Any] = {}

        for rule in rule_set.rules:
            outcome = self._apply_rule(record, rule)
            if isinstance(outcome, _MappingFailure):
                return None
            if not isinstance(outcome, _DroppedField):
                canonical[rule.canonical_field] = outcome

        return canonical

    def _apply_rule(  # noqa: C901
        self,
        record: dict[str, Any],
        rule: FieldMappingRule,
    ) -> _DroppedField | _MappingFailure | Any:
        missing = [f for f in rule.source_fields if f not in record or record[f] is None]

        if missing:
            if rule.missing_field_behavior == MissingFieldBehavior.RAISE_ERROR:
                _logger.error(
                    "mapping_required_field_missing",
                    fields=missing,
                    canonical=rule.canonical_field,
                )
                return _MAPPING_FAILURE
            if rule.missing_field_behavior == MissingFieldBehavior.USE_DEFAULT:
                return rule.default_value
            return _DROPPED_FIELD  # DROP_FIELD

        if rule.transformation == MappingTransformation.RENAME:
            return record[rule.source_fields[0]]

        if rule.transformation == MappingTransformation.CONCAT:
            sep = rule.transformation_params.get("separator", " ")
            return sep.join(str(record[f]) for f in rule.source_fields if record.get(f) is not None)

        if rule.transformation == MappingTransformation.DATE_FORMAT:
            raw_val = record[rule.source_fields[0]]
            in_fmt = rule.transformation_params.get("input_format", "%Y-%m-%dT%H:%M:%S.%fZ")
            out_fmt = rule.transformation_params.get("output_format", "%Y-%m-%d")
            if isinstance(raw_val, datetime):
                return raw_val.strftime(out_fmt)
            return datetime.strptime(str(raw_val), in_fmt).strftime(out_fmt)

        if rule.transformation == MappingTransformation.CAST:
            return _cast_value(
                record[rule.source_fields[0]], rule.transformation_params.get("type", "string")
            )

        if rule.transformation == MappingTransformation.MASK:
            raw_val = str(record[rule.source_fields[0]])
            visible = int(rule.transformation_params.get("visible_chars", "4"))
            if len(raw_val) <= visible:
                return "*" * len(raw_val)
            return "*" * (len(raw_val) - visible) + raw_val[-visible:]

        return record[rule.source_fields[0]]  # fallback: pass-through


def _cast_value(value: Any, target_type: str) -> Any:
    """Type-cast a value to the requested type. Raises ValueError on failure."""
    if target_type == "string":
        return str(value)
    if target_type == "integer":
        return int(value)
    if target_type in ("decimal", "float"):
        return float(value)
    if target_type == "boolean":
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("true", "1", "yes")
    return value  # unknown type: pass through


class FieldMappingRegistryClient:
    """
    S3-backed registry that loads and publishes FieldMappingRuleSet definitions.

    Rule sets are stored at:
      s3://{bucket}/field-mappings/{source_id}/{entity_id}/{mapping_version}.json

    A "latest" pointer file at:
      s3://{bucket}/field-mappings/{source_id}/{entity_id}/latest.json
    tracks the current active version.
    """

    def __init__(self, s3_bucket: str, region_name: str) -> None:
        self._s3_bucket = s3_bucket
        self._region_name = region_name
        self._s3: Any = boto3.client("s3", region_name=region_name)

    def load_rule_set(
        self,
        source_id: str,
        entity_id: str,
        mapping_version: str = "latest",
    ) -> FieldMappingRuleSet:
        """
        Load a versioned rule set from S3.

        Raises MappingRuleSetNotFoundError if the object does not exist.
        Raises MappingRuleSetParseError if the JSON is malformed.
        """
        if mapping_version == "latest":
            mapping_version = self._resolve_latest_version(source_id, entity_id)

        key = f"field-mappings/{source_id}/{entity_id}/{mapping_version}.json"

        try:
            response = self._s3.get_object(Bucket=self._s3_bucket, Key=key)
            raw: dict[str, Any] = json.loads(response["Body"].read().decode("utf-8"))
        except self._s3.exceptions.NoSuchKey as exc:
            raise MappingRuleSetNotFoundError(source_id, entity_id, mapping_version) from exc
        except (json.JSONDecodeError, KeyError) as exc:
            raise MappingRuleSetParseError(f"Failed to parse mapping rule set: {exc}") from exc

        return _deserialise_rule_set(raw)

    def _resolve_latest_version(self, source_id: str, entity_id: str) -> str:
        pointer_key = f"field-mappings/{source_id}/{entity_id}/latest.json"
        try:
            response = self._s3.get_object(Bucket=self._s3_bucket, Key=pointer_key)
            pointer: dict[str, str] = json.loads(response["Body"].read().decode("utf-8"))
            return pointer["mapping_version"]
        except Exception as exc:
            raise MappingRuleSetNotFoundError(source_id, entity_id, "latest") from exc

    def publish_rule_set(self, rule_set: FieldMappingRuleSet) -> str:
        """
        Persist a rule set to S3 and update the latest pointer.
        Returns the S3 key of the published rule set.
        """
        key = (
            f"field-mappings/{rule_set.source_id}/{rule_set.entity_id}"
            f"/{rule_set.mapping_version}.json"
        )
        body = json.dumps(_serialise_rule_set(rule_set), indent=2).encode("utf-8")

        self._s3.put_object(
            Bucket=self._s3_bucket, Key=key, Body=body, ContentType="application/json"
        )

        pointer_key = f"field-mappings/{rule_set.source_id}/{rule_set.entity_id}/latest.json"
        self._s3.put_object(
            Bucket=self._s3_bucket,
            Key=pointer_key,
            Body=json.dumps({"mapping_version": rule_set.mapping_version}).encode("utf-8"),
            ContentType="application/json",
        )

        _logger.info(
            "field_mapping_rule_set_published",
            source_id=rule_set.source_id,
            entity_id=rule_set.entity_id,
            mapping_version=rule_set.mapping_version,
            rule_count=len(rule_set.rules),
        )
        return key


def _serialise_rule_set(rule_set: FieldMappingRuleSet) -> dict[str, Any]:
    return {
        "source_id": rule_set.source_id,
        "entity_id": rule_set.entity_id,
        "mapping_version": rule_set.mapping_version,
        "rules": [
            {
                "source_fields": list(r.source_fields),
                "canonical_field": r.canonical_field,
                "transformation": r.transformation.value,
                "transformation_params": r.transformation_params,
                "missing_field_behavior": r.missing_field_behavior.value,
                "default_value": r.default_value,
            }
            for r in rule_set.rules
        ],
    }


def _deserialise_rule_set(raw: dict[str, Any]) -> FieldMappingRuleSet:
    rules = tuple(
        FieldMappingRule(
            source_fields=tuple(r["source_fields"]),
            canonical_field=r["canonical_field"],
            transformation=MappingTransformation(r["transformation"]),
            transformation_params=r.get("transformation_params", {}),
            missing_field_behavior=MissingFieldBehavior(
                r.get("missing_field_behavior", MissingFieldBehavior.DROP_FIELD.value)
            ),
            default_value=r.get("default_value"),
        )
        for r in raw["rules"]
    )
    return FieldMappingRuleSet(
        source_id=raw["source_id"],
        entity_id=raw["entity_id"],
        mapping_version=raw["mapping_version"],
        rules=rules,
    )


class MappingRuleSetNotFoundError(Exception):
    def __init__(self, source_id: str, entity_id: str, version: str) -> None:
        super().__init__(f"No mapping rule set found for {source_id}/{entity_id}@{version}")


class MappingRuleSetParseError(Exception):
    """Raised when a mapping rule set JSON document is structurally invalid."""
