"""
List endpoints must paginate rather than truncate silently (S12).

The defect: `list_twins(...)[:200]` returned a truncated list indistinguishable from a complete
one, so a dashboard built on it under-reports with no signal at all. At 80-100 entities per tenant
with many golden records that is the normal case, not an edge case.

The contract asserted here: a caller can tell there is more (`next_token`), can fetch it, and
cannot forge or corrupt the cursor.
"""

from __future__ import annotations

import base64

import pytest

from connector_runtime.api.control_plane_handler import (
    _encode_page_token,
    _page_offset,
)
from connector_runtime.api.errors import ValidationFailedError


def _event(token: str | None) -> dict[str, object]:
    return {"queryStringParameters": {"next_token": token} if token else {}}


class TestPageTokenRoundTrip:
    def test_absent_token_starts_at_the_beginning(self) -> None:
        assert _page_offset(_event(None)) == 0

    def test_an_empty_token_starts_at_the_beginning(self) -> None:
        assert _page_offset(_event("")) == 0

    @pytest.mark.parametrize("offset", [0, 1, 200, 4_000, 1_000_000])
    def test_a_token_round_trips(self, offset: int) -> None:
        assert _page_offset(_event(_encode_page_token(offset))) == offset

    def test_the_token_is_opaque_rather_than_a_bare_offset(self) -> None:
        # A bare integer would invite a caller to construct offsets by hand, which makes the
        # response's totals inconsistent with the page it returned.
        token = _encode_page_token(200)
        assert token != "200"
        assert "200" not in token


class TestPageTokenValidation:
    def test_a_malformed_token_is_a_client_error(self) -> None:
        # Not a silent restart from zero: a paginating client that silently restarts loops
        # forever over the first page.
        with pytest.raises(ValidationFailedError, match="continuation token"):
            _page_offset(_event("not-base64!!"))

    def test_a_bare_integer_is_rejected(self) -> None:
        with pytest.raises(ValidationFailedError):
            _page_offset(_event(base64.urlsafe_b64encode(b"200").decode("ascii")))

    def test_a_negative_offset_is_rejected(self) -> None:
        forged = base64.urlsafe_b64encode(b"edl-page:-5").decode("ascii")
        with pytest.raises(ValidationFailedError):
            _page_offset(_event(forged))

    def test_a_non_numeric_offset_is_rejected(self) -> None:
        forged = base64.urlsafe_b64encode(b"edl-page:DROP TABLE").decode("ascii")
        with pytest.raises(ValidationFailedError):
            _page_offset(_event(forged))
