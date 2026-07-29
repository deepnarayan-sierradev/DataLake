"""
Generated transformation documentation and survivorship explainability
(DL-DQ-06, DL-DQ-07, DL-DQ-08).

The data dictionary is *generated* from the field-mapping and survivorship configuration
already in S3, not maintained separately — a hand-written dictionary drifts from the config
that actually runs, which is the failure this requirement exists to prevent.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from contracts.identifier_policy import validate_tenant_code
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)


def data_dictionary_s3_key(tenant_code: str, entity_id: str, version: str) -> str:
    """`{tenant_code}/data-dictionary/{entity_id}/{version}.md` — tenant-prefixed."""
    validate_tenant_code(tenant_code)
    return f"{tenant_code}/data-dictionary/{entity_id}/{version}.md"


@dataclass(frozen=True)
class MappedFieldDocumentation:
    """One canonical field, its source, and the rule that produced it."""

    canonical_field: str
    source_field: str
    data_type: str
    transformation: str = "direct"
    classification: str = "internal"
    description: str = ""


@dataclass(frozen=True)
class SurvivorshipRuleDocumentation:
    """One golden field's survivorship rule, for the explainability section."""

    canonical_field: str
    strategy: str
    source_priority: tuple[str, ...] = ()
    timestamp_field: str | None = None


@dataclass
class DataDictionary:
    """The generated artefact for one entity at one config version."""

    tenant_code: str
    entity_id: str
    entity_type: str
    version: str
    mapping_version: str
    fields: tuple[MappedFieldDocumentation, ...]
    survivorship_rules: tuple[SurvivorshipRuleDocumentation, ...] = ()
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        validate_tenant_code(self.tenant_code)

    def render_markdown(self) -> str:
        """Human-readable dictionary; also the transition-package artefact (DL-PORT-02)."""
        lines = [
            f"# Data dictionary — {self.entity_id}",
            "",
            f"**Entity type:** {self.entity_type}  ",
            f"**Dictionary version:** {self.version}  ",
            f"**Field-mapping version:** {self.mapping_version}  ",
            f"**Generated:** {self.generated_at}",
            "",
            "Generated from the field-mapping and survivorship configuration. Do not edit by "
            "hand — regenerate from the config instead.",
            "",
            "## Fields",
            "",
            "| Canonical field | Source field | Type | Transformation | Classification "
            "| Description |",
            "|---|---|---|---|---|---|",
        ]
        for documented in self.fields:
            lines.append(
                f"| `{documented.canonical_field}` | `{documented.source_field}` | "
                f"{documented.data_type} | {documented.transformation} | "
                f"{documented.classification} | {documented.description} |"
            )
        if self.survivorship_rules:
            lines.extend(
                [
                    "",
                    "## Conflict resolution (survivorship)",
                    "",
                    "| Canonical field | Strategy | Source priority | Timestamp field |",
                    "|---|---|---|---|",
                ]
            )
            for rule in self.survivorship_rules:
                priority = ", ".join(rule.source_priority) or "—"
                lines.append(
                    f"| `{rule.canonical_field}` | {rule.strategy} | {priority} | "
                    f"{rule.timestamp_field or '—'} |"
                )
        lines.append("")
        return "\n".join(lines)

    def content_hash(self) -> str:
        """Hash of the rendered artefact, so a tampered copy is detectable (OWASP A08)."""
        return hashlib.sha256(self.render_markdown().encode("utf-8")).hexdigest()


