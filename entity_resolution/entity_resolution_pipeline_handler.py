"""
AWS Lambda handler for the entity resolution pipeline Step Functions task.

Receives the curated S3 prefix from the transformation stage, loads curated
records from ALL configured sources for the entity type (not just the
triggering source), merges them into a single candidate pool, runs match
clustering and survivorship, and writes golden records to the analytics layer.

Multi-source design: when a second source (e.g. NetSuite) is added for an
entity type, it is listed in _ENTITY_TYPE_SOURCES.  The handler discovers and
loads the latest curated partition for each source automatically.  Sources with
no data yet are skipped gracefully — the pipeline continues with the sources
that do have curated records.

Step Functions input schema (Parameters block in RunEntityResolution state):
  {
    "source_id":         str  — source_id from the triggering extraction run
    "entity_id":         str  — entity_id from the triggering extraction run
    "environment":       str  — "dev" | "staging" | "prod"
    "run_id":            str  — run_id produced by the extraction stage
    "curated_s3_prefix": str  — S3 prefix where curated Parquet was written
  }

Step Functions output schema (stored at $.entity_resolution):
  {
    "canonical_prefix":          str  — S3 prefix of written golden records
    "entity_type":               str  — resolved entity type
    "input_curated_record_count": int
    "golden_record_count":        int
    "cluster_count":              int
    "golden_date":                str  — YYYY-MM-DD
    "published_at":               str  — ISO-8601 UTC
  }

Required Lambda environment variables:
  AWS_REGION              — injected automatically by the Lambda runtime
  CURATED_S3_BUCKET       — bucket holding curated Parquet files and
                            entity resolution configs (entity-resolution/ prefix)
  ANALYTICS_S3_BUCKET     — bucket where golden records are written

Optional Lambda environment variables:
  GOVERNANCE_S3_BUCKET    — bucket for lineage records; lineage skipped if absent

Security (OWASP A03, A07, A09):
  - All event fields validated against stable identifier regex before use.
  - S3 bucket names sourced exclusively from Lambda env vars — never from the
    event — to prevent path injection (OWASP A03 / CWE-22).
  - _record_id is constructed server-side from validated source_id + pk values.
  - Golden record log output contains counts only — no field values (OWASP A09).
  - Lambda execution role is least-privilege entity_resolution_runtime_role.
"""

from __future__ import annotations

import io
import os
import re
from typing import Any, Final

import boto3
import pyarrow.parquet as pq

from contracts.identifier_policy import STABLE_ID_PATTERN as _STABLE_ID_PATTERN
from entity_resolution.canonical_record_publisher.canonical_record_publisher import (
    GoldenRecordPublicationError,
    GoldenRecordPublisher,
)
from entity_resolution.resolution_config.resolution_config_registry import (
    ResolutionConfigNotFoundError,
    ResolutionConfigRegistry,
)
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# ---------------------------------------------------------------------------
# Entity type registry
# ---------------------------------------------------------------------------

# Maps entity_id → canonical entity type used in analytics S3 paths and
# resolution config lookups.  New entity IDs must be added here when onboarded.
_ENTITY_ID_TO_TYPE: Final[dict[str, str]] = {
    "salesforce-account":    "company",
    "netsuite-customer":     "company",      # ready for NetSuite onboarding
    "sage-intacct-customer": "company",      # Sage Intacct AR customer
    "sage-x3-customer":      "company",      # Sage X3 business partner (customer)
    "salesforce-contact":    "person",
    "mysql-rds-contracts":   "contract",
    "sage-intacct-vendor":   "supplier",     # Sage Intacct AP vendor
    "sage-x3-supplier":      "supplier",     # Sage X3 business partner (supplier)
    "sage-intacct-arinvoice": "ar_invoice",  # Sage Intacct AR invoice
    "sage-intacct-apbill":   "ap_bill",      # Sage Intacct AP bill
}

