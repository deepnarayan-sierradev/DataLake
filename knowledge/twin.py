"""
Digital twin domain model (FR-1.3).

A twin is a connected view of one real-world entity: its mastered attributes
(from the golden record), its outgoing relationship edges (adjacency), an
optional lifecycle stage, and derived rollups (e.g. per-relationship degree).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TwinEdge:
    relationship_type: str
    to_entity_type: str
    to_golden_id: str
    scope_unit_id: str | None


@dataclass(frozen=True)
class Twin:
    entity_type: str
    golden_id: str
    attributes: dict[str, Any]
    edges: tuple[TwinEdge, ...]
    lifecycle_stage: str | None
    rollups: dict[str, int]
    scope_unit_id: str | None
