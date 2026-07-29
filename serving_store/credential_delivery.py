"""
Per-tenant serving-store credential delivery and rotation (DL-SERV-02).

The credential is never emailed, never logged, and is retrievable **exactly once** through a
time-limited link; rotation is self-service.

Security (OWASP A02, A09): the one-time claim is a conditional write, so a second retrieval
fails rather than returning the value again; the value itself lives only in Secrets Manager, and
this module handles a claim token, not a password.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError

from contracts.identifier_policy import validate_tenant_code
from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_TABLE_NAME: Final[str] = "EdlServingCredentialClaim"

# A claim link that lives longer than this is a credential sitting in someone's inbox.
CLAIM_TTL_SECONDS: Final[int] = 900

# Rotation cadence the console surfaces; also the `CredentialRotationAge` alarm threshold.
ROTATION_INTERVAL_DAYS: Final[int] = 90


class ClaimState(StrEnum):
    """One-time claim lifecycle."""

    ISSUED = "issued"
    CLAIMED = "claimed"
    EXPIRED = "expired"
    REVOKED = "revoked"


class CredentialClaimError(Exception):
    """Raised when a claim token is unknown, already used, expired, or revoked."""


def _hash_token(token: str) -> str:
    """Only the hash is stored, so a table read cannot replay a claim."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IssuedClaim:
    """The claim token handed to the console; the caller never sees the credential."""

    tenant_code: str
    claim_id: str
    claim_token: str
    expires_at: str

    @property
    def claim_url_path(self) -> str:
        """Relative path the console renders; the token is in the path, never in a log line."""
        return f"/tenants/{self.tenant_code}/serving-credential/claim/{self.claim_token}"


