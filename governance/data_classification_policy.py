"""
Data classification policy.

Defines PII and sensitivity classification for source entity fields.
Classification drives:
  - Masking/tokenization in the curated layer writer
  - Access control tiers in IAM and Glue
  - Retention policy enforcement
  - Audit trail requirements

Classification levels (ascending sensitivity):
  PUBLIC        — no restrictions; freely queryable
  INTERNAL      — internal use only; not exposed externally
  CONFIDENTIAL  — restricted access; logged on query
  PII           — personal data; masked in curated and analytics layers
  SENSITIVE_PII — high-risk personal data (SSN, financial); always tokenised

Security (OWASP A01, A04):
  - Classification policies are configuration artefacts, not runtime input.
  - Field masking applied before any write to curated or analytics layers.
  - PII fields never appear in quality violation logs or lineage records.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)


class DataClassificationLevel(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    PII = "pii"
    SENSITIVE_PII = "sensitive_pii"


class MaskingStrategy(StrEnum):
    NONE = "none"
    PARTIAL_MASK = "partial_mask"  # show last N chars; rest replaced with *
    FULL_MASK = "full_mask"  # replace entire value with ***
    TOKENISE = "tokenise"  # replace with a deterministic pseudonym
    HASH = "hash"  # SHA-256 hex of the value (irreversible)
    REDACT = "redact"  # replace with literal "REDACTED"


@dataclass(frozen=True)
class FieldClassification:
    """Classification rule for one field in a source entity."""

    field_name: str
    classification: DataClassificationLevel
    masking_strategy: MaskingStrategy
    visible_chars: int = 4  # for PARTIAL_MASK


@dataclass(frozen=True)
class EntityClassificationPolicy:
    """
    Complete classification policy for one source entity.

    Fields not listed default to INTERNAL / no masking.
    """

    source_id: str
    entity_id: str
    policy_version: str
    field_classifications: tuple[FieldClassification, ...]

    @property
    def pii_field_names(self) -> frozenset[str]:
        return frozenset(
            f.field_name
            for f in self.field_classifications
            if f.classification
            in (DataClassificationLevel.PII, DataClassificationLevel.SENSITIVE_PII)
        )


# ---------------------------------------------------------------------------
# Built-in classification heuristics (pattern-based auto-detection)
# ---------------------------------------------------------------------------

_PII_FIELD_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in [
        r"^email",
        r"^phone",
        r"^mobile",
        r"^ssn",
        r"^social.?security",
        r"^date.?of.?birth",
        r"^dob$",
        r"^birth.?date",
        r"^national.?id",
        r"^passport",
        r"^credit.?card",
        r"^account.?number",
        r"^ip.?address",
    ]
)

_SENSITIVE_FIELD_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in [
        r"^ssn",
        r"^social.?security",
        r"^credit.?card",
        r"^cvv",
        r"^tax.?id",
    ]
)


def auto_classify_field(field_name: str) -> DataClassificationLevel:
    """
    Return a best-effort classification for a field based on its name.

    This is a heuristic aid — definitive classification must be reviewed
    by a data steward and recorded in an EntityClassificationPolicy.
    """
    for pattern in _SENSITIVE_FIELD_PATTERNS:
        if pattern.search(field_name):
            return DataClassificationLevel.SENSITIVE_PII
    for pattern in _PII_FIELD_PATTERNS:
        if pattern.search(field_name):
            return DataClassificationLevel.PII
    return DataClassificationLevel.INTERNAL


# ---------------------------------------------------------------------------
# Masking applier
# ---------------------------------------------------------------------------


class TokenisationKeyMissingError(Exception):
    """
    Raised when TOKENISE masking is requested but no secret key was provided.

    Load the tokenisation key from AWS Secrets Manager and inject it into
    FieldMaskingApplier at construction time.  Never hardcode the key.
    """


class FieldMaskingApplier:
    """
    Applies masking strategies defined in an EntityClassificationPolicy to
    a list of canonical records before curated layer publication.

    The original records are never mutated — a new list is returned.

    For TOKENISE masking, a secret HMAC key must be provided to prevent
    dictionary re-identification of low-entropy PII values (OWASP A02).
    Load the key from AWS Secrets Manager; never hardcode or log it.
    """

    def __init__(self, tokenisation_secret: bytes | None = None) -> None:
        """
        Args:
            tokenisation_secret: 32-byte secret key for HMAC-SHA256 tokenisation.
                When None, any field with MaskingStrategy.TOKENISE will raise
                TokenisationKeyMissingError at masking time.
        """
        self._tokenisation_secret = tokenisation_secret

    def apply(
        self,
        records: list[dict[str, Any]],
        policy: EntityClassificationPolicy,
    ) -> list[dict[str, Any]]:
        """
        Return a new list of records with classified fields masked.
        Original records are unchanged.
        """
        classification_map = {fc.field_name: fc for fc in policy.field_classifications}
        masked_records: list[dict[str, Any]] = []

        for record in records:
            masked = dict(record)
            for field_name, fc in classification_map.items():
                if field_name in masked and masked[field_name] is not None:
                    masked[field_name] = self._mask(
                        str(masked[field_name]), fc, self._tokenisation_secret
                    )
            masked_records.append(masked)

        pii_count = len(policy.pii_field_names & set(classification_map))
        if pii_count > 0:
            _logger.info(
                "pii_masking_applied",
                source_id=policy.source_id,
                entity_id=policy.entity_id,
                pii_field_count=pii_count,
                record_count=len(records),
            )

        return masked_records

    @staticmethod
    def _mask(value: str, fc: FieldClassification, secret: bytes | None = None) -> str:
        if fc.masking_strategy == MaskingStrategy.NONE:
            return value
        if fc.masking_strategy == MaskingStrategy.FULL_MASK:
            return "***"
        if fc.masking_strategy == MaskingStrategy.REDACT:
            return "REDACTED"
        if fc.masking_strategy == MaskingStrategy.PARTIAL_MASK:
            visible = fc.visible_chars
            if len(value) <= visible:
                return "*" * len(value)
            return "*" * (len(value) - visible) + value[-visible:]
        if fc.masking_strategy == MaskingStrategy.HASH:
            return hashlib.sha256(value.encode("utf-8")).hexdigest()
        if fc.masking_strategy == MaskingStrategy.TOKENISE:
            # HMAC-SHA256 pseudonymisation: re-identification requires the secret key.
            # An unsalted hash (SHA-256 without key) is reversible by dictionary attack
            # for low-entropy inputs like email addresses or phone numbers (OWASP A02).
            if secret is None:
                raise TokenisationKeyMissingError(
                    "TOKENISE masking requires a tokenisation_secret. "
                    "Load the 32-byte key from AWS Secrets Manager and inject it into "
                    "FieldMaskingApplier at construction time. Never hardcode the key."
                )
            digest = hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()
            return f"TOKEN-{digest[:16].upper()}"
        return value  # fallback: no masking
