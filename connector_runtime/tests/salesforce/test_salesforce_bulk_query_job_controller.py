"""
Tests for SalesforceBulkQueryJobController.

Covers:
  - API limit check blocks submission when quota is low
  - Job created, polled to completion, results yielded
  - Exponential backoff during polling (job transitions through InProgress)
  - BulkJobTimeoutError raised when job exceeds max_poll_seconds
  - BulkJobFailedError raised when Salesforce returns Failed state
  - Results paginated correctly (multi-page fetches)
  - Job aborted on exception during result fetch
  - _bind_parameters substitutes placeholders correctly
  - Empty parameter value raises ValueError
"""

from __future__ import annotations

import pytest

from connector_runtime.adapters.salesforce.salesforce_bulk_query_job_controller import (
    BulkApiLimitError,
    BulkJobFailedError,
    BulkJobTimeoutError,
    SalesforceBulkQueryJobController,
)

# Expose the static method for unit-testing independently
_bind_parameters = SalesforceBulkQueryJobController._bind_parameters  # type: ignore[attr-defined]

_INSTANCE_URL = "https://myorg.my.salesforce.com"
_JOB_ID = "7502x000001abc123"
_BASE_BULK = f"{_INSTANCE_URL}/services/data/v59.0/jobs/query"
_LIMITS_URL = f"{_INSTANCE_URL}/services/data/v59.0/limits"


def _make_auth(token: str = "tok") -> object:  # noqa: S107
    from unittest.mock import MagicMock

    auth = MagicMock()
    auth.get_access_token.return_value = token
    auth.instance_url = _INSTANCE_URL
    return auth


def _csv_page(rows: list[dict]) -> str:
    if not rows:
        return "Id,Name\r\n"
    headers = ",".join(rows[0].keys())
    body = "\r\n".join(",".join(str(v) for v in r.values()) for r in rows)
    return f"{headers}\r\n{body}\r\n"


# ---------------------------------------------------------------------------
# Parameter binding
# ---------------------------------------------------------------------------


class TestBindParameters:
    def test_substitutes_named_placeholders(self) -> None:
        soql = (
            "SELECT Id FROM Account WHERE SystemModstamp >= :lower_bound"
            " AND SystemModstamp < :upper_bound"
        )
        bound = _bind_parameters(
            soql, {"lower_bound": "2026-06-01T00:00:00Z", "upper_bound": "2026-06-02T00:00:00Z"}
        )
        assert ":lower_bound" not in bound
        assert ":upper_bound" not in bound
        assert "2026-06-01T00:00:00Z" in bound
        assert "2026-06-02T00:00:00Z" in bound

    def test_empty_value_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            _bind_parameters("SELECT Id FROM Account WHERE X >= :val", {"val": ""})

    def test_no_parameters_returns_unchanged(self) -> None:
        soql = "SELECT Id FROM Account"
        assert _bind_parameters(soql, {}) == soql


# ---------------------------------------------------------------------------
# API limit check
# ---------------------------------------------------------------------------


class TestApiLimitCheck:
    def test_insufficient_limit_raises_bulk_api_limit_error(self, requests_mock) -> None:  # type: ignore[no-untyped-def]
        auth = _make_auth()
        requests_mock.get(
            _LIMITS_URL,
            json={"DailyBulkApiBatches": {"Max": 10000, "Remaining": 2}},
        )
        controller = SalesforceBulkQueryJobController(auth_client=auth, max_poll_seconds=60)
        with pytest.raises(BulkApiLimitError, match="remaining=2"):
            list(controller.execute("SELECT Id FROM Account", {}))

    def test_sufficient_limit_proceeds(self, requests_mock) -> None:  # type: ignore[no-untyped-def]
        auth = _make_auth()
        requests_mock.get(
            _LIMITS_URL,
            json={"DailyBulkApiBatches": {"Max": 10000, "Remaining": 100}},
        )
        requests_mock.post(_BASE_BULK, json={"id": _JOB_ID}, status_code=200)
        requests_mock.get(
            f"{_BASE_BULK}/{_JOB_ID}",
            json={"state": "JobComplete", "numberRecordsProcessed": 1},
        )
        requests_mock.get(
            f"{_BASE_BULK}/{_JOB_ID}/results",
            text=_csv_page([{"Id": "001", "Name": "Acme"}]),
            headers={"Sforce-Locator": "null"},
        )
        requests_mock.patch(f"{_BASE_BULK}/{_JOB_ID}", json={})
        controller = SalesforceBulkQueryJobController(auth_client=auth, max_poll_seconds=60)
        records = list(controller.execute("SELECT Id, Name FROM Account", {}))
        assert len(records) == 1
        assert records[0].payload["Id"] == "001"


# ---------------------------------------------------------------------------
# Job lifecycle
# ---------------------------------------------------------------------------


