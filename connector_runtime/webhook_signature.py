"""
Webhook payload signature verification (DL-CONN-14, OWASP A08).

One function parameterised by algorithm, not one per provider. Verification is mandatory
and fails closed; an unsigned provider is polled, never webhooked.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

# Reject a signed payload whose timestamp is outside this window (replay defence).
DEFAULT_TIMESTAMP_TOLERANCE_SECONDS: Final[int] = 300


class SignatureAlgorithm(StrEnum):
    """Signature shapes the providers on the source list actually use."""

    HMAC_SHA256_HEX = "hmac_sha256_hex"
    HMAC_SHA256_BASE64 = "hmac_sha256_base64"
    HMAC_SHA1_HEX = "hmac_sha1_hex"


class WebhookSignatureError(Exception):
    """Raised when a webhook payload's signature is absent, malformed, or wrong."""


@dataclass(frozen=True)
class SignatureSpec:
    """How one provider signs its webhooks."""

    algorithm: SignatureAlgorithm
    signature_header: str
    timestamp_header: str | None = None
    signed_payload_template: str = "{body}"
    timestamp_tolerance_seconds: int = DEFAULT_TIMESTAMP_TOLERANCE_SECONDS


_DIGESTS: Final[dict[SignatureAlgorithm, str]] = {
    SignatureAlgorithm.HMAC_SHA256_HEX: "sha256",
    SignatureAlgorithm.HMAC_SHA256_BASE64: "sha256",
    SignatureAlgorithm.HMAC_SHA1_HEX: "sha1",
}


def compute_signature(algorithm: SignatureAlgorithm, secret: str, signed_payload: str) -> str:
    """Compute the expected signature for a signed payload."""
    digest_name = _DIGESTS[algorithm]
    mac = hmac.new(secret.encode("utf-8"), signed_payload.encode("utf-8"), digest_name)
    if algorithm is SignatureAlgorithm.HMAC_SHA256_BASE64:
        return base64.b64encode(mac.digest()).decode("ascii")
    return mac.hexdigest()


def verify_webhook_signature(
    spec: SignatureSpec,
    secret: str,
    body: str,
    headers: Mapping[str, str],
    *,
    now_epoch_seconds: float | None = None,
) -> None:
    """
    Verify a webhook payload, raising on any failure.

    Fails closed on an absent header, a stale timestamp, or a mismatch — there is no path
    through this function that accepts an unverified payload.
    """
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    provided = lowered.get(spec.signature_header.lower())
    if not provided:
        raise WebhookSignatureError(
            f"Webhook payload carries no {spec.signature_header!r} header. Unsigned providers "
            "must be polled, not webhooked."
        )

    timestamp = ""
    if spec.timestamp_header:
        timestamp = lowered.get(spec.timestamp_header.lower(), "")
        if not timestamp:
            raise WebhookSignatureError(
                f"Webhook payload carries no {spec.timestamp_header!r} header, so replay "
                "cannot be bounded."
            )
        _guard_timestamp_freshness(timestamp, spec.timestamp_tolerance_seconds, now_epoch_seconds)

    signed_payload = spec.signed_payload_template.format(body=body, timestamp=timestamp)
    expected = compute_signature(spec.algorithm, secret, signed_payload)
    # Constant-time comparison — a timing side channel would leak the expected signature.
    if not hmac.compare_digest(expected, provided.strip()):
        raise WebhookSignatureError("Webhook payload signature does not match the expected value.")


def _guard_timestamp_freshness(
    timestamp: str, tolerance_seconds: int, now_epoch_seconds: float | None
) -> None:
    try:
        sent_at = float(timestamp)
    except ValueError as exc:
        raise WebhookSignatureError(
            f"Webhook timestamp header {timestamp!r} is not a numeric epoch value."
        ) from exc
    # Providers send milliseconds (HubSpot) or seconds; normalise on magnitude.
    if sent_at > 1e11:
        sent_at /= 1000.0
    if now_epoch_seconds is None:
        import time

        now_epoch_seconds = time.time()
    if abs(now_epoch_seconds - sent_at) > tolerance_seconds:
        raise WebhookSignatureError(
            f"Webhook timestamp is outside the {tolerance_seconds}s tolerance window; "
            "treating it as a replay."
        )


# Provider specs. HubSpot v2 signs method+uri+body+timestamp; the receiver supplies the
# rendered prefix in `signed_payload_template`.
WEBHOOK_SIGNATURE_SPECS: Final[dict[str, SignatureSpec]] = {
    "hubspot": SignatureSpec(
        algorithm=SignatureAlgorithm.HMAC_SHA256_BASE64,
        signature_header="X-HubSpot-Signature-V3",
        timestamp_header="X-HubSpot-Request-Timestamp",
        signed_payload_template="{body}{timestamp}",
    ),
    "dialpad": SignatureSpec(
        algorithm=SignatureAlgorithm.HMAC_SHA256_HEX,
        signature_header="X-Dialpad-Signature",
    ),
    "housecall-pro": SignatureSpec(
        algorithm=SignatureAlgorithm.HMAC_SHA256_HEX,
        signature_header="X-Housecall-Signature",
        timestamp_header="X-Housecall-Timestamp",
        signed_payload_template="{timestamp}.{body}",
    ),
}


def spec_for_source(source_id: str) -> SignatureSpec:
    """The provider's signature spec; a source with none is not webhook-capable."""
    spec = WEBHOOK_SIGNATURE_SPECS.get(source_id)
    if spec is None:
        raise WebhookSignatureError(
            f"Source {source_id!r} has no webhook signature spec, so its webhooks cannot be "
            "verified. Poll it instead (DL-CONN-13)."
        )
    return spec


def sha256_hex(value: str) -> str:
    """Stable hash used for webhook event dedup keys."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
