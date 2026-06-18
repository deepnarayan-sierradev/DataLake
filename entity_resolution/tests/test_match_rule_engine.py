"""Tests for MatchRuleEngine — Phase 7."""

from __future__ import annotations

from entity_resolution.matching_engine.match_rule_engine import (
    DeterministicMatchField,
    DeterministicMatchRule,
    MatchRuleEngine,
    MatchRuleSet,
    ProbabilisticMatchField,
    ProbabilisticMatchRule,
    _field_similarity,
    _jaro_winkler,
    _normalise,
    _token_set_ratio,
    stable_cluster_id,
)
from entity_resolution.matching_engine.record_blocker import BlockingKeyType, BlockingStrategy

_ENTITY_TYPE = "customer"


def _det_rule(fields, rule_id="rule-det-01"):
    return DeterministicMatchRule(
        rule_id=rule_id,
        fields=tuple(DeterministicMatchField(field_name=f) for f in fields),
    )


def _prob_rule(field_weights, threshold=0.7, rule_id="rule-prob-01"):
    return ProbabilisticMatchRule(
        rule_id=rule_id,
        fields=tuple(
            ProbabilisticMatchField(field_name=f, weight=w, similarity_kind="jaro_winkler")
            for f, w in field_weights.items()
        ),
        match_threshold=threshold,
    )


def _rule_set(*rules, version="1.0.0"):
    return MatchRuleSet(
        entity_type=_ENTITY_TYPE,
        rule_set_version=version,
        rules=tuple(rules),
    )


# ---------------------------------------------------------------------------
# Deterministic matching
# ---------------------------------------------------------------------------


class TestDeterministicMatching:
    def test_exact_email_match(self):
        rule = _det_rule(["email"])
        engine = MatchRuleEngine(_rule_set(rule))
        rec_a = {"id": "1", "email": "alice@example.com"}
        rec_b = {"id": "2", "email": "alice@example.com"}
        decisions = engine.compare(rec_a, rec_b, "id")
        assert len(decisions) == 1
        assert decisions[0].is_match is True
        assert decisions[0].confidence_score == 1.0

    def test_email_mismatch(self):
        rule = _det_rule(["email"])
        engine = MatchRuleEngine(_rule_set(rule))
        decisions = engine.compare(
            {"id": "1", "email": "alice@example.com"},
            {"id": "2", "email": "bob@example.com"},
            "id",
        )
        assert decisions[0].is_match is False

    def test_normalisation_case_insensitive(self):
        rule = _det_rule(["email"])
        engine = MatchRuleEngine(_rule_set(rule))
        decisions = engine.compare(
            {"id": "1", "email": "Alice@EXAMPLE.COM"},
            {"id": "2", "email": "alice@example.com"},
            "id",
        )
        assert decisions[0].is_match is True

    def test_multi_field_all_must_match(self):
        rule = _det_rule(["first_name", "last_name"])
        engine = MatchRuleEngine(_rule_set(rule))
        # first_name matches, last_name doesn't → no match
        decisions = engine.compare(
            {"id": "1", "first_name": "alice", "last_name": "smith"},
            {"id": "2", "first_name": "alice", "last_name": "jones"},
            "id",
        )
        assert decisions[0].is_match is False

    def test_missing_field_does_not_match(self):
        rule = _det_rule(["email"])
        engine = MatchRuleEngine(_rule_set(rule))
        decisions = engine.compare(
            {"id": "1"},
            {"id": "2", "email": "alice@example.com"},
            "id",
        )
        assert decisions[0].is_match is False


# ---------------------------------------------------------------------------
# Probabilistic matching
# ---------------------------------------------------------------------------


class TestProbabilisticMatching:
    def test_high_similarity_name_matches(self):
        rule = _prob_rule({"full_name": 1.0}, threshold=0.8)
        engine = MatchRuleEngine(_rule_set(rule))
        decisions = engine.compare(
            {"id": "1", "full_name": "Jonathan Smith"},
            {"id": "2", "full_name": "Jonathan Smith"},
            "id",
        )
        assert decisions[0].is_match is True

    def test_low_similarity_does_not_match(self):
        rule = _prob_rule({"full_name": 1.0}, threshold=0.8)
        engine = MatchRuleEngine(_rule_set(rule))
        decisions = engine.compare(
            {"id": "1", "full_name": "Alice Brown"},
            {"id": "2", "full_name": "Bob Green"},
            "id",
        )
        assert decisions[0].is_match is False

    def test_confidence_score_in_range(self):
        rule = _prob_rule({"name": 1.0}, threshold=0.5)
        engine = MatchRuleEngine(_rule_set(rule))
        decisions = engine.compare(
            {"id": "1", "name": "Jonathan"},
            {"id": "2", "name": "Jonathan"},
            "id",
        )
        assert 0.0 <= decisions[0].confidence_score <= 1.0


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


