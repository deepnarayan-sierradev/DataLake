"""
Salesforce connector adapter.

Implements ConnectorInterface for Salesforce as the single, metadata-driven
adapter for all Salesforce entities.  No object-specific subclasses.

Design enforced by spec:
  - All fields discovered at runtime via Describe API — no hardcoded lists.
  - Single connector class handles all Salesforce objects (Account, Contact,
    Opportunity, and any future objects) through configuration only.
  - Registers itself with the platform ConnectorRegistry at import time.
  - Bulk API 2.0 used when estimated record volume ≥ 2,000.

Credentials:
  - Fetched from AWS Secrets Manager at first token request.
  - Never in constructor arguments, env vars, or logs.

Security (OWASP A05, A07, A09):
  - SOQL is built from validated, discovered field names only.
  - Watermark values bound as parameters — never interpolated by hand.
  - Token absent from all log events and exception messages.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Final

from connector_runtime.adapters.salesforce.salesforce_auth_client import (
    SalesforceAuthClient,
    SalesforceAuthError,
    SalesforceCredentialError,
)
from connector_runtime.adapters.salesforce.salesforce_bulk_query_job_controller import (
    BulkApiLimitError,
    BulkJobFailedError,
    BulkJobTimeoutError,
    SalesforceBulkQueryJobController,
)
from connector_runtime.adapters.salesforce.salesforce_metadata_discovery_client import (
    SalesforceMetadataDiscoveryClient,
)
from connector_runtime.interfaces.connector_interface import (
    ConnectorCapabilities,
    ConnectorInterface,
    ExtractionErrorClassification,
    ExtractionRecord,
    FieldContract,
    QueryContract,
)
from connector_runtime.query_builders.salesforce_soql_query_builder import (
    SalesforceSoqlQueryBuilder,
    SalesforceSoqlQueryBuilderError,
)
from connector_runtime.registry import connector_registry
from contracts.entity_configuration_contract import FieldMode, LoadType
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# Salesforce object name is derived from the entity_id by convention:
# entity_id "salesforce-account" → object "Account"
# The mapping is one-directional: entity_id is the platform identifier,
# object_name is the Salesforce API name stored in entity configuration.
_SOURCE_ID: Final[str] = "salesforce"


@connector_registry.register(_SOURCE_ID)
class SalesforceConnector(ConnectorInterface):
    """
    Metadata-driven Salesforce connector for all Salesforce entities.

    One instance per extraction run, constructed by the connector runtime
    after loading EntityExtractionConfig.  The Salesforce object name is
    provided as a constructor argument (from entity config) rather than
    being hardcoded or derived by convention — this keeps the mapping
    explicit and auditable.

    Constructor args are NOT used for credentials — those come exclusively
    from AWS Secrets Manager via SalesforceAuthClient.
    """

    def __init__(
        self,
        environment: str,
        region_name: str,
        object_name: str,
        max_bulk_poll_seconds: float = 1800.0,
    ) -> None:
        if not object_name:
            raise ValueError("object_name must not be empty.")
        self._object_name = object_name
        self._auth = SalesforceAuthClient(
            environment=environment,
            region_name=region_name,
        )
        self._max_bulk_poll_seconds = max_bulk_poll_seconds

    def get_capability_declaration(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            source_id=_SOURCE_ID,
            supports_bulk_extraction=True,
            supports_incremental=True,
            supports_full_load=True,
            supports_metadata_discovery=True,
            bulk_threshold_records=2_000,
            max_concurrent_jobs=1,
            supported_field_modes=(
                FieldMode.ALL,
                FieldMode.STANDARD,
                FieldMode.CUSTOM,
                FieldMode.INCLUDE_ONLY,
            ),
        )

    def discover_queryable_fields(
        self,
        source_id: str,
        entity_id: str,
        field_mode: FieldMode,
        include_fields: list[str],
        exclude_fields: list[str],
    ) -> FieldContract:
        """
        Discover all queryable fields for this entity via the Salesforce Describe API.

        New fields added to the Salesforce object appear automatically in the
        next run without any code changes.
        """
        client = SalesforceMetadataDiscoveryClient(
            auth_client=self._auth,
            object_name=self._object_name,
        )
        return client.discover_fields(
            source_id=source_id,
            entity_id=entity_id,
            field_mode=field_mode,
            include_fields=include_fields,
            exclude_fields=exclude_fields,
        )

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
        Build a parameterized SOQL query from the discovered FieldContract.

        Watermark bounds are passed as query_parameters — never interpolated
        into query_text.  The Bulk API controller binds them at submission.
        """
        builder = SalesforceSoqlQueryBuilder(object_name=self._object_name)
        return builder.build(
            field_contract=field_contract,
            load_type=load_type,
            watermark_field=watermark_field,
            watermark_lower=watermark_lower,
            watermark_upper=watermark_upper,
            extraction_window_days=extraction_window_days,
        )

    def execute_extraction(
        self,
        query_contract: QueryContract,
        run_id: str,
    ) -> Iterator[ExtractionRecord]:
        """
        Execute extraction via Salesforce Bulk API 2.0 and yield records.

        Uses the Bulk API path unconditionally for this implementation —
        the capability declaration sets bulk_threshold_records=2000 so the
        runtime selects this path when estimated_record_count >= 2000.
        For smaller estimated counts the same path is used (Bulk API 2.0
        handles small queries efficiently).

        The watermark field value is extracted from each record's payload
        and set as ExtractionRecord.source_timestamp.
        """
        _logger.info(
            "salesforce_extraction_started",
            source_id=query_contract.source_id,
            entity_id=query_contract.entity_id,
            run_id=run_id,
            load_type=str(query_contract.load_type),
            object_name=self._object_name,
        )

        controller = SalesforceBulkQueryJobController(
            auth_client=self._auth,
            max_poll_seconds=self._max_bulk_poll_seconds,
        )

        record_count = 0
        for record in controller.execute(
            soql=query_contract.query_text,
            query_parameters=query_contract.query_parameters,
        ):
            record_count += 1
            # Populate source_timestamp from the watermark field in the record payload.
            # query_contract.watermark_field is set by the SOQL builder for INCREMENTAL runs.
            if query_contract.watermark_field and query_contract.watermark_field in record.payload:
                record.source_timestamp = record.payload[query_contract.watermark_field]
            yield record

        _logger.info(
            "salesforce_extraction_completed",
            source_id=query_contract.source_id,
            entity_id=query_contract.entity_id,
            run_id=run_id,
            record_count=record_count,
        )

    def classify_extraction_error(
        self,
        exc: Exception,
    ) -> ExtractionErrorClassification:
        """
        Classify a Salesforce extraction exception for the retry framework.

        Deterministic failures are not retried — they indicate configuration
        or credential problems that a retry cannot fix.
        Transient failures are eligible for exponential backoff retry.
        """
        if isinstance(exc, SalesforceCredentialError):
            return ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS
        if isinstance(exc, SalesforceSoqlQueryBuilderError):
            return ExtractionErrorClassification.DETERMINISTIC_INVALID_CONFIGURATION
        if isinstance(exc, SalesforceAuthError):
            # Auth errors are deterministic — retrying with the same (broken)
            # credentials won't help.
            return ExtractionErrorClassification.DETERMINISTIC_INVALID_CREDENTIALS
        if isinstance(exc, BulkApiLimitError):
            # API limit exhaustion is transient — quota resets daily.
            return ExtractionErrorClassification.TRANSIENT_THROTTLE
        if isinstance(exc, BulkJobTimeoutError):
            # Timeout may be transient (large job, network slowness).
            return ExtractionErrorClassification.TRANSIENT_TIMEOUT
        if isinstance(exc, BulkJobFailedError):
            # Salesforce-side job failure — could be transient or deterministic;
            # classify as unknown to route to DLQ for manual review.
            return ExtractionErrorClassification.UNKNOWN
        if isinstance(exc, OSError):
            return ExtractionErrorClassification.TRANSIENT_NETWORK
        return ExtractionErrorClassification.UNKNOWN