def build_data_dictionary(
    tenant_code: str,
    entity_id: str,
    entity_type: str,
    mapping_config: dict[str, Any],
    survivorship_config: dict[str, Any] | None = None,
    classification_by_field: dict[str, str] | None = None,
) -> DataDictionary:
    """Render the dictionary from the stored mapping and survivorship configuration."""
    classifications = classification_by_field or {}
    fields = tuple(
        MappedFieldDocumentation(
            canonical_field=str(rule.get("canonical_field", "")),
            source_field=str(rule.get("source_field", "")),
            data_type=str(rule.get("target_type", "string")),
            transformation=str(rule.get("transformation", "direct")),
            classification=classifications.get(str(rule.get("canonical_field", "")), "internal"),
            description=str(rule.get("description", "")),
        )
        for rule in mapping_config.get("rules", [])
    )
    survivorship_rules: tuple[SurvivorshipRuleDocumentation, ...] = ()
    if survivorship_config:
        survivorship_rules = tuple(
            SurvivorshipRuleDocumentation(
                canonical_field=str(rule.get("canonical_field", "")),
                strategy=str(rule.get("strategy", "")),
                source_priority=tuple(rule.get("source_priority", [])),
                timestamp_field=rule.get("timestamp_field"),
            )
            for rule in survivorship_config.get("attribute_rules", [])
        )
    return DataDictionary(
        tenant_code=tenant_code,
        entity_id=entity_id,
        entity_type=entity_type,
        version=str(mapping_config.get("rule_set_version", "v1")),
        mapping_version=str(mapping_config.get("rule_set_version", "v1")),
        fields=fields,
        survivorship_rules=survivorship_rules,
    )


def publish_data_dictionary(s3_client: Any, bucket: str, dictionary: DataDictionary) -> str:
    """Write the dictionary to S3 and return its key."""
    key = data_dictionary_s3_key(dictionary.tenant_code, dictionary.entity_id, dictionary.version)
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=dictionary.render_markdown().encode("utf-8"),
        ContentType="text/markdown",
    )
    _logger.info(
        "data_dictionary_published",
        tenant_code=dictionary.tenant_code,
        entity_id=dictionary.entity_id,
        version=dictionary.version,
        s3_key=key,
    )
    return key


# ---------------------------------------------------------------------------
# Survivorship explainability (DL-DQ-07) and golden-id history (DL-DQ-08)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldProvenanceEntry:
    """Which source won a golden field, and under which rule."""

    canonical_field: str
    winning_source_id: str
    rule_id: str
    strategy: str
    contributing_source_ids: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "source_id": self.winning_source_id,
            "rule_id": self.rule_id,
            "strategy": self.strategy,
            "contributing_source_ids": list(self.contributing_source_ids),
        }


def build_field_provenance(
    entries: tuple[FieldProvenanceEntry, ...],
) -> str:
    """
    Serialise per-field provenance carrying the rule id, not only the source (DL-DQ-07).

    Extends the existing `field_provenance` payload rather than adding a parallel structure,
    so the Athena consumers that already read it keep working.
    """
    return json.dumps(
        {entry.canonical_field: entry.to_json() for entry in entries},
        sort_keys=True,
        separators=(",", ":"),
    )


class GoldenIdAssignment(dict[str, Any]):
    """One golden-id assignment event; a mapping so it persists without translation."""


def build_golden_id_assignment(
    tenant_code: str,
    entity_type: str,
    golden_id: str,
    contributing_record_ids: tuple[str, ...],
    match_run_id: str,
    operation: str = "assign",
    previous_golden_ids: tuple[str, ...] = (),
) -> GoldenIdAssignment:
    """
    Record a golden-id assignment, merge, or split so it can be traced and reversed.

    `previous_golden_ids` is what makes a merge reversible: without it, two records that were
    merged cannot be told apart from two that were always one (DL-DQ-08).
    """
    validate_tenant_code(tenant_code)
    if operation not in ("assign", "merge", "split"):
        raise ValueError(f"operation {operation!r} must be one of assign, merge, split.")
    if operation in ("merge", "split") and not previous_golden_ids:
        raise ValueError(
            f"A {operation!r} must name the previous golden ids, or the change cannot be "
            "traced or reversed (DL-DQ-08)."
        )
    return GoldenIdAssignment(
        {
            "tenant_code": tenant_code,
            "entity_type": entity_type,
            "golden_id": golden_id,
            "operation": operation,
            "contributing_record_ids": list(contributing_record_ids),
            "previous_golden_ids": list(previous_golden_ids),
            "match_run_id": match_run_id,
            "assigned_at": datetime.now(UTC).isoformat(),
        }
    )