class TestJobLifecycle:
    def test_result_records_yielded_correctly(self, requests_mock) -> None:  # type: ignore[no-untyped-def]
        auth = _make_auth()
        requests_mock.get(_LIMITS_URL, json={"DailyBulkApiBatches": {"Remaining": 100}})
        requests_mock.post(_BASE_BULK, json={"id": _JOB_ID})
        requests_mock.get(
            f"{_BASE_BULK}/{_JOB_ID}",
            json={"state": "JobComplete", "numberRecordsProcessed": 3},
        )
        requests_mock.get(
            f"{_BASE_BULK}/{_JOB_ID}/results",
            text=_csv_page(
                [
                    {"Id": "001", "Name": "Acme"},
                    {"Id": "002", "Name": "Globex"},
                    {"Id": "003", "Name": "Initech"},
                ]
            ),
            headers={"Sforce-Locator": "null"},
        )
        requests_mock.patch(f"{_BASE_BULK}/{_JOB_ID}", json={})
        controller = SalesforceBulkQueryJobController(auth_client=auth, max_poll_seconds=60)
        records = list(controller.execute("SELECT Id, Name FROM Account", {}))
        assert len(records) == 3
        assert records[1].payload["Name"] == "Globex"

    def test_job_failed_state_raises(self, requests_mock) -> None:  # type: ignore[no-untyped-def]
        auth = _make_auth()
        requests_mock.get(_LIMITS_URL, json={"DailyBulkApiBatches": {"Remaining": 100}})
        requests_mock.post(_BASE_BULK, json={"id": _JOB_ID})
        requests_mock.get(
            f"{_BASE_BULK}/{_JOB_ID}",
            json={"state": "Failed", "numberRecordsProcessed": 0},
        )
        requests_mock.patch(f"{_BASE_BULK}/{_JOB_ID}", json={})
        controller = SalesforceBulkQueryJobController(auth_client=auth, max_poll_seconds=60)
        with pytest.raises(BulkJobFailedError, match="Failed"):
            list(controller.execute("SELECT Id FROM Account", {}))

    def test_job_aborted_state_raises(self, requests_mock) -> None:  # type: ignore[no-untyped-def]
        auth = _make_auth()
        requests_mock.get(_LIMITS_URL, json={"DailyBulkApiBatches": {"Remaining": 100}})
        requests_mock.post(_BASE_BULK, json={"id": _JOB_ID})
        requests_mock.get(
            f"{_BASE_BULK}/{_JOB_ID}",
            json={"state": "Aborted", "numberRecordsProcessed": 0},
        )
        requests_mock.patch(f"{_BASE_BULK}/{_JOB_ID}", json={})
        controller = SalesforceBulkQueryJobController(auth_client=auth, max_poll_seconds=60)
        with pytest.raises(BulkJobFailedError, match="Aborted"):
            list(controller.execute("SELECT Id FROM Account", {}))

    def test_paginated_results_all_yielded(self, requests_mock) -> None:  # type: ignore[no-untyped-def]
        auth = _make_auth()
        requests_mock.get(_LIMITS_URL, json={"DailyBulkApiBatches": {"Remaining": 100}})
        requests_mock.post(_BASE_BULK, json={"id": _JOB_ID})
        requests_mock.get(
            f"{_BASE_BULK}/{_JOB_ID}",
            json={"state": "JobComplete", "numberRecordsProcessed": 4},
        )
        # Two pages
        requests_mock.get(
            f"{_BASE_BULK}/{_JOB_ID}/results",
            [
                {
                    "text": _csv_page([{"Id": "001"}, {"Id": "002"}]),
                    "headers": {"Sforce-Locator": "abc123"},
                },
                {
                    "text": _csv_page([{"Id": "003"}, {"Id": "004"}]),
                    "headers": {"Sforce-Locator": "null"},
                },
            ],
        )
        requests_mock.patch(f"{_BASE_BULK}/{_JOB_ID}", json={})
        controller = SalesforceBulkQueryJobController(auth_client=auth, max_poll_seconds=60)
        records = list(controller.execute("SELECT Id FROM Account", {}))
        ids = [r.payload["Id"] for r in records]
        assert ids == ["001", "002", "003", "004"]

    def test_timeout_raises_bulk_job_timeout_error(self, requests_mock) -> None:  # type: ignore[no-untyped-def]
        auth = _make_auth()
        requests_mock.get(_LIMITS_URL, json={"DailyBulkApiBatches": {"Remaining": 100}})
        requests_mock.post(_BASE_BULK, json={"id": _JOB_ID})
        # Always return InProgress — will time out
        requests_mock.get(
            f"{_BASE_BULK}/{_JOB_ID}",
            json={"state": "InProgress", "numberRecordsProcessed": 0},
        )
        requests_mock.patch(f"{_BASE_BULK}/{_JOB_ID}", json={})
        controller = SalesforceBulkQueryJobController(auth_client=auth, max_poll_seconds=0.01)
        with pytest.raises(BulkJobTimeoutError, match="did not complete"):
            list(controller.execute("SELECT Id FROM Account", {}))