# ---------------------------------------------------------------------------
# Connector builder — registered with the platform ConnectorRegistry so the
# extraction pipeline Lambda can fully wire this connector + raw-layer writer
# from the Step Functions execution input without hardcoded source dispatch.
# ---------------------------------------------------------------------------


def _build_salesforce(
    environment: str,
    region_name: str,
    connector_params: dict[str, str],
    raw_s3_bucket: str,
) -> tuple[ConnectorInterface, Any]:
    """
    Factory used by the extraction pipeline Lambda to construct a fully-wired
    SalesforceConnector and SalesforceRawLayerWriter from the Step Functions
    execution input.

    Required connector_params key:
      object_name (str) — Salesforce API object name (e.g. 'Account', 'Contact').
    """
    from connector_runtime.adapters.salesforce.salesforce_raw_layer_writer import (
        SalesforceRawLayerWriter,
    )

    object_name = connector_params.get("object_name", "")
    if not object_name:
        raise ValueError(
            "connector_params must include 'object_name' for source_id='salesforce'. "
            "Example: {'object_name': 'Account'}."
        )
    connector = SalesforceConnector(
        environment=environment,
        region_name=region_name,
        object_name=object_name,
    )
    writer = SalesforceRawLayerWriter(
        s3_bucket=raw_s3_bucket,
        s3_prefix="salesforce",
        region_name=region_name,
    )
    return connector, writer


connector_registry.register_builder(_SOURCE_ID, _build_salesforce)
