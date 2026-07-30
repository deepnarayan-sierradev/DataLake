"""
Continuation tokens carry a DynamoDB key, not a row offset (F7).

The offset form made every page cost the same as the first — the handler re-read the whole result
set and sliced it — and was unstable: a row inserted or resolved between requests shifted the
offset, silently skipping or duplicating rows.

Carrying a key introduces a new attack surface, which is what most of this module tests: a caller
can now craft a token naming another tenant's partition. The KeyConditionExpression already pins
`tenant_code`, but relying on that implicitly is exactly how the twin filter came to read a field
that did not exist, so the check is explicit and asserted here.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

import connector_runtime.api.request_context as cp
from connector_runtime.api.errors import ValidationFailedError


def _event(token: str | None) -> dict[str, Any]:
    return {"queryStringParameters": {"next_token": token} if token else {}}


def _forge(payload: str) -> str:
    return base64.urlsafe_b64encode(payload.encode("ascii")).decode("ascii")


def _envelope(tenant_code: str, key: dict[str, Any]) -> str:
    """A well-formed token: the tenant sits beside the key, not inside it."""
    return _forge(f"{cp.PAGE_TOKEN_PREFIX}{json.dumps({'t': tenant_code, 'k': key})}")


class TestRoundTrip:
    def test_a_key_survives_encode_then_decode(self) -> None:
        key = {"tenant_code": "demo", "sk": "company#c-1"}
        token = cp.encode_page_token(key, "demo")
        assert token is not None
        assert cp.decode_page_token(_event(token), "demo") == key

    def test_a_key_without_tenant_code_also_round_trips(self) -> None:
        """
        The defect this pins. `datalake-entity-extraction-config-<env>` is keyed
        (source_id, entity_id) and
        `datalake-run-audit-log-dev` on its Scan fallback is keyed (run_id, stage) — neither carries
        `tenant_code`. Requiring it *inside the key* made `/entities` hand out a token its own
        validator rejected, and made `/runs` build an ExclusiveStartKey outside the table's key
        schema. The tenant now travels in the envelope, so the check is schema-independent.
        """
        key = {"source_id": "demo#salesforce", "entity_id": "ent-1"}
        token = cp.encode_page_token(key, "demo")
        assert token is not None
        assert cp.decode_page_token(_event(token), "demo") == key

    def test_the_decoded_key_contains_only_what_dynamodb_returned(self) -> None:
        key = {"run_id": "run-1", "stage": "extraction"}
        decoded = cp.decode_page_token(_event(cp.encode_page_token(key, "demo")), "demo")
        assert decoded == key
        assert "tenant_code" not in (decoded or {})

    def test_no_cursor_encodes_to_no_token(self) -> None:
        assert cp.encode_page_token(None, "demo") is None
        assert cp.encode_page_token({}, "demo") is None

    def test_absent_token_decodes_to_none(self) -> None:
        assert cp.decode_page_token(_event(None), "demo") is None

    def test_token_is_opaque(self) -> None:
        token = cp.encode_page_token({"tenant_code": "demo", "sk": "company#c-1"}, "demo")
        assert token is not None
        assert "company" not in token
        assert "demo" not in token


class TestCrossTenantForgeryIsRejected:
    def test_a_key_naming_another_tenant_is_refused(self) -> None:
        forged = _envelope("other", {"sk": "company#c-1"})
        with pytest.raises(ValidationFailedError, match="does not belong to this tenant"):
            cp.decode_page_token(_event(forged), "demo")

    def test_a_bare_key_with_no_envelope_is_refused(self) -> None:
        """
        The reasoning here was wrong twice, and both versions are worth recording.

        First it accepted a token with no tenant at all ("not every table's key carries
        tenant_code") — an opt-out from the check available to any caller who left the field off.
        Then it required `tenant_code` *inside the key*, which was false for two of the four tables
        and broke `/entities` outright. The envelope resolves both: the tenant is always present and
        always checked, and it never has to be part of the key.
        """
        token = _forge(f"{cp.PAGE_TOKEN_PREFIX}{json.dumps({'sk': 'company#c-1'})}")
        with pytest.raises(ValidationFailedError):
            cp.decode_page_token(_event(token), "demo")

    def test_an_envelope_missing_its_key_is_refused(self) -> None:
        token = _forge(f"{cp.PAGE_TOKEN_PREFIX}{json.dumps({'t': 'demo'})}")
        with pytest.raises(ValidationFailedError):
            cp.decode_page_token(_event(token), "demo")

    def test_positive_control_a_well_formed_own_tenant_key_is_accepted(self) -> None:
        token = cp.encode_page_token({"tenant_code": "demo", "sk": "company#c-1"}, "demo")
        assert token is not None
        assert cp.decode_page_token(_event(token), "demo") == {
            "tenant_code": "demo",
            "sk": "company#c-1",
        }


class TestMalformedTokensAre400s:
    @pytest.mark.parametrize(
        "token",
        [
            "not-base64!!",
            _forge("no-marker-here"),
            _forge("datalake-page:not-json"),
            _forge("datalake-page:[]"),
            _forge("datalake-page:{}"),
            _forge("datalake-page:42"),
            _forge('datalake-page:"a string"'),
        ],
    )
    def test_rejected_rather_than_restarting_from_zero(self, token: str) -> None:
        with pytest.raises(ValidationFailedError):
            cp.decode_page_token(_event(token), "demo")

    def test_a_bare_offset_is_no_longer_accepted(self) -> None:
        with pytest.raises(ValidationFailedError):
            cp.decode_page_token(_event(_forge("datalake-page:50")), "demo")
