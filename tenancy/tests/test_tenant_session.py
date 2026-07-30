"""
Tests for tenant-tagged sessions (DL-SEC-01, DL-SEC-02).

The tenant boundary conditions on `aws:PrincipalTag/tenant_code`, and nothing ever set that tag —
the four runtime roles it attaches to are shared across every tenant, so a role tag cannot carry it.
This is the mechanism that can: the stage role assumes a per-stage data role with a session tag.

The properties that matter are the caching ones. A per-tenant cache keyed only by role would hand
one tenant's credentials to the next tenant a warm container serves, which would defeat the boundary
being built — so that is asserted directly rather than assumed from the key expression.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from botocore.exceptions import ClientError

from tenancy.tenant_session import (
    TENANT_DATA_ROLE_ARN_ENV,
    TenantSessionUnavailableError,
    clear_cached_sessions,
    tenant_data_role_arn,
    tenant_scoped_session,
)

_ROLE = "arn:aws:iam::123456789012:role/datalake-tenant-data-extraction-dev-exec"
_REGION = "us-east-1"


class _RecordingSts:
    """Records every assume_role call, so caching and tag propagation are both observable."""

    def __init__(self, *, expires_in: timedelta = timedelta(hours=1)) -> None:
        self.calls: list[dict[str, Any]] = []
        self._expires_in = expires_in

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        index = len(self.calls)
        return {
            "Credentials": {
                "AccessKeyId": f"ASIAKEY{index}",
                "SecretAccessKey": f"secret{index}",
                "SessionToken": f"token{index}",
                "Expiration": datetime.now(UTC) + self._expires_in,
            }
        }


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    clear_cached_sessions()


class TestTheTagIsSent:
    def test_the_tenant_code_travels_as_a_session_tag(self) -> None:
        sts = _RecordingSts()
        tenant_scoped_session("evive", region_name=_REGION, role_arn=_ROLE, sts_client=sts)
        assert sts.calls[0]["Tags"] == [{"Key": "tenant_code", "Value": "evive"}]

    def test_the_session_name_identifies_the_tenant_in_cloudtrail(self) -> None:
        sts = _RecordingSts()
        tenant_scoped_session("evive", region_name=_REGION, role_arn=_ROLE, sts_client=sts)
        assert "evive" in sts.calls[0]["RoleSessionName"]

    def test_the_session_carries_the_assumed_credentials(self) -> None:
        sts = _RecordingSts()
        session = tenant_scoped_session(
            "evive", region_name=_REGION, role_arn=_ROLE, sts_client=sts
        )
        credentials = session.get_credentials()
        assert credentials is not None
        assert credentials.access_key == "ASIAKEY1"

    def test_an_invalid_tenant_code_is_rejected_before_any_sts_call(self) -> None:
        sts = _RecordingSts()
        with pytest.raises(ValueError):
            tenant_scoped_session("BAD_CODE", region_name=_REGION, role_arn=_ROLE, sts_client=sts)
        assert sts.calls == []


class TestCachingIsPerTenant:
    def test_the_same_tenant_reuses_credentials(self) -> None:
        sts = _RecordingSts()
        for _ in range(5):
            tenant_scoped_session("evive", region_name=_REGION, role_arn=_ROLE, sts_client=sts)
        assert len(sts.calls) == 1, "a warm container must not call STS per read"

    def test_a_different_tenant_gets_different_credentials(self) -> None:
        """
        The assertion that matters most. A cache keyed by role alone would hand the second tenant
        the
        first tenant's session tag, which is precisely the cross-tenant read the boundary exists to
        stop — introduced by the mechanism meant to prevent it.
        """
        sts = _RecordingSts()
        first = tenant_scoped_session("evive", region_name=_REGION, role_arn=_ROLE, sts_client=sts)
        second = tenant_scoped_session(
            "acme-corp", region_name=_REGION, role_arn=_ROLE, sts_client=sts
        )

        assert len(sts.calls) == 2
        assert [call["Tags"][0]["Value"] for call in sts.calls] == ["evive", "acme-corp"]
        first_credentials = first.get_credentials()
        second_credentials = second.get_credentials()
        assert first_credentials is not None and second_credentials is not None
        assert first_credentials.access_key != second_credentials.access_key

    def test_credentials_near_expiry_are_refreshed(self) -> None:
        sts = _RecordingSts(expires_in=timedelta(minutes=2))
        tenant_scoped_session("evive", region_name=_REGION, role_arn=_ROLE, sts_client=sts)
        tenant_scoped_session("evive", region_name=_REGION, role_arn=_ROLE, sts_client=sts)
        assert len(sts.calls) == 2


class TestFailureIsNeverAFallback:
    def test_a_failed_assume_role_raises_rather_than_using_ambient_credentials(self) -> None:
        """
        A fallback to the Lambda's own credentials would make the boundary's coverage depend on
        whether STS happened to succeed — indistinguishable from no boundary, and invisible.
        """

        class _FailingSts:
            def assume_role(self, **_kwargs: Any) -> dict[str, Any]:
                raise ClientError({"Error": {"Code": "AccessDenied"}}, "AssumeRole")

        with pytest.raises(TenantSessionUnavailableError, match="Refusing to fall back"):
            tenant_scoped_session(
                "evive", region_name=_REGION, role_arn=_ROLE, sts_client=_FailingSts()
            )

    def test_an_undeployed_role_is_an_explicit_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(TENANT_DATA_ROLE_ARN_ENV, raising=False)
        assert tenant_data_role_arn() is None
        with pytest.raises(TenantSessionUnavailableError, match="is not set"):
            tenant_scoped_session("evive", region_name=_REGION, sts_client=_RecordingSts())

    def test_the_role_comes_from_the_environment_when_not_passed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(TENANT_DATA_ROLE_ARN_ENV, _ROLE)
        sts = _RecordingSts()
        tenant_scoped_session("evive", region_name=_REGION, sts_client=sts)
        assert sts.calls[0]["RoleArn"] == _ROLE


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
