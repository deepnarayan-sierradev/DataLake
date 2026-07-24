"""
Shared publishing helpers for the entity-resolution golden/canonical publishers.

Consolidates logic shared by the canonical record publisher (finding DUP-3):

  - :func:`flatten_list_fields`      — Parquet-compatibility flattening of list
                                        columns to JSON strings.
  - :func:`serialise_decisions`      — match-decision audit-trail JSON (no PII).
  - :func:`emit_golden_record_lineage` — best-effort ENTITY_RESOLUTION lineage.

Golden records are written to S3 via the shared, multipart-capable
:class:`observability.s3_writer.S3ParquetWriter` (finding PERF-4) rather than a
local in-memory ``_to_parquet()`` + ``put_object`` — matching the calling
convention already established by ``transformation.curated_layer_writer``.

Security (OWASP A09):
  - :func:`serialise_decisions` emits match statistics only, never PII values.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from typing import Any

from entity_resolution.matching_engine.match_rule_engine import MatchDecision
from governance.lineage_record import LineageEmitter, build_entity_resolution_lineage
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)


def flatten_list_fields(records: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """
    Flatten list-valued fields to JSON strings for Parquet compatibility.

    Golden records carry list columns (e.g. ``contributing_source_records``).
    PyArrow can represent these as nested list columns, but the platform stores
    them as JSON strings so they are directly queryable in Athena via
    ``json_extract_scalar(...)``. Non-list values are passed through unchanged.

    Yields flattened records lazily so the caller can stream straight into
    :class:`S3ParquetWriter` without materialising a second full copy in RAM.
    """
    for record in records:
        yield {
            key: json.dumps(value) if isinstance(value, list) else value
            for key, value in record.items()
        }


def serialise_decisions(decisions: list[MatchDecision]) -> str:
    """Serialise match decisions to an audit-trail JSON string (no PII values)."""
    return json.dumps(
        [
            {
                "record_a_id": d.record_a_id,
                "record_b_id": d.record_b_id,
                "rule_id": d.rule_id,
                "strategy": d.strategy.value,
                "is_match": d.is_match,
                "confidence_score": d.confidence_score,
                "matched_fields": list(d.matched_fields),
                "rule_set_version": d.rule_set_version,
            }
            for d in decisions
        ],
        indent=2,
    )


def emit_golden_record_lineage(
    s3_governance_bucket: str,
    curated_s3_bucket: str,
    curated_s3_prefixes: tuple[str, ...],
    analytics_s3_bucket: str,
    analytics_s3_prefix: str,
    match_run_id: str,
    entity_type: str,
    golden_record_count: int,
    rule_set_version: str,
    survivorship_version: str,
    region_name: str,
) -> None:
    """Persist an ENTITY_RESOLUTION lineage record (spec §9.1). Best-effort."""
    try:
        record = build_entity_resolution_lineage(
            run_id=match_run_id,
            source_id=entity_type,
            entity_type=entity_type,
            curated_s3_bucket=curated_s3_bucket,
            curated_s3_prefixes=curated_s3_prefixes,
            analytics_s3_bucket=analytics_s3_bucket,
            analytics_s3_prefix=analytics_s3_prefix,
            record_count=golden_record_count,
            rule_set_version=rule_set_version,
            survivorship_version=survivorship_version,
        )
        LineageEmitter(
            governance_s3_bucket=s3_governance_bucket,
            region_name=region_name,
        ).emit(record)
    except Exception as exc:
        logging.getLogger(__name__).warning("golden_record_lineage_emission_failed error=%s", exc)
