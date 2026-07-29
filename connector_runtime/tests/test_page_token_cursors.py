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


class TestRoundTrip:
    def test_a_key_survives_encode_then_decode(self) -> None:
        key = {"tenant_code": "demo", "sk": "company#c-1"}
        token = cp.encode_page_token(key)
        assert token is not None
        assert cp.decode_page_token(_event(token), "demo") == key

    def test_no_cursor_encodes_to_no_token(self) -> None:
        # The last page must not advertise a next page.
        assert cp.encode_page_token(None) is None
        assert cp.encode_page_token({}) is None

    def test_absent_token_decodes_to_none(self) -> None:
        assert cp.decode_page_token(_event(None), "demo") is None

    def test_token_is_opaque(self) -> None:
        token = cp.encode_page_token({"tenant_code": "demo", "sk": "company#c-1"})
        assert token is not None
        assert "company" not in token
        assert "demo" not in token


class TestCrossTenantForgeryIsRejected:
    def test_a_key_naming_another_tenant_is_refused(self) -> None:
        # The assertion that matters: the cursor cannot be used to reach across the boundary.
        forged = _forge(f"{cp.PAGE_TOKEN_PREFIX}{json.dumps({'tenant_code': 'other'})}")
        with pytest.raises(ValidationFailedError, match="does not belong to this tenant"):
            cp.decode_page_token(_event(forged), "demo")

    def test_a_key_without_a_tenant_is_refused(self) -> None:
        """
        This asserted the opposite until 2026-07-29, on the reasoning that "not every table's key
        carries tenant_code". Every table this function decodes for does: `EdlTwinIndex` and
        `EdlDataQualityException` are both partitioned on `tenant_code`. So the omission was not a
        legitimate shape being accommodated — it was an opt-out from the check, available to any
        caller willing to leave the field off. A guard a caller can skip is not a guard.
        """
        token = _forge(f"{cp.PAGE_TOKEN_PREFIX}{json.dumps({'sk': 'company#c-1'})}")
        with pytest.raises(ValidationFailedError):
            cp.decode_page_token(_event(token), "demo")

    def test_positive_control_a_well_formed_own_tenant_key_is_accepted(self) -> None:
        # Without this, a decoder that rejected everything would pass every test in this class.
        token = cp.encode_page_token({"tenant_code": "demo", "sk": "company#c-1"})
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
            _forge("edl-page:not-json"),
            _forge("edl-page:[]"),
            _forge("edl-page:{}"),
            _forge("edl-page:42"),
            _forge('edl-page:"a string"'),
        ],
    )
    def test_rejected_rather_than_restarting_from_zero(self, token: str) -> None:
        # A silent restart would loop a paginating client forever.
        with pytest.raises(ValidationFailedError):
            cp.decode_page_token(_event(token), "demo")

    def test_a_bare_offset_is_no_longer_accepted(self) -> None:
        # The previous implementation took an integer offset; an old client's token must fail
        # loudly rather than be reinterpreted as a key.
        with pytest.raises(ValidationFailedError):
            cp.decode_page_token(_event(_forge("edl-page:50")), "demo")
