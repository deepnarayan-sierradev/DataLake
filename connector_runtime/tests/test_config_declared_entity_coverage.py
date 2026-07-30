"""
DL-CONN-21 across *every* registered REST source, not just the ones it was written against.

Two distinct guarantees live here, and the second is why this file exists separately from
`test_rest_substrate_extensions.py`:

1. **Every REST source accepts a config-declared entity.** The mechanism sits in the shared
   builder, so this should be automatic — but "should be automatic" is exactly the claim that
   `test_capability_reachability.py` (G10) exists to stop anyone from making without proof.

2. **What a config-declared entity inherits is actually right for that source.** This is the
   sharper risk. Inheriting `("results",)` on a source whose envelope is `items` produces
   **zero records and no error** — a silent empty extraction that looks like a healthy run
   with no data. Five sources were wrong this way immediately after DL-CONN-21 landed
   (dialpad, google-analytics, housecall-pro, meta-ads, servman-pro), which is what prompted
   the reconciliation below rather than a per-source spot check.

Imports only the deployed entry point, so the registry is populated the way production
populates it — an adapter this file forgot cannot hide.
"""

from __future__ import annotations

from typing import Any

import pytest

# The deployed entry point, imported for its registration side effects (G2's discipline).
import connector_runtime.extraction_pipeline_handler  # noqa: F401
from connector_runtime.adapters.rest_api.rest_adapter_registration import (
    RestSourceParams,
    UndeclaredEntityError,
    resolve_entity_spec,
)
from connector_runtime.adapters.rest_api.rest_source_spec import (
    MAX_REQUEST_TIMEOUT_SECONDS,
    RestSourceSpec,
    rest_source_spec_registry,
)
from connector_runtime.pagination import pagination_strategy_registry
from connector_runtime.source_capabilities import SourceCapability

_SOURCE_IDS = sorted(rest_source_spec_registry.registered_source_ids())


def _spec(source_id: str) -> RestSourceSpec:
    return rest_source_spec_registry.get(source_id)


def _params(source_id: str, **overrides: Any) -> RestSourceParams:
    payload: dict[str, Any] = {"entity_id": f"{source_id}-console-added"}
    payload.update(overrides)
    return RestSourceParams.model_validate(payload)


class TestEveryRestSourceAcceptsAConfigDeclaredEntity:
    def test_the_registry_is_not_empty(self) -> None:
        # Guards against the whole file passing vacuously if registration ever breaks.
        assert len(_SOURCE_IDS) >= 12

    @pytest.mark.parametrize("source_id", _SOURCE_IDS)
    def test_an_entity_the_spec_never_declared_resolves(self, source_id: str) -> None:
        resolved = resolve_entity_spec(
            _spec(source_id), _params(source_id, entity_path="/v1/console-added")
        )
        assert resolved.entity_id == f"{source_id}-console-added"
        assert resolved.path == "/v1/console-added"

    @pytest.mark.parametrize("source_id", _SOURCE_IDS)
    def test_it_resolves_to_a_runnable_pagination_strategy(self, source_id: str) -> None:
        resolved = resolve_entity_spec(
            _spec(source_id), _params(source_id, entity_path="/v1/console-added")
        )
        name = resolved.pagination_strategy or _spec(source_id).default_pagination_strategy
        assert name in pagination_strategy_registry.registered_names()

    @pytest.mark.parametrize("source_id", _SOURCE_IDS)
    def test_a_declared_entity_still_wins(self, source_id: str) -> None:
        declared_id = _spec(source_id).entity_ids()[0]
        declared = _spec(source_id).entity(declared_id)
        resolved = resolve_entity_spec(
            _spec(source_id),
            _params(source_id, entity_id=declared_id, entity_path="/v1/somewhere-else"),
        )
        assert resolved.path == declared.path

    @pytest.mark.parametrize("source_id", _SOURCE_IDS)
    def test_an_unknown_entity_without_a_path_fails_closed(self, source_id: str) -> None:
        with pytest.raises(UndeclaredEntityError, match="entity_path"):
            resolve_entity_spec(_spec(source_id), _params(source_id))

    @pytest.mark.parametrize("source_id", _SOURCE_IDS)
    def test_configuration_can_never_enable_writeback(self, source_id: str) -> None:
        resolved = resolve_entity_spec(
            _spec(source_id), _params(source_id, entity_path="/v1/console-added")
        )
        assert resolved.supports_writeback is False


