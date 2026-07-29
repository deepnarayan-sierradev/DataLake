"""Shared narrowing for `requests_mock.last_request`, which is Optional at the type level."""

from __future__ import annotations

import requests_mock as requests_mock_lib


def _sent_request(mock: requests_mock_lib.Mocker) -> requests_mock_lib.request._RequestObjectProxy:
    """Assert a request was actually sent and return it, so `.url`/`.text` reads are typed."""
    request = mock.last_request
    assert request is not None, "expected the client to have sent a request"
    return request
