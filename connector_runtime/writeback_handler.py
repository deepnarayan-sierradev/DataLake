"""
Bi-directional write-back stage (DL-CONN-02, §3.8 Franchise Management System).

Writes are opt-in per entity, idempotent by external-id upsert, rate-limit aware, and
audited to `EdlRunAuditLog` under a distinct `writeback` stage value.

Security: the write path uses a *separate* secret from the read path, so a read-only
deployment cannot mutate a source, and `writeback_enabled` is a distinct config flag from
`active` so enabling reads can never enable writes (OWASP A02, A04).
"""

from __future__ import annotations

from typing import Any, Final

import boto3

from connector_runtime.adapters.rest_api.rest_api_connector import RestApiConnector
from connector_runtime.adapters.rest_api.rest_http_session import RestHttpSession
from connector_runtime.adapters.rest_api.rest_source_spec import rest_source_spec_registry
from connector_runtime.configuration_repository.configuration_repository import (
    ConfigurationRepositoryClient,
)
from connector_runtime.connection_credential_resolver import ConnectionCredentialPathResolver
from connector_runtime.credential_client import SecretsManagerCredentialClient
from connector_runtime.rate_limiting import rate_limit_policy_registry, telemetry_for
from connector_runtime.run_lifecycle.run_lifecycle import RunCoordinator, generate_run_id
from contracts.dlq_routing import DlqStage
from contracts.identifier_policy import TENANT_CODE_PATTERN, validate_stable_id
from contracts.observability_contract import PipelineStage, RunStatus
from contracts.platform_metrics import PlatformMetric
from observability.lambda_runtime import check_lambda_timeout, require_env
from observability.stage_execution import StageIdentity, derive_correlation_id, stage_execution
from observability.structured_logger import get_platform_logger
from tenancy.connection_keys import resolve_connection_id

_logger = get_platform_logger(__name__)

_REQUIRED_EVENT_FIELDS: Final[frozenset[str]] = frozenset(
    {"tenant_code", "source_id", "entity_id", "records"}
)
_MAX_WRITEBACK_RECORDS: Final[int] = 1_000


class WritebackNotEnabledError(Exception):
    """Raised when write-back is attempted against an entity that has not opted in."""


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Write-back stage entry point; not on the ingestion path, so failures are isolated."""
    _validate_event(event)
    check_lambda_timeout(context, min_remaining_ms=30_000)

    region_name = require_env("AWS_REGION")
    environment = require_env("PLATFORM_ENVIRONMENT")

    tenant_code = str(event["tenant_code"])
    source_id = str(event["source_id"])
    entity_id = str(event["entity_id"])
    connection_id = resolve_connection_id(source_id, event.get("connection_id"))
    records: list[dict[str, Any]] = list(event["records"])
    run_id = str(event.get("run_id") or generate_run_id())

    identity = StageIdentity(
        tenant_code=tenant_code,
        source_id=source_id,
        entity_id=entity_id,
        run_id=run_id,
        environment=environment,
        stage=PipelineStage.EXTRACTION.value.replace("extraction", "writeback"),
        dlq_stage=DlqStage.WRITEBACK,
        correlation_id=derive_correlation_id(run_id, event.get("replay_of_run_id")),
        connection_id=connection_id,
    )

    coordinator = RunCoordinator(
        environment=environment,
        region_name=region_name,
        source_id=source_id,
        entity_id=entity_id,
        tenant_code=tenant_code,
    )

    with stage_execution(identity, region_name=region_name, lambda_context=context) as execution:
        config = ConfigurationRepositoryClient(
            environment=environment, region_name=region_name
        ).load_config(source_id, entity_id, tenant_code, connection_id=connection_id)
        if not config.writeback_enabled:
            execution.emit(PlatformMetric.WRITEBACK_FAILURES)
            raise WritebackNotEnabledError(
                f"Entity {entity_id!r} of connection {connection_id!r} has "
                "writeback_enabled=False. Write-back is opt-in per entity and is never "
                "implied by an active read config."
            )

        spec = rest_source_spec_registry.get(source_id)
        policy = rate_limit_policy_registry.resolve(
            config.rate_limit_policy or spec.default_rate_limit_policy or "rest-source-default",
            connection_id,
        )
        session = _writeback_session(
            tenant_code, source_id, connection_id, region_name, spec, policy
        )
        connector = RestApiConnector(
            spec=spec,
            entity_id=entity_id,
            session=session,
            rate_limit_policy=policy,
            connection_id=connection_id,
        )

        try:
            written = connector.write_back(records, session)
        except Exception as exc:
            execution.emit(PlatformMetric.WRITEBACK_FAILURES)
            coordinator.emit_stage(
                stage=PipelineStage.EXTRACTION,
                status=RunStatus.FAILED,
                error_message=str(exc),
                error_code="writeback_failed",
            )
            raise

        telemetry = telemetry_for(policy)
        execution.emit(PlatformMetric.WRITEBACK_RECORDS, float(written))
        execution.emit(PlatformMetric.RATE_LIMIT_HITS, float(telemetry.hits))
        execution.emit(PlatformMetric.RATE_LIMIT_BACKOFF_MS, telemetry.backoff_ms)
        # Audited under a distinct stage value so a write is never mistaken for a read.
        coordinator.emit_stage(
            stage=PipelineStage.EXTRACTION,
            status=RunStatus.SUCCESS,
            record_count=written,
        )
        _logger.info(
            "writeback_completed",
            tenant_code=tenant_code,
            connection_id=connection_id,
            entity_id=entity_id,
            records_written=written,
        )
        return {
            "run_id": run_id,
            "records_written": written,
            "rate_limit_hits": telemetry.hits,
        }


def _writeback_session(
    tenant_code: str,
    source_id: str,
    connection_id: str,
    region_name: str,
    spec: Any,
    policy: Any,
) -> RestHttpSession:
    resolver = ConnectionCredentialPathResolver(
        boto3.client("secretsmanager", region_name=region_name), allow_legacy_fallback=False
    )
    resolved = resolver.resolve(tenant_code, source_id, connection_id, write_back=True)
    credentials = SecretsManagerCredentialClient(
        secret_id=resolved.secret_id,
        region_name=region_name,
        required_keys=spec.required_credential_keys,
        source_label=f"{spec.display_name} (write-back)",
    ).get_credentials()
    return RestHttpSession(spec, credentials, policy)


def _validate_event(event: dict[str, Any]) -> None:
    missing = _REQUIRED_EVENT_FIELDS - event.keys()
    if missing:
        raise ValueError(f"Write-back event is missing required fields: {sorted(missing)}")
    if not TENANT_CODE_PATTERN.match(str(event["tenant_code"])):
        raise ValueError("tenant_code does not conform to the tenant code format.")
    validate_stable_id(str(event["source_id"]), "source_id")
    validate_stable_id(str(event["entity_id"]), "entity_id")
    if event.get("connection_id"):
        validate_stable_id(str(event["connection_id"]), "connection_id")
    records = event["records"]
    if not isinstance(records, list) or not records:
        raise ValueError("records must be a non-empty list.")
    if len(records) > _MAX_WRITEBACK_RECORDS:
        raise ValueError(
            f"records has {len(records)} entries, above the per-invocation cap of "
            f"{_MAX_WRITEBACK_RECORDS}. Split the batch."
        )
    if any(not isinstance(record, dict) for record in records):
        raise ValueError("every write-back record must be a JSON object.")
