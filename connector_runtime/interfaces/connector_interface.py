"""
Connector interface contract for the Enterprise Data Lake platform.

Every source adapter MUST implement ConnectorInterface. The connector runtime
invokes these methods in sequence for each entity extraction run.

Design principles enforced by this interface:
  1. Credentials: obtained exclusively from the injected secrets client —
     never from environment variables, constructor arguments, or config files.
  2. Logging: no credentials, tokens, or PII values may appear in any log
     or exception message emitted by an adapter.
  3. Idempotency: execute_extraction() must be safe to replay without
     producing duplicate records or advancing watermarks.
  4. Error taxonomy: classify_extraction_error() drives the reliability
     framework's retry-vs-fail-fast decisions. Incorrect classification
     causes either unnecessary retries or missed recovery opportunities.
  5. No hardcoded field lists: adapters must discover fields at runtime
     via discover_queryable_fields().

Prohibited patterns in adapter implementations:
  - Object-specific extractor subclasses (e.g. AccountExtractor, ContactExtractor)
  - Hardcoded SOQL or SQL field lists
  - Direct os.environ access for credentials
  - Catching all exceptions silently (bare except: pass)
"""

from __future__ import annotations

import abc
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

# Import canonical enums from contracts — never redefine them in adapter code.
# A single definition prevents enum values diverging between contract validation
# and runtime connector behaviour.
from contracts.entity_configuration_contract import FieldMode, LoadType


class ExtractionErrorClassification(StrEnum):
    """
    Error classification taxonomy for the reliability framework.

    TRANSIENT errors are retry-eligible (exponential backoff with jitter).
    DETERMINISTIC errors trigger immediate fail-fast with DLQ routing.

    Implementors MUST classify errors accurately — the reliability framework
    relies on this taxonomy to decide retry vs fail-fast behaviour.
    """

    # Retry-eligible — transient infrastructure issues
    TRANSIENT_TIMEOUT = "transient_timeout"
    TRANSIENT_THROTTLE = "transient_throttle"
    TRANSIENT_NETWORK = "transient_network"

    # Fail-fast — configuration or credential problems that retrying cannot fix
    DETERMINISTIC_INVALID_CREDENTIALS = "deterministic_invalid_credentials"
    DETERMINISTIC_INVALID_OBJECT = "deterministic_invalid_object"
    DETERMINISTIC_INVALID_CONFIGURATION = "deterministic_invalid_configuration"
    DETERMINISTIC_SCHEMA_VIOLATION = "deterministic_schema_violation"

    # Used when the error cannot be reliably classified — routes to DLQ with manual review
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldDescriptor:
    """
    Describes a single queryable field discovered from source metadata.

    Frozen: field descriptors are immutable once discovered. A new discovery
    run produces a new FieldContract, not a mutation of an existing one.
    """

    name: str
    data_type: str
    is_nullable: bool
    is_queryable: bool
    length: int | None = None
    precision: int | None = None
    scale: int | None = None
    is_custom: bool = False
    source_label: str | None = None


@dataclass(frozen=True)
class FieldContract:
    """
    Output of metadata discovery: the complete set of queryable fields
    for a given entity, respecting field_mode configuration.

    The schema_fingerprint is a deterministic hash of the field set and is
    used by the schema drift evaluator to detect changes between runs.
    """

    source_id: str
    entity_id: str
    fields: tuple[FieldDescriptor, ...]
    discovery_timestamp: datetime  # UTC datetime of this discovery run
    schema_fingerprint: str  # SHA-256 hex of sorted field names + types

    @classmethod
    def compute_fingerprint(cls, fields: tuple[FieldDescriptor, ...]) -> str:
        """
        Compute a deterministic fingerprint for the given field set.

        The fingerprint changes when:
          - A field is added or removed
          - A field's data_type changes
          - A field's precision, scale, or length changes

        The fingerprint does NOT change for label or description changes.
        """
        canonical = sorted(
            (
                {
                    "name": f.name,
                    "type": f.data_type,
                    "len": f.length,
                    "prec": f.precision,
                    "scale": f.scale,
                }
                for f in fields
            ),
            key=lambda d: str(d["name"]),
        )
        return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class QueryContract:
    """
    Output of the query builder: a fully-formed, parameterized extraction query.

    Security requirement: query_text must use parameter placeholders (e.g. :param
    for SOQL, %s for MySQL). Values are NEVER interpolated into query_text strings.
    The runtime binds query_parameters separately to prevent injection.
    """

    source_id: str
    entity_id: str
    query_text: str  # Parameterized query — NO values interpolated
    query_parameters: dict[str, Any]  # Bound parameter values (passed separately)
    load_type: LoadType
    watermark_lower: str | None  # ISO8601 UTC
    watermark_upper: str | None  # ISO8601 UTC
    watermark_field: str | None = None  # Source field name used for watermark filtering
    estimated_record_count: int | None = None