class TestStructuralTrapsNoSourceMayFallInto:
    """
    Cross-source invariants for the two ways a spec can look right and read nothing.

    Both were live on 2026-07-30: WellSky drove an unpaginated endpoint with `page_number`
    (which re-requests page 0 to the 10,000-page ceiling), and several endpoints carry a
    `{patient_id}` template the substrate never substitutes.
    """

    @pytest.mark.parametrize("source_id", _SOURCE_IDS)
    def test_no_read_path_carries_an_unsubstituted_template(self, source_id: str) -> None:
        offenders = [
            e.entity_id for e in _spec(source_id).entities if "{" in e.path or "}" in e.path
        ]
        assert not offenders, (
            f"{source_id}: {offenders} declare a path template. Nothing substitutes it, so "
            "the request is issued literally and 404s. Use a parent-scoped fan-out."
        )

    @pytest.mark.parametrize("source_id", _SOURCE_IDS)
    def test_keyset_paging_always_has_a_key_to_seek_on(self, source_id: str) -> None:
        # KeysetPagination raises at run time without one; catching it here means the
        # failure surfaces in CI rather than on the first scheduled extraction.
        for entity in _spec(source_id).entities:
            if entity.pagination_strategy == "keyset":
                assert entity.keyset_field, (
                    f"{source_id}/{entity.entity_id}: keyset paging with no keyset_field "
                    "cannot seek and raises on the first page."
                )

    @pytest.mark.parametrize("source_id", _SOURCE_IDS)
    def test_the_read_timeout_leaves_room_for_the_run_to_checkpoint(self, source_id: str) -> None:
        spec = _spec(source_id)
        assert 1.0 <= spec.request_timeout_seconds <= MAX_REQUEST_TIMEOUT_SECONDS

    @pytest.mark.parametrize("source_id", _SOURCE_IDS)
    def test_a_watermark_field_is_only_claimed_where_an_entity_can_filter(
        self, source_id: str
    ) -> None:
        # A watermark on an entity whose source cannot filter incrementally advances state
        # against an unfiltered read. Either the capability is declared, or no entity claims
        # a watermark field.
        spec = _spec(source_id)
        claims = [e.entity_id for e in spec.entities if e.watermark_field]
        if SourceCapability.INCREMENTAL not in spec.capabilities:
            assert not claims, (
                f"{source_id} does not declare the incremental capability, but {claims} "
                "carry a watermark field. The watermark would advance against a full load."
            )


class TestInheritedDefaultsMatchWhatTheSourceActuallyUses:
    """
    A config-declared entity inherits the source's conventions, so those conventions have to
    be the source's real ones. Where every declared entity agrees on a value, the default
    must be that value — otherwise a console-added entity reads the wrong shape.
    """

    @pytest.mark.parametrize("source_id", _SOURCE_IDS)
    def test_the_records_json_path_default_matches_its_entities(self, source_id: str) -> None:
        spec = _spec(source_id)
        used = {entity.records_json_path for entity in spec.entities}
        if len(used) != 1:
            pytest.skip(
                f"{source_id} entities use more than one envelope: {sorted(map(str, used))}"
            )
        expected = next(iter(used))
        assert spec.default_records_json_path == expected, (
            f"{source_id}: entities read records from {expected}, but a config-declared "
            f"entity would inherit {spec.default_records_json_path}. That mismatch yields "
            "zero records and no error — a silent empty extraction (DL-CONN-21)."
        )

    @pytest.mark.parametrize("source_id", _SOURCE_IDS)
    def test_the_page_size_default_matches_its_entities(self, source_id: str) -> None:
        spec = _spec(source_id)
        used = {entity.page_size for entity in spec.entities}
        if len(used) != 1:
            pytest.skip(f"{source_id} entities use more than one page size: {sorted(used)}")
        assert spec.default_page_size == next(iter(used)), (
            f"{source_id}: a config-declared entity would inherit page_size "
            f"{spec.default_page_size} while every declared entity uses {next(iter(used))}."
        )

    @pytest.mark.parametrize("source_id", _SOURCE_IDS)
    def test_the_pagination_default_is_one_its_entities_use(self, source_id: str) -> None:
        spec = _spec(source_id)
        declared = {
            entity.pagination_strategy for entity in spec.entities if entity.pagination_strategy
        }
        if not declared:
            pytest.skip(f"{source_id} declares no per-entity pagination strategy")
        assert spec.default_pagination_strategy in declared, (
            f"{source_id}: default_pagination_strategy is "
            f"{spec.default_pagination_strategy!r}, which none of its entities use "
            f"({sorted(declared)}). A config-declared entity would page the wrong way."
        )

    @pytest.mark.parametrize("source_id", _SOURCE_IDS)
    def test_the_inherited_envelope_is_reachable_from_a_resolved_entity(
        self, source_id: str
    ) -> None:
        # The end-to-end form of the assertion above: what resolve_entity_spec hands back
        # must carry the source's envelope, not the dataclass's generic default.
        spec = _spec(source_id)
        resolved = resolve_entity_spec(spec, _params(source_id, entity_path="/v1/console-added"))
        assert resolved.records_json_path == spec.default_records_json_path