# Maps entity_type → canonical primary-key field name in curated records.
# Used to construct the cross-source _record_id without ambiguity.
# All company sources must produce 'account_id' in their curated field mapping.
_ENTITY_TYPE_PK_FIELD: Final[dict[str, str]] = {
    "company":    "account_id",   # Salesforce Account, NetSuite Customer, Sage Intacct Customer,
                                  #   Sage X3 Customer — each map their native ID to account_id
    "person":     "contact_id",
    "contract":   "contract_id",
    "supplier":   "vendor_id",    # Sage Intacct Vendor, Sage X3 Supplier
    "ar_invoice": "invoice_id",   # Sage Intacct AR Invoice
    "ap_bill":    "bill_id",       # Sage Intacct AP Bill
}

# Maps entity_type → ordered list of (source_id, entity_id) pairs that
# contribute curated records to that entity type.
# Order determines which source's curated prefix is preferred for "other sources"
# S3 scanning — does not affect survivorship (that is policy-controlled).
_ENTITY_TYPE_SOURCES: Final[dict[str, list[tuple[str, str]]]] = {
    "company": [
        ("salesforce", "salesforce-account"),
        ("netsuite",   "netsuite-customer"),     # loaded only when curated data exists
        ("sage",       "sage-intacct-customer"), # loaded only when curated data exists
        ("sage",       "sage-x3-customer"),      # loaded only when curated data exists
    ],
    "person": [
        ("salesforce", "salesforce-contact"),
    ],
    "contract": [
        ("mysql-rds", "mysql-rds-contracts"),
    ],
    "supplier": [
        ("sage", "sage-intacct-vendor"),   # Intacct preferred for contact richness
        ("sage", "sage-x3-supplier"),
    ],
    "ar_invoice": [
        ("sage", "sage-intacct-arinvoice"),
    ],
    "ap_bill": [
        ("sage", "sage-intacct-apbill"),
    ],
}

# ---------------------------------------------------------------------------
# Validation constants (OWASP A03)
# ---------------------------------------------------------------------------

_REQUIRED_EVENT_FIELDS: Final[frozenset[str]] = frozenset(
    {"source_id", "entity_id", "environment", "run_id", "curated_s3_prefix"}
)
_KNOWN_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"dev", "staging", "prod"})
# Matches curated S3 prefixes produced by the transformation stage.
# Allows letters, digits, hyphens, underscores, slashes, and equals signs
# (needed for Hive partition paths like curated_date=2026-06-29).
_SAFE_S3_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9\-_/=\.]{0,511}$"
)

# ---------------------------------------------------------------------------
# Module-level singleton (warm invocation cache)
# ---------------------------------------------------------------------------