@dataclass
class ExtractionRecord:
    """
    A single record returned by the extraction engine.

    payload: the raw source field values exactly as returned — no transformation.
    source_timestamp: the ISO8601 value of the watermark field for this record,
                      used by the runtime to track extraction progress.
    """

    payload: dict[str, Any]
    source_timestamp: str | None = None  # ISO8601 value of the watermark field


@dataclass
class ConnectorCapabilities:
    """
    Capability declaration for a connector adapter.

    The runtime uses this declaration to select extraction strategies:
      - bulk_threshold_records: switch to bulk extraction above this count
      - supports_bulk_extraction: False means REST/query API only
      - supported_field_modes: modes the adapter can honour

    Adapters register themselves via ConnectorRegistry at startup.
    """

    source_id: str
    supports_bulk_extraction: bool = False
    supports_incremental: bool = True
    supports_full_load: bool = True
    supports_metadata_discovery: bool = True
    bulk_threshold_records: int = 2_000
    max_concurrent_jobs: int = 1
    supported_field_modes: tuple[FieldMode, ...] = field(default_factory=lambda: (FieldMode.ALL,))


# ---------------------------------------------------------------------------
# Abstract connector interface
# ---------------------------------------------------------------------------


class ConnectorInterface(abc.ABC):
    """
    Abstract base contract for all source connector adapters.

    Adapters MUST:
      - Retrieve credentials exclusively from the injected secrets client.
      - Never log credentials, tokens, or PII values.
      - Implement idempotent extraction safe for replay.
      - Classify errors accurately via classify_extraction_error().
      - Discover fields dynamically — no hardcoded field lists.

    Adapters MUST NOT:
      - Create object-specific subclasses (e.g. AccountConnector).
      - Access os.environ or constructor args for credentials.
      - Silently swallow exceptions.
    """

    @abc.abstractmethod
    def get_capability_declaration(self) -> ConnectorCapabilities:
        """
        Return the connector's capability declaration.
        Called once at runtime startup to register the adapter.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def discover_queryable_fields(
        self,
        source_id: str,
        entity_id: str,
        field_mode: FieldMode,
        include_fields: list[str],
        exclude_fields: list[str],
    ) -> FieldContract:
        """
        Discover all queryable fields for the entity from source metadata.

        Requirements:
          - Must filter non-queryable fields automatically.
          - Must not require code changes when new fields are added to the source.
          - Must respect field_mode: ALL, STANDARD, CUSTOM, or INCLUDE_ONLY.
          - Must apply exclude_fields regardless of field_mode.

        Returns:
            FieldContract with the complete set of queryable fields and
            a deterministic schema_fingerprint for drift detection.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def build_extraction_query(
        self,
        field_contract: FieldContract,
        load_type: LoadType,
        watermark_field: str | None,
        watermark_lower: str | None,
        watermark_upper: str | None,
        extraction_window_days: int,
    ) -> QueryContract:
        """
        Build a parameterized extraction query from the field contract.

        Requirements:
          - query_text must use parameter placeholders — never interpolate values.
          - Incremental queries must apply watermark bounds as parameters.
          - Full load queries must handle pagination or chunking if required.

        Returns:
            QueryContract with parameterized query_text and bound query_parameters.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def execute_extraction(
        self,
        query_contract: QueryContract,
        run_id: str,
    ) -> Iterator[ExtractionRecord]:
        """
        Execute the extraction and yield records as a lazy stream.

        Requirements:
          - Must be safe to replay without producing duplicate raw records.
          - Must not advance watermarks — the orchestration layer does that.
          - Must yield records in source order where the source supports it.
          - Must emit progress logs using the platform structured logger.

        Yields:
            ExtractionRecord instances, one per source record.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def classify_extraction_error(
        self,
        exc: Exception,
    ) -> ExtractionErrorClassification:
        """
        Classify an exception as transient (retry) or deterministic (fail-fast).

        This classification drives the reliability framework's retry decisions:
          - TRANSIENT_* → exponential backoff retry (up to configured limit)
          - DETERMINISTIC_* → immediate fail-fast, DLQ routing, no retry
          - UNKNOWN → DLQ routing with manual review flag

        Requirements:
          - Classification must be based on exception type and content.
          - Must not log the exception here — caller handles logging.
          - Must never raise itself.
        """
        raise NotImplementedError
