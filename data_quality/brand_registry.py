"""
Brand as a first-class dimension (DL-DQ-09).

Brand is distinct from `tenant_code` and from `scope_unit_id`: one tenant (Evive) operates
seven brands, and a brand may span several franchisees while a franchisee belongs to exactly
one brand. Brand drives row-level access (DL-SEC-11) and dashboard filtering, so it is a
validated registry rather than a free-text column.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

import boto3

from contracts.identifier_policy import validate_tenant_code
from observability.lambda_runtime import require_env
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)


BRAND_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9\-]{1,47}$")

BRAND_COLUMN: Final[str] = "brand_code"


class UnknownBrandError(Exception):
    """Raised when a record carries a brand code the tenant has not registered."""


def validate_brand_code(value: str) -> str:
    """Validate a brand code slug."""
    if not BRAND_CODE_PATTERN.match(value):
        raise ValueError(
            f"brand_code {value!r} does not conform to the brand code format. Use lowercase "
            "letters, digits, and hyphens only (2-48 chars; must start with a letter). "
            "Example: 'maid-brigade'."
        )
    return value


@dataclass(frozen=True)
class Brand:
    """One brand of a multi-brand tenant."""

    tenant_code: str
    brand_code: str
    display_name: str
    department: str = ""
    active: bool = True

    def __post_init__(self) -> None:
        validate_tenant_code(self.tenant_code)
        validate_brand_code(self.brand_code)
        if not self.display_name.strip():
            raise ValueError(f"brand {self.brand_code!r}: display_name must not be empty.")


EVIVE_BRANDS: Final[tuple[tuple[str, str], ...]] = (
    ("maid-brigade", "Maid Brigade"),
    ("pacific-lawn", "Pacific Lawn & Sprinklers"),
    ("executive-home-care", "Executive Home Care"),
    ("brothers-gutters", "Brothers Gutters"),
    ("shine", "Shine"),
    ("grasons", "Grasons"),
    ("assisted-living-locators", "Assisted Living Locators"),
)


class BrandRegistry:
    """Tenant brand registry; validates the `brand_code` on every curated record."""

    def __init__(self, environment: str, region_name: str) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        table_name = require_env("BRAND_REGISTRY_TABLE")
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)
        self._cache: dict[str, frozenset[str]] = {}

    def register(self, brand: Brand) -> None:
        self._table.put_item(
            Item={
                "tenant_code": brand.tenant_code,
                "brand_code": brand.brand_code,
                "display_name": brand.display_name,
                "department": brand.department,
                "active": brand.active,
                "environment": self._environment,
            }
        )
        self._cache.pop(brand.tenant_code, None)
        _logger.info("brand_registered", tenant_code=brand.tenant_code, brand_code=brand.brand_code)

    def list_brands(self, tenant_code: str) -> list[Brand]:
        tenant_code = validate_tenant_code(tenant_code)
        response = self._table.query(
            KeyConditionExpression="tenant_code = :tc",
            ExpressionAttributeValues={":tc": tenant_code},
        )
        return [
            Brand(
                tenant_code=tenant_code,
                brand_code=str(item["brand_code"]),
                display_name=str(item.get("display_name", item["brand_code"])),
                department=str(item.get("department", "")),
                active=bool(item.get("active", True)),
            )
            for item in response.get("Items", [])
        ]

    def known_brand_codes(self, tenant_code: str) -> frozenset[str]:
        """Cached active brand codes; invalidated on register (DL-CFG-04 signal-driven)."""
        cached = self._cache.get(tenant_code)
        if cached is not None:
            return cached
        codes = frozenset(b.brand_code for b in self.list_brands(tenant_code) if b.active)
        self._cache[tenant_code] = codes
        return codes

    def invalidate(self, tenant_code: str) -> None:
        """Drop the cached brand set for one tenant."""
        self._cache.pop(tenant_code, None)

    def validate_record_brand(self, tenant_code: str, brand_code: str | None) -> str | None:
        """
        Validate a record's brand against the registry.

        A None brand is permitted (a single-brand tenant has no brand dimension), but an
        unrecognised brand is an error — silently accepting it would make brand-level row
        security filter on a value nobody governs.
        """
        if brand_code in (None, ""):
            return None
        code = validate_brand_code(str(brand_code))
        known = self.known_brand_codes(tenant_code)
        if known and code not in known:
            raise UnknownBrandError(
                f"Record carries brand_code {code!r}, which tenant {tenant_code!r} has not "
                f"registered. Registered brands: {sorted(known)}."
            )
        return code


def stamp_brand(record: dict[str, Any], brand_code: str | None) -> dict[str, Any]:
    """Attach the brand dimension to a curated record without mutating the input."""
    return {**record, BRAND_COLUMN: brand_code}