_registry: ResolutionConfigRegistry | None = None


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    AWS Lambda entry point for the entity resolution pipeline Step Functions task.

    Args:
        event:   Step Functions Parameters block — see module docstring.
        context: Lambda runtime context (unused).

    Returns:
        Dict matching the Step Functions output schema (stored at $.entity_resolution).

    Raises:
        ValueError:    Input validation failure.
        RuntimeError:  Required environment variable absent at startup.
        Exception:     Any pipeline failure propagates to Step Functions for
                       retry / catch handling.
    """
    _validate_event(event)

    source_id: str = event["source_id"]
    entity_id: str = event["entity_id"]
    environment: str = event["environment"]
    run_id: str = event["run_id"]
    curated_s3_prefix: str = event["curated_s3_prefix"]

    # ── Env vars ─────────────────────────────────────────────────────────────
    region_name = _require_env("AWS_REGION")
    curated_s3_bucket = _require_env("CURATED_S3_BUCKET")
    analytics_s3_bucket = _require_env("ANALYTICS_S3_BUCKET")
    governance_s3_bucket: str | None = os.environ.get("GOVERNANCE_S3_BUCKET") or None

    # ── Resolve entity type ───────────────────────────────────────────────────
    entity_type = _ENTITY_ID_TO_TYPE.get(entity_id)
    if entity_type is None:
        raise ValueError(
            f"No entity type mapping found for entity_id={entity_id!r}. "
            "Add it to _ENTITY_ID_TO_TYPE in entity_resolution_pipeline_handler.py."
        )
    pk_field = _ENTITY_TYPE_PK_FIELD[entity_type]

    _logger.info(
        "entity_resolution_handler_invoked",
        source_id=source_id,
        entity_id=entity_id,
        entity_type=entity_type,
        environment=environment,
        run_id=run_id,
        pk_field=pk_field,
    )

    s3 = boto3.client("s3", region_name=region_name)

    # ── Load curated records from all contributing sources ────────────────────
    # The triggering source's records come from the exact prefix Step Functions
    # passed in.  All other configured sources are located by scanning the
    # curated bucket for their latest partition — skipped gracefully if absent.
    all_curated_records: list[dict[str, Any]] = []
    loaded_prefixes: list[str] = []

    for contrib_source_id, contrib_entity_id in _ENTITY_TYPE_SOURCES.get(entity_type, []):
        contrib_domain = _source_id_to_domain(contrib_source_id)

        if contrib_source_id == source_id and contrib_entity_id == entity_id:
            # Current run — load from the exact prefix passed in by Step Functions.
            prefix = curated_s3_prefix
        else:
            # Other source — find the latest curated partition in the bucket.
            prefix = _find_latest_curated_prefix(
                s3, curated_s3_bucket, contrib_domain, contrib_entity_id
            )
            if prefix is None:
                _logger.info(
                    "entity_resolution_source_skipped_no_data",
                    contrib_source_id=contrib_source_id,
                    contrib_entity_id=contrib_entity_id,
                )
                continue

        records = _load_curated_records(s3, curated_s3_bucket, prefix)
        _logger.info(
            "entity_resolution_source_loaded",
            contrib_source_id=contrib_source_id,
            contrib_entity_id=contrib_entity_id,
            record_count=len(records),
        )

        # Tag each record with a unified cross-source identifier and source label.
        # _record_id is constructed server-side; never derived from event input.
        for rec in records:
            pk_value = str(rec.get(pk_field, ""))
            rec["_record_id"] = f"{contrib_source_id}:{pk_value}"
            rec["_source_id"] = contrib_source_id

        all_curated_records.extend(records)
        loaded_prefixes.append(prefix)

    if not all_curated_records:
        raise GoldenRecordPublicationError(
            f"No curated records found for entity_type={entity_type!r}. "
            "Ensure the transformation stage completed successfully."
        )

    # ── Load resolution config + build publisher ──────────────────────────────
    global _registry  # noqa: PLW0603  — module-level warm-invocation cache
    if _registry is None:
        _registry = ResolutionConfigRegistry(
            s3_bucket=curated_s3_bucket,
            region_name=region_name,
        )

    try:
        publisher = GoldenRecordPublisher.from_registry(
            registry=_registry,
            entity_type=entity_type,
            analytics_s3_bucket=analytics_s3_bucket,
            region_name=region_name,
            governance_s3_bucket=governance_s3_bucket,
            curated_s3_bucket=curated_s3_bucket,
            curated_s3_prefixes=tuple(loaded_prefixes),
        )
    except ResolutionConfigNotFoundError as exc:
        raise ResolutionConfigNotFoundError(
            f"Resolution config not found for entity_type={entity_type!r}. "
            "Upload match_rules and survivorship JSON files to S3 via "
            "scripts/seed_entity_resolution_configs.py, then retry."
        ) from exc

    # ── Run golden record publication ─────────────────────────────────────────
    result = publisher.publish(
        curated_records=all_curated_records,
        entity_type=entity_type,
        match_run_id=run_id,
        id_field="_record_id",    # unified cross-source identifier
        source_field="_source_id",
    )

    _logger.info(
        "entity_resolution_complete",
        entity_type=entity_type,
        run_id=run_id,
        input_record_count=result.input_curated_record_count,
        golden_record_count=result.golden_record_count,
        cluster_count=result.cluster_count,
        analytics_s3_prefix=result.analytics_s3_prefix,
    )

    return {
        "canonical_prefix":           result.analytics_s3_prefix,
        "entity_type":                result.entity_type,
        "input_curated_record_count": result.input_curated_record_count,
        "golden_record_count":        result.golden_record_count,
        "cluster_count":              result.cluster_count,
        "golden_date":                result.golden_date,
        "published_at":               result.published_at,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source_id_to_domain(source_id: str) -> str:
    """Convert source_id to S3/Glue-safe domain string.

    "mysql-rds" → "mysql_rds", "salesforce" → "salesforce", etc.
    Mirrors the same function in transformation_pipeline_handler.py.
    """
    return source_id.replace("-", "_")


def _find_latest_curated_prefix(
    s3: Any, bucket: str, domain: str, entity_id: str
) -> str | None:
    """
    Scan the curated bucket for the most recent partition for (domain, entity_id).

    Curated path structure:
      curated/{domain}/{entity_id}/curated_date={YYYY-MM-DD}/run_id={run_id}/

    Returns the full prefix string (with trailing slash) for the latest run, or
    None if no curated data exists for this source yet.
    """
    base_prefix = f"curated/{domain}/{entity_id}/"
    paginator = s3.get_paginator("list_objects_v2")
    # Collect all curated_date= partition prefixes using the delimiter trick.
    date_prefixes: list[str] = []
    for page in paginator.paginate(
        Bucket=bucket, Prefix=base_prefix, Delimiter="/"
    ):
        for cp in page.get("CommonPrefixes", []):
            pfx: str = cp["Prefix"]
            if "curated_date=" in pfx:
                date_prefixes.append(pfx)

    if not date_prefixes:
        return None

    # Latest date partition — ISO format sorts lexicographically.
    latest_date_prefix = sorted(date_prefixes)[-1]

    # Within the date partition, find the latest run_id sub-prefix.
    run_prefixes: list[str] = []
    for page in paginator.paginate(
        Bucket=bucket, Prefix=latest_date_prefix, Delimiter="/"
    ):
        for cp in page.get("CommonPrefixes", []):
            pfx = cp["Prefix"]
            if "run_id=" in pfx:
                run_prefixes.append(pfx)

    if not run_prefixes:
        return None

    return sorted(run_prefixes)[-1]


def _load_curated_records(
    s3: Any, bucket: str, prefix: str
) -> list[dict[str, Any]]:
    """
    Load all Parquet files from a curated S3 prefix into a list of dicts.

    Validates prefix for path traversal before use (OWASP A03 / CWE-22).
    """
    clean_prefix = prefix.strip()
    if ".." in clean_prefix or clean_prefix.startswith("/"):
        raise ValueError(f"Unsafe curated_s3_prefix rejected: {clean_prefix!r}")
    if not _SAFE_S3_PREFIX_PATTERN.match(clean_prefix.rstrip("/")):
        raise ValueError(
            f"curated_s3_prefix {clean_prefix!r} contains disallowed characters."
        )

    paginator = s3.get_paginator("list_objects_v2")
    records: list[dict[str, Any]] = []

    for page in paginator.paginate(Bucket=bucket, Prefix=clean_prefix.rstrip("/") + "/"):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".parquet"):
                continue
            raw = s3.get_object(Bucket=bucket, Key=obj["Key"])
            buf = io.BytesIO(raw["Body"].read())
            table = pq.read_table(buf)  # type: ignore[no-untyped-call]
            records.extend(table.to_pylist())
            del table  # release memory before next file

    return records


def _require_env(name: str) -> str:
    """Return environment variable value or raise RuntimeError if absent."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            "Configure it in the Lambda function's environment variables."
        )
    return value


def _validate_event(event: dict[str, Any]) -> None:
    """
    Validate the Step Functions event payload.

    Raises ValueError for missing fields, unknown environments, or field values
    that fail the stable-identifier pattern (OWASP A03).
    """
    missing = _REQUIRED_EVENT_FIELDS - set(event.keys())
    if missing:
        raise ValueError(f"Missing required event fields: {sorted(missing)}")

    for field in ("source_id", "entity_id", "run_id"):
        value = str(event[field])
        if not _STABLE_ID_PATTERN.match(value):
            raise ValueError(
                f"Event field {field}={value!r} contains disallowed characters. "
                "Expected lowercase alphanumeric with hyphens (max 64 chars)."
            )

    environment = str(event["environment"])
    if environment not in _KNOWN_ENVIRONMENTS:
        raise ValueError(
            f"Unknown environment={environment!r}. "
            f"Expected one of {sorted(_KNOWN_ENVIRONMENTS)}."
        )

    curated_prefix = str(event["curated_s3_prefix"])
    if not _SAFE_S3_PREFIX_PATTERN.match(curated_prefix.rstrip("/")):
        raise ValueError(
            f"curated_s3_prefix={curated_prefix!r} contains disallowed characters."
        )