class TestClustering:
    def test_two_matching_records_form_one_cluster(self):
        rule = _det_rule(["email"])
        engine = MatchRuleEngine(_rule_set(rule))
        records = [
            {"id": "1", "email": "alice@example.com"},
            {"id": "2", "email": "alice@example.com"},
        ]
        clusters, _ = engine.cluster(records, "id")
        assert len(clusters) == 1
        assert frozenset({"1", "2"}) in clusters

    def test_no_matching_records_each_in_own_cluster(self):
        rule = _det_rule(["email"])
        engine = MatchRuleEngine(_rule_set(rule))
        records = [
            {"id": "1", "email": "alice@example.com"},
            {"id": "2", "email": "bob@example.com"},
        ]
        clusters, _ = engine.cluster(records, "id")
        assert len(clusters) == 2

    def test_transitive_matching_forms_single_cluster(self):
        rule = _det_rule(["email"])
        engine = MatchRuleEngine(_rule_set(rule))
        records = [
            {"id": "1", "email": "alice@example.com"},
            {"id": "2", "email": "alice@example.com"},
            {"id": "3", "email": "alice@example.com"},
        ]
        clusters, _ = engine.cluster(records, "id")
        assert len(clusters) == 1

    def test_audit_decisions_generated(self):
        rule = _det_rule(["email"])
        engine = MatchRuleEngine(_rule_set(rule))
        records = [
            {"id": "1", "email": "alice@example.com"},
            {"id": "2", "email": "alice@example.com"},
        ]
        _, decisions = engine.cluster(records, "id")
        assert len(decisions) > 0
        assert all(d.rule_set_version == "1.0.0" for d in decisions)


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


class TestNormalisationHelpers:
    def test_normalise_unicode(self):
        assert _normalise("Ãlicé") == "alice"

    def test_normalise_whitespace(self):
        assert _normalise("  Alice   Smith  ") == "alice smith"

    def test_jaro_winkler_identical(self):
        assert _jaro_winkler("smith", "smith") == 1.0

    def test_jaro_winkler_empty_strings(self):
        assert _jaro_winkler("", "smith") == 0.0

    def test_token_set_ratio_identical(self):
        assert _token_set_ratio("alice smith", "alice smith") == 1.0

    def test_token_set_ratio_no_overlap(self):
        assert _token_set_ratio("alice", "bob") == 0.0


# ---------------------------------------------------------------------------
# stable_cluster_id
# ---------------------------------------------------------------------------


class TestStableClusterId:
    def test_same_inputs_produce_same_id(self):
        assert stable_cluster_id("sf", "customer", "k1") == stable_cluster_id(
            "sf", "customer", "k1"
        )

    def test_different_inputs_produce_different_ids(self):
        assert stable_cluster_id("sf", "customer", "k1") != stable_cluster_id(
            "sf", "customer", "k2"
        )

    def test_id_starts_with_gid_prefix(self):
        assert stable_cluster_id("sf", "customer", "k1").startswith("gid-")


# ---------------------------------------------------------------------------
# Uncovered branches — targeted gap-fill tests
# ---------------------------------------------------------------------------


class TestClusteringWithBlocking:
    """Cover the blocking_strategy branch in cluster() (lines 199-200)."""

    def test_cluster_with_blocking_strategy_groups_same_domain(self):
        rule = DeterministicMatchRule(
            rule_id="email-exact",
            fields=(DeterministicMatchField(field_name="email"),),
        )
        blocking = BlockingStrategy(
            key_type=BlockingKeyType.EMAIL_DOMAIN,
            source_field="email",
            max_block_size=100,
        )
        rule_set = MatchRuleSet(
            entity_type="company",
            rule_set_version="v1",
            rules=(rule,),
            blocking_strategy=blocking,
        )
        engine = MatchRuleEngine(rule_set)
        records = [
            {"id": "1", "email": "alice@acme.com"},
            {"id": "2", "email": "alice@acme.com"},
            {"id": "3", "email": "bob@other.com"},
        ]
        clusters, _ = engine.cluster(records, "id")
        # Records 1 and 2 share the same email → same cluster; 3 is separate
        assert len(clusters) == 2

    def test_cluster_with_blocking_no_cross_block_comparisons(self):
        rule = DeterministicMatchRule(
            rule_id="email-exact",
            fields=(DeterministicMatchField(field_name="email"),),
        )
        blocking = BlockingStrategy(
            key_type=BlockingKeyType.EMAIL_DOMAIN,
            source_field="email",
            max_block_size=100,
        )
        rule_set = MatchRuleSet(
            entity_type="company",
            rule_set_version="v1",
            rules=(rule,),
            blocking_strategy=blocking,
        )
        engine = MatchRuleEngine(rule_set)
        records = [
            {"id": "1", "email": "x@domain-a.com"},
            {"id": "2", "email": "x@domain-b.com"},
        ]
        clusters, decisions = engine.cluster(records, "id")
        # Different domains → different blocks → no cross-block decisions → 2 clusters
        assert len(clusters) == 2


