"""
Analytics-layer partition locator.

Finds the latest ``analytics_date=`` partition an entity type was published to,
so downstream readers (twin build, semantic queries) target current-state golden
records rather than every historical daily snapshot. Shared by the twin-build
handler and the control-plane query endpoints (REU — one locator, not two).
"""

from __future__ import annotations

from typing import Any


def latest_partition_uri(s3: Any, bucket: str, tenant_code: str, entity_type: str) -> str | None:
    """Return the s3:// URI of the latest analytics partition, or None if none exist."""
    prefix = f"{tenant_code}/analytics/{entity_type}/"
    partitions: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=prefix, Delimiter="/"
    ):
        for common in page.get("CommonPrefixes", []):
            candidate = str(common.get("Prefix", ""))
            if "analytics_date=" in candidate:
                partitions.append(candidate)
    if not partitions:
        return None
    return f"s3://{bucket}/{max(partitions).rstrip('/')}"
