"""
Metric lineage and generated calculation methodology (DL-SEM-10, DL-SEM-05).

Each metric records the physical columns, joins, and filters it derives from, queryable
through the API for the data dictionary and impact analysis. The methodology document is
generated from the model, not maintained separately — one artefact per model version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from semantic.semantic_model import Metric, SemanticEntity, SemanticModel


@dataclass(frozen=True)
class MetricLineage:
    """Everything one metric derives from."""

    metric_name: str
    entity_name: str
    entity_type: str
    aggregation: str
    physical_columns: tuple[str, ...]
    derived_from_metrics: tuple[str, ...] = ()
    joins: tuple[str, ...] = ()
    access_tag: str | None = None
    business_owner: str | None = None
    steward: str | None = None
    definition: str = ""
    unit: str = ""
    signed_by: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "metric": self.metric_name,
            "entity": self.entity_name,
            "entity_type": self.entity_type,
            "aggregation": self.aggregation,
            "physical_columns": list(self.physical_columns),
            "derived_from_metrics": list(self.derived_from_metrics),
            "joins": list(self.joins),
            "access_tag": self.access_tag,
            "business_owner": self.business_owner,
            "steward": self.steward,
            "definition": self.definition,
            "unit": self.unit,
            "signed_by": self.signed_by,
        }


def metric_lineage(model: SemanticModel, entity_name: str, metric_name: str) -> MetricLineage:
    """Resolve one metric's lineage, following derived metrics to their physical columns."""
    entity = model.entity(entity_name)
    metric = entity.metric(metric_name)
    columns, derived = _resolve_columns(entity, metric)
    return MetricLineage(
        metric_name=metric.name,
        entity_name=entity.name,
        entity_type=entity.entity_type,
        aggregation=metric.aggregation if not metric.is_derived else metric.kind.value,
        physical_columns=columns,
        derived_from_metrics=derived,
        joins=tuple(f"{entity.name}->{j.target_entity}" for j in entity.joins),
        access_tag=metric.access_tag,
        business_owner=metric.business_owner,
        steward=metric.steward,
        definition=metric.definition,
        unit=metric.unit,
        signed_by=metric.definition_signed_by,
    )


def _resolve_columns(
    entity: SemanticEntity, metric: Metric
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not metric.is_derived:
        return ((metric.column,), ())
    columns: list[str] = []
    derived: list[str] = []
    for component_name in (metric.numerator_metric, metric.denominator_metric):
        if not component_name:
            continue
        derived.append(component_name)
        component = entity.metric(component_name)
        component_columns, nested = _resolve_columns(entity, component)
        columns.extend(component_columns)
        derived.extend(nested)
    return tuple(dict.fromkeys(columns)), tuple(dict.fromkeys(derived))


def all_metric_lineage(model: SemanticModel) -> tuple[MetricLineage, ...]:
    """Lineage for every metric in the model — the impact-analysis input."""
    return tuple(
        metric_lineage(model, entity.name, metric.name)
        for entity in model.entities
        for metric in entity.metrics
    )


def metrics_touching_column(model: SemanticModel, column: str) -> tuple[str, ...]:
    """Which metrics a physical column change would affect."""
    return tuple(
        f"{lineage.entity_name}.{lineage.metric_name}"
        for lineage in all_metric_lineage(model)
        if column in lineage.physical_columns
    )


@dataclass
class MethodologyDocument:
    """The generated calculation-methodology artefact for one model version (DL-SEM-05)."""

    tenant_code: str
    model_version: str
    lineage: tuple[MetricLineage, ...] = field(default_factory=tuple)

    def render_markdown(self) -> str:
        lines = [
            f"# Calculation methodology — semantic model {self.model_version}",
            "",
            f"**Tenant:** {self.tenant_code}",
            "",
            "Generated from the published semantic model. Every figure in a dashboard, report, "
            "or export resolves to one of the definitions below.",
            "",
        ]
        by_entity: dict[str, list[MetricLineage]] = {}
        for item in self.lineage:
            by_entity.setdefault(item.entity_name, []).append(item)
        for entity_name in sorted(by_entity):
            lines.extend([f"## {entity_name}", ""])
            for item in sorted(by_entity[entity_name], key=lambda m: m.metric_name):
                lines.extend(
                    [
                        f"### {item.metric_name}",
                        "",
                        f"- **Definition:** {item.definition or '_not yet signed_'}",
                        f"- **Calculation:** {item.aggregation} over "
                        f"{', '.join(f'`{c}`' for c in item.physical_columns)}",
                        f"- **Unit:** {item.unit or '—'}",
                        f"- **Business owner:** {item.business_owner or '_unowned_'}",
                        f"- **Steward:** {item.steward or '—'}",
                        f"- **Signed by:** {item.signed_by or '_unsigned_'}",
                        f"- **Access tag:** {item.access_tag or 'none'}",
                    ]
                )
                if item.derived_from_metrics:
                    lines.append(
                        "- **Derived from:** "
                        + ", ".join(f"`{m}`" for m in item.derived_from_metrics)
                    )
                lines.append("")
        return "\n".join(lines)


def build_methodology_document(model: SemanticModel) -> MethodologyDocument:
    """Generate the methodology artefact from the model."""
    return MethodologyDocument(
        tenant_code=model.tenant_code,
        model_version=model.model_version,
        lineage=all_metric_lineage(model),
    )


def methodology_s3_key(tenant_code: str, model_version: str) -> str:
    """Tenant-prefixed key; the same artefact the transition package ships (DL-PORT-02)."""
    return f"{tenant_code}/semantic-methodology/{model_version}.md"