class TestProbabilisticNullField:
    """Cover the `if val_a is None or val_b is None: continue` branch (line 296)."""

    def test_null_field_in_probabilistic_rule_skipped(self):
        rule = ProbabilisticMatchRule(
            rule_id="name-prob",
            fields=(
                ProbabilisticMatchField(field_name="name", weight=0.6, similarity_kind="jaro_winkler"),
                ProbabilisticMatchField(field_name="phone", weight=0.4, similarity_kind="exact"),
            ),
            match_threshold=0.5,
        )
        engine = MatchRuleEngine(MatchRuleSet(entity_type="x", rule_set_version="v1", rules=(rule,)))
        # phone is absent on both records → field skipped, score based on name only
        decisions = engine.compare(
            {"id": "1", "name": "Alice Smith"},
            {"id": "2", "name": "Alice Smith"},
            "id",
        )
        assert decisions[0].is_match is True  # name alone crosses threshold


class TestFieldSimilarityHelpers:
    """Cover _field_similarity branches for token_set, unknown kind, and edge cases."""

    def test_token_set_similarity_partial_overlap(self):
        score = _field_similarity("alice smith jones", "alice smith", "token_set")
        assert 0.0 < score < 1.0

    def test_token_set_similarity_identical(self):
        assert _field_similarity("hello world", "hello world", "token_set") == 1.0

    def test_token_set_empty_string_returns_zero(self):
        # Covers _token_set_ratio empty tokens branch
        assert _field_similarity("", "bob", "token_set") == 0.0

    def test_unknown_similarity_kind_falls_back_to_exact_match(self):
        # Covers the fallback "exact" branch for unrecognised kind
        assert _field_similarity("alice", "alice", "unknown_kind") == 1.0
        assert _field_similarity("alice", "bob", "unknown_kind") == 0.0

    def test_exact_similarity_kind_matching_values(self):
        # Covers line 335: `return 1.0 if a_norm == b_norm else 0.0` inside kind=="exact"
        assert _field_similarity("alice", "alice", "exact") == 1.0

    def test_exact_similarity_kind_non_matching_values(self):
        # Covers the else-branch (return 0.0) of the same line
        assert _field_similarity("alice", "bob", "exact") == 0.0

    def test_probabilistic_multi_field_all_present_loops_twice(self):
        # Covers branch 300->291: loop iterates ≥2 times and appends matched_fields
        # (sim > 0.5 for first field, loop continues to second field)
        rule = ProbabilisticMatchRule(
            rule_id="multi-exact",
            fields=(
                ProbabilisticMatchField(field_name="name", weight=0.5, similarity_kind="exact"),
                ProbabilisticMatchField(field_name="country", weight=0.5, similarity_kind="exact"),
            ),
            match_threshold=0.8,
        )
        engine = MatchRuleEngine(MatchRuleSet(entity_type="x", rule_set_version="v1", rules=(rule,)))
        decisions = engine.compare(
            {"id": "1", "name": "Alice", "country": "US"},
            {"id": "2", "name": "Alice", "country": "US"},
            "id",
        )
        assert decisions[0].is_match is True
        assert "name" in decisions[0].matched_fields
        assert "country" in decisions[0].matched_fields

    def test_probabilistic_multi_field_low_similarity_skips_append_then_loops(self):
        # Covers branch 300->291 False path: sim <= 0.5 (no append) but loop
        # still has another field → loop body ends without appending and iterates again.
        rule = ProbabilisticMatchRule(
            rule_id="multi-prob",
            fields=(
                # First field: completely different strings → sim ≈ 0.0 (≤ 0.5, no append)
                ProbabilisticMatchField(field_name="name", weight=0.2, similarity_kind="exact"),
                # Second field: exact match → sim = 1.0 (> 0.5, appended)
                ProbabilisticMatchField(field_name="country", weight=0.8, similarity_kind="exact"),
            ),
            match_threshold=0.7,
        )
        engine = MatchRuleEngine(MatchRuleSet(entity_type="x", rule_set_version="v1", rules=(rule,)))
        decisions = engine.compare(
            {"id": "1", "name": "Alice", "country": "US"},
            {"id": "2", "name": "Zork",  "country": "US"},
            "id",
        )
        # name has sim=0 (not appended), country has sim=1.0 (appended)
        assert "country" in decisions[0].matched_fields
        assert "name" not in decisions[0].matched_fields

    def test_jaro_winkler_no_common_chars_returns_zero(self):
        # Covers the `if matches == 0: return 0.0` branch
        assert _jaro_winkler("aaa", "bbb") == 0.0

    def test_jaro_winkler_with_transpositions(self):
        # Covers the transpositions increment branch
        score = _jaro_winkler("MARTHA", "MARHTA")
        assert score > 0.9  # classic jaro-winkler example

    def test_jaro_winkler_one_char_prefix_bonus(self):
        # Covers the prefix loop and winkler adjustment
        score_with_common_prefix = _jaro_winkler("john", "johnny")
        score_no_prefix = _jaro_winkler("john", "alice")
        assert score_with_common_prefix > score_no_prefix
