"""
Entity match rule engine.

Supports two matching strategies:
  DETERMINISTIC — exact match on one or more key fields (email, domain, ID)
  PROBABILISTIC — weighted similarity scoring across multiple fields

Match rule sets are versioned and externally configurable (no hardcoded
thresholds).  Every match decision produces an explainability record traceable
to the rule version and source record pair.

Security (OWASP A03, A09):
  - Match key values never appear in log output (PII protection).
  - Rule sets loaded from config, not constructed from request parameters.
  - Normalisation functions are deterministic and side-effect free.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from entity_resolution.matching_engine.record_blocker import BlockingStrategy, RecordBlocker
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)


# ---------------------------------------------------------------------------
# Rule definitions
# ---------------------------------------------------------------------------


class MatchStrategy(StrEnum):
    DETERMINISTIC = "deterministic"
    PROBABILISTIC = "probabilistic"


@dataclass(frozen=True)
class DeterministicMatchField:
    """Single field participating in a deterministic exact-match key."""

    field_name: str
    normalise: bool = True  # strip, lower, unicode-normalise before comparison


@dataclass(frozen=True)
class DeterministicMatchRule:
    """
    Exact match on one or more fields.  All fields must match simultaneously
    (logical AND).
    """

    rule_id: str
    fields: tuple[DeterministicMatchField, ...]
    strategy: MatchStrategy = MatchStrategy.DETERMINISTIC


@dataclass(frozen=True)
class ProbabilisticMatchField:
    """Field participating in a probabilistic score."""

    field_name: str
    weight: float  # contribution to total score; weights across all fields sum to 1.0
    similarity_kind: str  # "exact", "jaro_winkler", "token_set"


@dataclass(frozen=True)
class ProbabilisticMatchRule:
    """
    Score-based matching.  The overall match score is the weighted sum of
    per-field similarity scores.  Records with score >= match_threshold are
    considered a match.
    """

    rule_id: str
    fields: tuple[ProbabilisticMatchField, ...]
    match_threshold: float  # [0.0, 1.0]
    strategy: MatchStrategy = MatchStrategy.PROBABILISTIC


MatchRule = DeterministicMatchRule | ProbabilisticMatchRule


@dataclass(frozen=True)
class MatchRuleSet:
    """Versioned collection of match rules for an entity type."""

    entity_type: str
    rule_set_version: str
    rules: tuple[MatchRule, ...]
    blocking_strategy: BlockingStrategy | None = None
    # None means no blocking (correct for small datasets < ~5 k records).
    # Set a BlockingStrategy for datasets above ~5 k records to prevent O(n²)
    # pairwise comparisons that would exceed Lambda time/memory limits.


# ---------------------------------------------------------------------------
# Match decision (explainability record)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchDecision:
    """
    Explainability record for a single match decision.

    Does NOT include the actual field values — only the structural
    metadata needed to trace the decision back to rule and source records.
    """

    record_a_id: str
    record_b_id: str
    rule_id: str
    strategy: MatchStrategy
    is_match: bool
    confidence_score: float  # [0.0, 1.0]; 1.0 for deterministic matches
    matched_fields: tuple[str, ...]
    rule_set_version: str


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class MatchRuleEngine:
    """
    Applies a MatchRuleSet to pairs of candidate records.

    Usage pattern:
      engine = MatchRuleEngine(rule_set)
      clusters = engine.cluster(records, id_field="record_id")
      # clusters is list[frozenset[str]] — each set is a group of matching IDs
    """

    def __init__(self, rule_set: MatchRuleSet) -> None:
        self._rule_set = rule_set

    def compare(
        self,
        record_a: dict[str, Any],
        record_b: dict[str, Any],
        id_field: str,
    ) -> list[MatchDecision]:
        """
        Compare two records against all rules in the rule set.
        Returns one MatchDecision per rule.
        """
        decisions: list[MatchDecision] = []
        for rule in self._rule_set.rules:
            decision = self._apply_rule(
                rule,
                record_a,
                record_b,
                str(record_a.get(id_field, "?")),
                str(record_b.get(id_field, "?")),
            )
            decisions.append(decision)
        return decisions

    def cluster(
        self,
        records: list[dict[str, Any]],
        id_field: str,
    ) -> tuple[list[frozenset[str]], list[MatchDecision]]:
        """
        Group records into match clusters using union-find.

        When the rule set includes a blocking_strategy, records are first
        partitioned into blocks.  Only records within the same block are
        compared pairwise, reducing comparisons from O(n²) to O(b·k²)
        where b = number of blocks, k = average block size.

        Returns:
            clusters      — list of frozensets, each containing matching record IDs
            all_decisions — complete explainability audit trail
        """
        all_decisions: list[MatchDecision] = []
        parent: dict[str, str] = {str(r[id_field]): str(r[id_field]) for r in records}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]  # path compression
                x = parent[x]
            return x

        def union(x: str, y: str) -> None:
            parent[find(x)] = find(y)

        # Blocking: partition records before pairwise comparison.
        # Without a strategy, all records go into one block (brute-force).
        # With a strategy, only records sharing a blocking key are compared.
        if self._rule_set.blocking_strategy is not None:
            blocker = RecordBlocker(self._rule_set.blocking_strategy)
            blocks = blocker.partition(records)
        else:
            blocks = [records]

        for block in blocks:
            for rec_idx, rec_a in enumerate(block):
                for rec_b in block[rec_idx + 1 :]:
                    decisions = self.compare(rec_a, rec_b, id_field)
                    all_decisions.extend(decisions)
                    if any(d.is_match for d in decisions):
                        union(str(rec_a[id_field]), str(rec_b[id_field]))

        # Collect clusters from union-find structure
        cluster_map: dict[str, set[str]] = {}
        for r in records:
            rid = str(r[id_field])
            root = find(rid)
            cluster_map.setdefault(root, set()).add(rid)

        clusters = [frozenset(v) for v in cluster_map.values()]
        _logger.info(
            "match_clustering_complete",
            entity_type=self._rule_set.entity_type,
            input_records=len(records),
            cluster_count=len(clusters),
            decision_count=len(all_decisions),
            blocking_enabled=self._rule_set.blocking_strategy is not None,
        )
        return clusters, all_decisions

    def _apply_rule(
        self,
        rule: MatchRule,
        rec_a: dict[str, Any],
        rec_b: dict[str, Any],
        id_a: str,
        id_b: str,
    ) -> MatchDecision:
        if isinstance(rule, DeterministicMatchRule):
            return self._deterministic(rule, rec_a, rec_b, id_a, id_b)
        return self._probabilistic(rule, rec_a, rec_b, id_a, id_b)

    def _deterministic(
        self,
        rule: DeterministicMatchRule,
        rec_a: dict[str, Any],
        rec_b: dict[str, Any],
        id_a: str,
        id_b: str,
    ) -> MatchDecision:
        matched_fields: list[str] = []
        all_match = True

        for mf in rule.fields:
            val_a = rec_a.get(mf.field_name)
            val_b = rec_b.get(mf.field_name)

            if val_a is None or val_b is None:
                all_match = False
                break

            a_norm = _normalise(str(val_a)) if mf.normalise else str(val_a)
            b_norm = _normalise(str(val_b)) if mf.normalise else str(val_b)

            if a_norm != b_norm:
                all_match = False
                break
            matched_fields.append(mf.field_name)

        return MatchDecision(
            record_a_id=id_a,
            record_b_id=id_b,
            rule_id=rule.rule_id,
            strategy=MatchStrategy.DETERMINISTIC,
            is_match=all_match,
            confidence_score=1.0 if all_match else 0.0,
            matched_fields=tuple(matched_fields),
            rule_set_version=self._rule_set.rule_set_version,
        )

    def _probabilistic(
        self,
        rule: ProbabilisticMatchRule,
        rec_a: dict[str, Any],
        rec_b: dict[str, Any],
        id_a: str,
        id_b: str,
    ) -> MatchDecision:
        total_score = 0.0
        matched_fields: list[str] = []

        for pf in rule.fields:
            val_a = rec_a.get(pf.field_name)
            val_b = rec_b.get(pf.field_name)

            if val_a is None or val_b is None:
                continue

            sim = _field_similarity(str(val_a), str(val_b), pf.similarity_kind)
            total_score += sim * pf.weight
            if sim > 0.5:
                matched_fields.append(pf.field_name)

        is_match = total_score >= rule.match_threshold

        return MatchDecision(
            record_a_id=id_a,
            record_b_id=id_b,
            rule_id=rule.rule_id,
            strategy=MatchStrategy.PROBABILISTIC,
            is_match=is_match,
            confidence_score=round(total_score, 4),
            matched_fields=tuple(matched_fields),
            rule_set_version=self._rule_set.rule_set_version,
        )


# ---------------------------------------------------------------------------
# Normalisation & similarity helpers
# ---------------------------------------------------------------------------


def _normalise(value: str) -> str:
    """Unicode-normalise, lower-case, and collapse whitespace."""
    nfkd = unicodedata.normalize("NFKD", value)
    ascii_str = nfkd.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_str).strip().lower()


def _field_similarity(a: str, b: str, kind: str) -> float:
    """Return similarity score in [0.0, 1.0] between two string values."""
    a_norm = _normalise(a)
    b_norm = _normalise(b)

    if kind == "exact":
        return 1.0 if a_norm == b_norm else 0.0

    if kind == "jaro_winkler":
        return _jaro_winkler(a_norm, b_norm)

    if kind == "token_set":
        return _token_set_ratio(a_norm, b_norm)

    # fallback: exact
    return 1.0 if a_norm == b_norm else 0.0


def _jaro_winkler(s1: str, s2: str) -> float:  # noqa: C901
    """Simplified Jaro-Winkler similarity (no external deps required)."""
    if s1 == s2:
        return 1.0
    if not s1 or not s2:
        return 0.0

    len1, len2 = len(s1), len(s2)
    match_dist = max(len1, len2) // 2 - 1

    s1_matches = [False] * len1
    s2_matches = [False] * len2
    matches = 0
    transpositions = 0

    for i in range(len1):
        start = max(0, i - match_dist)
        end = min(i + match_dist + 1, len2)
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    k = 0
    for i in range(len1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1

    jaro = (matches / len1 + matches / len2 + (matches - transpositions / 2) / matches) / 3

    prefix = 0
    for i in range(min(4, min(len1, len2))):
        if s1[i] == s2[i]:
            prefix += 1
        else:
            break

    return jaro + prefix * 0.1 * (1 - jaro)


def _token_set_ratio(s1: str, s2: str) -> float:
    """Token-set ratio: Jaccard similarity of word token sets."""
    tokens_a = set(s1.split())
    tokens_b = set(s2.split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


def stable_cluster_id(source_id: str, entity_type: str, cluster_key: str) -> str:
    """
    Generate a deterministic golden_id from the cluster's canonical key.
    Uses SHA-256 to produce a stable 16-char hex prefix.
    """
    raw = f"{source_id}:{entity_type}:{cluster_key}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"gid-{digest[:16]}"
