"""
Spec-driven REST/report connector implementing `ConnectorInterface`.

Field discovery is sample-driven because none of the ten sources on the customer list
publishes a metadata endpoint the way Salesforce and NetSuite do: the connector reads one
page, unions the observed keys, and fingerprints them — so a new source field is picked up
without a code change, which is what `discover_queryable_fields` exists to guarantee.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

from connector_runtime.adapters.rest_api.rest_http_session import (
    RestHttpSession,
    RestResponse,
    RestSourceCredentialError,
    RestSourceObjectError,
    RestSourceRequestError,
    RestSourceThrottledError,
    RestSourceTransientError,
)
from connector_runtime.adapters.rest_api.rest_source_spec import (
    EntityShape,
    RestEntitySpec,
    RestSourceSpec,
)
from connector_runtime.interfaces.connector_interface import (
    ConnectorCapabilities,
    ConnectorInterface,
    DeterministicConnectorError,
    ExtractionErrorClassification,
    ExtractionRecord,
    FieldContract,
    FieldDescriptor,
    QueryContract,
    TransientConnectorError,
)
from connector_runtime.pagination import (
    SourcePage,
    SourceRequest,
    pagination_strategy_registry,
)
from connector_runtime.rate_limiting import RateLimitPolicy
from connector_runtime.source_capabilities import (
    SourceCapability,
    SourceCapabilityUnavailableError,
)
from contracts.entity_configuration_contract import FieldMode, LoadType
from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# Records read to infer the field set. One page is enough for a fingerprint and keeps
# discovery from consuming the rate-limit budget the extraction itself needs.
_DISCOVERY_SAMPLE_PAGES = 1


class RestApiConnector(ConnectorInterface):
    """One connector for every spec-declared REST or report source."""

    def __init__(
        self,
        spec: RestSourceSpec,
        entity_id: str,
        session: RestHttpSession,
        rate_limit_policy: RateLimitPolicy,
        connection_id: str | None = None,
    ) -> None:
        self._spec = spec
        self._entity = spec.entity(entity_id)
        self._session = session
        self._rate_limit = rate_limit_policy
        self._connection_id = connection_id or spec.source_id
        self.pages_fetched = 0

    # ── Capability declaration ────────────────────────────────────────────────

    def get_capability_declaration(self) -> ConnectorCapabilities:
        capabilities = self._spec.capabilities
        return ConnectorCapabilities(
            source_id=self._spec.source_id,
            supports_bulk_extraction=SourceCapability.BULK_EXPORT in capabilities,
            supports_incremental=SourceCapability.INCREMENTAL in capabilities,
            supports_full_load=True,
            supports_metadata_discovery=SourceCapability.SCHEMA_DISCOVERY in capabilities,
            max_concurrent_jobs=1,
            supported_field_modes=(FieldMode.ALL, FieldMode.INCLUDE_ONLY),
        )

    # ── Field discovery ───────────────────────────────────────────────────────

    def discover_queryable_fields(
        self,
        source_id: str,
        entity_id: str,
        field_mode: FieldMode,
        include_fields: list[str],
        exclude_fields: list[str],
    ) -> FieldContract:
        observed = self._sample_field_names()
        if field_mode is FieldMode.INCLUDE_ONLY:
            if not include_fields:
                raise RestSourceRequestError(
                    f"{source_id}/{entity_id}: field_mode is INCLUDE_ONLY with no include_fields."
                )
            selected = [name for name in include_fields if name not in set(exclude_fields)]
        else:
            selected = [name for name in observed if name not in set(exclude_fields)]

        fields = tuple(
            FieldDescriptor(
                name=name,
                # The raw layer stores every source value as a string; typing is the
                # curated layer's responsibility, so discovery does not guess types.
                data_type="string",
                is_nullable=True,
                is_queryable=True,
                is_custom=name.startswith(("custom_", "hs_custom_", "x_")),
            )
            for name in sorted(selected)
        )
        return FieldContract(
            source_id=source_id,
            entity_id=entity_id,
            fields=fields,
            discovery_timestamp=datetime.now(UTC),
            schema_fingerprint=FieldContract.compute_fingerprint(fields),
        )

    # ── Query build ───────────────────────────────────────────────────────────

    def build_extraction_query(
        self,
        field_contract: FieldContract,
        load_type: LoadType,
        watermark_field: str | None,
        watermark_lower: str | None,
        watermark_upper: str | None,
        extraction_window_days: int,
    ) -> QueryContract:
        parameters: dict[str, Any] = dict(self._entity.static_query_parameters)
        if self._entity.shape is EntityShape.REPORT:
            parameters["metrics"] = ",".join(self._entity.report_metrics)
            if self._entity.report_dimensions:
                parameters["dimensions"] = ",".join(self._entity.report_dimensions)
        elif field_contract.fields:
            parameters["properties"] = ",".join(f.name for f in field_contract.fields)

        if load_type is LoadType.INCREMENTAL:
            if watermark_lower:
                parameters[self._spec.watermark_lower_parameter] = watermark_lower
            if watermark_upper:
                parameters[self._spec.watermark_upper_parameter] = watermark_upper

        return QueryContract(
            source_id=self._spec.source_id,
            entity_id=self._entity.entity_id,
            # The endpoint path is the query text; values stay in query_parameters and are
            # bound by the HTTP layer, never interpolated (OWASP A03).
            query_text=self._entity.path,
            query_parameters=parameters,
            load_type=load_type,
            watermark_lower=watermark_lower,
            watermark_upper=watermark_upper,
            watermark_field=watermark_field or self._entity.watermark_field,
        )

    # ── Extraction ────────────────────────────────────────────────────────────

    def execute_extraction(
        self, query_contract: QueryContract, run_id: str
    ) -> Iterator[ExtractionRecord]:
        strategy_name = self._entity.pagination_strategy or self._spec.default_pagination_strategy
        strategy = pagination_strategy_registry.resolve(strategy_name, self._fetch_page)
        request = SourceRequest(
            entity_id=self._entity.entity_id,
            page_size=self._entity.page_size,
            query_parameters=query_contract.query_parameters,
            keyset_field=self._entity.keyset_field,
        )
        watermark_field = query_contract.watermark_field
        for page in strategy.pages(request):
            self.pages_fetched = strategy.pages_fetched
            record_platform_metric(
                PlatformMetric.PAGES_FETCHED,
                1.0,
                SourceId=self._spec.source_id,
                ConnectionId=self._connection_id,
            )
            for record in page.records:
                yield ExtractionRecord(
                    payload=record,
                    source_timestamp=(
                        str(record.get(watermark_field)) if watermark_field else None
                    ),
                )

    def _fetch_page(self, parameters: Mapping[str, Any]) -> SourcePage:
        working = dict(parameters)
        url = working.pop("url", None)
        path = str(url) if url else self._entity.path
        response = self._session.get(path, working)
        return SourcePage(
            records=response.records(self._entity.records_json_path),
            next_cursor=_next_cursor(response),
            next_link=None,
            headers=response.headers,
        )

    def _sample_field_names(self) -> list[str]:
        strategy = pagination_strategy_registry.resolve(
            self._entity.pagination_strategy or self._spec.default_pagination_strategy,
            self._fetch_page,
            max_pages=_DISCOVERY_SAMPLE_PAGES,
        )
        request = SourceRequest(
            entity_id=self._entity.entity_id,
            page_size=self._entity.page_size,
            query_parameters=dict(self._entity.static_query_parameters),
            keyset_field=self._entity.keyset_field,
        )
        names: list[str] = []
        seen: set[str] = set()
        for page in strategy.pages(request):
            for record in page.records:
                for key in record:
                    if key not in seen:
                        seen.add(key)
                        names.append(str(key))
            break
        if not names:
            # An entity with no rows yet still needs a stable, non-empty contract so the
            # drift evaluator has something to compare against next run.
            names = [self._entity.natural_key_field]
            if self._entity.watermark_field:
                names.append(self._entity.watermark_field)
        return names

    # ── Write-back (DL-CONN-02) ───────────────────────────────────────────────

    def write_back(self, records: list[dict[str, Any]], writeback_session: RestHttpSession) -> int:
        """
        Upsert records into the source by external id.

        Uses a separate session so write-back credentials are a distinct secret from read
        credentials — a read-only deployment cannot mutate a source (OWASP A02).
        """
        if not self._entity.supports_writeback:
            raise SourceCapabilityUnavailableError(
                f"Entity {self._entity.entity_id!r} of source {self._spec.source_id!r} declares "
                "no write-back path; write-back must be opt-in per entity (DL-CONN-02)."
            )
        external_id_field = str(self._entity.writeback_external_id_field)
        written = 0
        for record in records:
            external_id = record.get(external_id_field)
            if external_id in (None, ""):
                raise RestSourceRequestError(
                    f"Write-back record for {self._entity.entity_id!r} has no "
                    f"{external_id_field!r}; an upsert without an external id would create a "
                    "duplicate on every retry."
                )
            writeback_session.patch(
                f"{self._entity.writeback_path}/{external_id}",
                payload={k: v for k, v in record.items() if k != external_id_field},
            )
            written += 1
        return written

    # ── Error classification ──────────────────────────────────────────────────

    def classify_extraction_error(self, exc: Exception) -> ExtractionErrorClassification:
        if isinstance(exc, SourceCapabilityUnavailableError):
            # A vendor endpoint that does not exist yet is a configuration fact, not an outage.
            return ExtractionErrorClassification.DETERMINISTIC_INVALID_CONFIGURATION
        if isinstance(exc, TransientConnectorError | DeterministicConnectorError):
            return exc.classification
        return ExtractionErrorClassification.UNKNOWN

    def health_check(self) -> bool:
        """Structural check only — a live call would consume the source's rate-limit budget."""
        try:
            self.get_capability_declaration()
            self._spec.entity(self._entity.entity_id)
            return True
        except Exception:
            return False


def _next_cursor(response: RestResponse) -> str | None:
    """
    Extract a provider cursor from the common response shapes.

    HubSpot nests it under `paging.next.after`; several others return a flat field.
    """
    body = response.body
    if not isinstance(body, Mapping):
        return None
    paging = body.get("paging")
    if isinstance(paging, Mapping):
        nxt = paging.get("next")
        if isinstance(nxt, Mapping) and nxt.get("after"):
            return str(nxt["after"])
    for key in ("nextPageToken", "next_cursor", "nextCursor", "cursor"):
        if body.get(key):
            return str(body[key])
    return None


__all__ = [
    "RestApiConnector",
    "RestEntitySpec",
    "RestSourceCredentialError",
    "RestSourceObjectError",
    "RestSourceRequestError",
    "RestSourceThrottledError",
    "RestSourceTransientError",
]