class ServingCredentialDelivery:
    """Issues, claims, rotates, and revokes a tenant's read-only serving credential."""

    def __init__(
        self,
        environment: str,
        region_name: str,
        secrets_client: Any | None = None,
        table_name: str | None = None,
    ) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        self._secrets = secrets_client or boto3.client("secretsmanager", region_name=region_name)
        resolved = table_name or os.environ.get("SERVING_CLAIM_TABLE") or _TABLE_NAME
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(resolved)

    # ── Issue ─────────────────────────────────────────────────────────────────

    def issue_claim(self, tenant_code: str, *, issued_by: str) -> IssuedClaim:
        """Issue a one-time, time-limited claim for the tenant's reader credential."""
        tenant_code = validate_tenant_code(tenant_code)
        if not issued_by:
            raise ValueError("A credential claim must record who issued it (OWASP A09).")
        token = secrets.token_urlsafe(32)
        claim_id = f"clm-{secrets.token_hex(6)}"
        expires = datetime.now(UTC) + timedelta(seconds=CLAIM_TTL_SECONDS)
        self._table.put_item(
            Item={
                "tenant_code": tenant_code,
                "claim_id": claim_id,
                "token_hash": _hash_token(token),
                "state": ClaimState.ISSUED.value,
                "issued_by": issued_by,
                "issued_at": datetime.now(UTC).isoformat(),
                "expires_at": expires.isoformat(),
                "expires_epoch": int(expires.timestamp()),
                "environment": self._environment,
            }
        )
        _logger.info(
            "serving_credential_claim_issued",
            tenant_code=tenant_code,
            claim_id=claim_id,
            issued_by=issued_by,
        )
        return IssuedClaim(
            tenant_code=tenant_code,
            claim_id=claim_id,
            claim_token=token,
            expires_at=expires.isoformat(),
        )

    # ── Claim ─────────────────────────────────────────────────────────────────

    def claim(self, tenant_code: str, claim_token: str) -> dict[str, str]:
        """
        Redeem a claim exactly once and return the credential.

        The conditional update is what makes "exactly once" true under a concurrent second
        attempt, rather than a check-then-act race.
        """
        tenant_code = validate_tenant_code(tenant_code)
        token_hash = _hash_token(claim_token)
        record = self._find_by_token_hash(tenant_code, token_hash)
        if record is None:
            raise CredentialClaimError("Claim token is not recognised.")
        if str(record["state"]) != ClaimState.ISSUED.value:
            raise CredentialClaimError(
                "Claim token has already been used or was revoked; issue a new one."
            )
        if datetime.fromisoformat(str(record["expires_at"])) < datetime.now(UTC):
            self._mark(tenant_code, str(record["claim_id"]), ClaimState.EXPIRED)
            raise CredentialClaimError("Claim token has expired; issue a new one.")
        try:
            self._table.update_item(
                Key={"tenant_code": tenant_code, "claim_id": str(record["claim_id"])},
                UpdateExpression="SET #state = :claimed, claimed_at = :ts",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":claimed": ClaimState.CLAIMED.value,
                    ":ts": datetime.now(UTC).isoformat(),
                    ":issued": ClaimState.ISSUED.value,
                },
                ConditionExpression="#state = :issued",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise CredentialClaimError(
                    "Claim token was redeemed concurrently; a credential is retrievable once."
                ) from exc
            raise
        credential = self._read_secret(tenant_code)
        _logger.info(
            "serving_credential_claimed", tenant_code=tenant_code, claim_id=record["claim_id"]
        )
        return credential

    def revoke_outstanding_claims(self, tenant_code: str) -> int:
        """Revoke every unredeemed claim — used on rotation and on offboarding."""
        tenant_code = validate_tenant_code(tenant_code)
        revoked = 0
        for record in self._list_claims(tenant_code):
            if str(record.get("state")) == ClaimState.ISSUED.value:
                self._mark(tenant_code, str(record["claim_id"]), ClaimState.REVOKED)
                revoked += 1
        return revoked

    # ── Rotate ────────────────────────────────────────────────────────────────

    def rotate(self, tenant_code: str, *, rotated_by: str) -> str:
        """
        Rotate the reader credential and revoke outstanding claims.

        Revoking first: a claim issued against the old password must not silently hand out a
        stale value after rotation.
        """
        tenant_code = validate_tenant_code(tenant_code)
        if not rotated_by:
            raise ValueError("A rotation must record who requested it.")
        self.revoke_outstanding_claims(tenant_code)
        new_password = secrets.token_urlsafe(24)
        secret_id = serving_credential_secret_id(tenant_code)
        existing = self._read_secret(tenant_code)
        payload = {**existing, "password": new_password, "rotated_at": datetime.now(UTC).isoformat()}
        import json

        self._secrets.put_secret_value(
            SecretId=secret_id, SecretString=json.dumps(payload, separators=(",", ":"))
        )
        _logger.warning(
            "serving_credential_rotated", tenant_code=tenant_code, rotated_by=rotated_by
        )
        # The caller receives the claim path, never the password.
        return self.issue_claim(tenant_code, issued_by=rotated_by).claim_url_path

    def rotation_age_days(self, tenant_code: str) -> float | None:
        """Age of the current credential, feeding `CredentialRotationAge`."""
        credential = self._read_secret(tenant_code)
        rotated_at = credential.get("rotated_at")
        if not rotated_at:
            return None
        try:
            rotated = datetime.fromisoformat(rotated_at)
        except ValueError:
            return None
        age_days = (datetime.now(UTC) - rotated).total_seconds() / 86_400
        record_platform_metric(
            PlatformMetric.CREDENTIAL_ROTATION_AGE, age_days * 86_400, TenantCode=tenant_code
        )
        return age_days

    def is_rotation_due(self, tenant_code: str) -> bool:
        age = self.rotation_age_days(tenant_code)
        return age is None or age >= ROTATION_INTERVAL_DAYS

    # ── Private ───────────────────────────────────────────────────────────────

    def _read_secret(self, tenant_code: str) -> dict[str, str]:
        import json

        try:
            response = self._secrets.get_secret_value(
                SecretId=serving_credential_secret_id(tenant_code)
            )
        except ClientError as exc:
            raise CredentialClaimError(
                f"Serving credential for tenant {tenant_code!r} could not be read: "
                f"{exc.response['Error']['Code']}"
            ) from None
        raw = response.get("SecretString") or "{}"
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            raise CredentialClaimError("Serving credential secret is not valid JSON.") from None
        return {str(k): str(v) for k, v in parsed.items()}

    def _find_by_token_hash(self, tenant_code: str, token_hash: str) -> dict[str, Any] | None:
        for record in self._list_claims(tenant_code):
            if str(record.get("token_hash")) == token_hash:
                return record
        return None

    def _list_claims(self, tenant_code: str) -> list[dict[str, Any]]:
        response = self._table.query(
            KeyConditionExpression="tenant_code = :tc",
            ExpressionAttributeValues={":tc": tenant_code},
        )
        return [dict(item) for item in response.get("Items", [])]

    def _mark(self, tenant_code: str, claim_id: str, state: ClaimState) -> None:
        self._table.update_item(
            Key={"tenant_code": tenant_code, "claim_id": claim_id},
            UpdateExpression="SET #state = :state, updated_at = :ts",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":state": state.value,
                ":ts": datetime.now(UTC).isoformat(),
            },
        )


def serving_credential_secret_id(tenant_code: str) -> str:
    """Per-tenant reader credential path."""
    validate_tenant_code(tenant_code)
    return f"edl/tenants/{tenant_code}/serving-store/reader-credentials"
