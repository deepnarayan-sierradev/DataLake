"""
Portability Lambda: tenant export and tenant deletion (DL-PORT-01…DL-PORT-10).

Both operations were complete, tested libraries with no caller — `DeletionWorkflow` had no
reference anywhere in the repository. This is their entry point.

Why one Lambda for two operations: both are privileged, tenant-scoped, long-running, and audited
the same way, and both are invoked rarely. Two functions would duplicate the IAM surface and the
audit wiring for no separation benefit — they are separated by *action*, validated below, not by
deployment unit.

Security properties enforced here (OWASP A01, A04, A09):

- The **capability** is checked before anything is read: export requires an export-specific
  capability distinct from read, so a reader cannot exfiltrate in bulk.
- The **scope predicate is applied row by row** during export, so a franchisee's export contains
  only their rows. It is a required parameter, not a default.
- **Deletion is maker-checker** and refuses to proceed past an unacknowledged legal hold, so a
  partial deletion is never silently certified as complete.
- `AdminActions` is recorded for both, because they are privileged operations this system genuinely
  owns (unlike tenant administration, which belongs to the Identity API).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Final

from analytics_publisher.analytics_location import latest_partition_uri
from contracts.dlq_routing import DlqStage
from contracts.platform_metrics import PlatformMetric
from observability.lambda_runtime import require_env
from observability.metric_recorder import record_platform_metric
from observability.stage_execution import StageIdentity, derive_correlation_id, stage_execution
from observability.structured_logger import get_platform_logger
from persistence.parquet_reader import iter_parquet_records
from persistence.tenant_tables import TENANT_SCOPED_TABLES
from portability.export_service import ExportFormat, ExportLayer, ExportService
from tenancy.scope_predicate import (
    ConsumptionSurface,
    ScopePredicate,
    build_scope_claims,
    scope_predicate,
)
from tenancy.scope_unit_repository import ScopeUnitRepository

_logger = get_platform_logger(__name__)

_STAGE: Final[str] = "portability"

SUPPORTED_ACTIONS: Final[frozenset[str]] = frozenset({"export", "delete"})


class PortabilityEventError(ValueError):
    """Raised when the invocation payload is not a well-formed portability request."""


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Entry point for an export or deletion request."""
    action = str(event.get("action") or "")
    if action not in SUPPORTED_ACTIONS:
        raise PortabilityEventError(
            f"action must be one of {sorted(SUPPORTED_ACTIONS)}; got {action!r}. The action is "
            "validated before any tenant data is touched."
        )
    tenant_code = str(event.get("tenant_code") or "")
    if not tenant_code:
        raise PortabilityEventError("A portability request must name its tenant_code.")

    region_name = require_env("AWS_REGION")
    environment = require_env("PLATFORM_ENVIRONMENT")
    run_id = str(event.get("run_id") or f"prt-{action}")

    identity = StageIdentity(
        tenant_code=tenant_code,
        source_id="portability",
        entity_id=str(event.get("entity_id") or "tenant"),
        run_id=run_id,
        environment=environment,
        stage=_STAGE,
        # Deliberately not replayable: automatically retrying a deletion or an export is wrong, and
        # the deletion certificate (or the failed job record) is already the evidence. Declared
        # rather than omitted, so a handler that simply forgot to route remains a build error.
        dlq_stage=DlqStage.NOT_REPLAYABLE,
        correlation_id=derive_correlation_id(run_id, event.get("replay_of_run_id")),
    )

    with stage_execution(identity, region_name=region_name, lambda_context=context):
        # A privileged operation this system owns outright, so it is the correct producer for
        # AdminActions — see the ownership boundary in the root CLAUDE.md.
        record_platform_metric(
            PlatformMetric.ADMIN_ACTIONS, 1.0, Capability=f"portability_{action}"
        )
        if action == "export":
            return _run_export(event, tenant_code, environment, region_name)
        return _run_deletion(event, tenant_code, environment, region_name)


def _scope_predicate_from_event(
    event: dict[str, Any], tenant_code: str, environment: str, region_name: str
) -> ScopePredicate:
    """
    Build the export's row filter from the granted scope units on the request.

    Two defects lived here until 2026-07-29, and the docstring described the fix for neither:

    - It said "an absent grant raises `EmptyScopeDenialError`… an export with no scope is an export
      of everything", and then `return None` on exactly that path — which `_apply_scope` treated as
      "no filter". The stated behaviour is now the implemented behaviour.
    - It built `ScopeClaims(...)` directly, bypassing `build_scope_claims`, so a unit the tenant
      does not own was never rejected, a region grant was never expanded to its leaves, and
      `CrossScopeAccessAttempts` never fired. Claims are now built the one supported way, against
      the tenant's real partition profile and registered units.

    The grant is *forwarded* by the control plane, which read it from a verified JWT. That is only
    trustworthy because the function's invoke permission is restricted to the control-plane role
    (see `infrastructure/modules/iam/platform_lambda_roles.tf`) — payload trust is an IAM property
    here, not an assumption, and the units are re-validated against the registry regardless.
    """
    granted = frozenset(
        str(unit).strip().lower()
        for unit in (event.get("granted_scope_units") or [])
        if str(unit).strip()
    )
    tenant_wide = bool(event.get("granted_scope_tenant_wide", False))
    repository = ScopeUnitRepository(environment=environment, region_name=region_name)
    claims = build_scope_claims(
        tenant_code,
        repository.get_partition_profile(tenant_code),
        granted_scope_unit_ids=granted,
        tenant_wide=tenant_wide,
        units=repository.list_scope_units(tenant_code),
    )
    return scope_predicate(claims, surface=ConsumptionSurface.EXPORT)


def _run_export(
    event: dict[str, Any], tenant_code: str, environment: str, region_name: str
) -> dict[str, Any]:
    """
    Request the job and produce the artefact in one invocation.

    This previously created the job, returned its id, and commented that "row production is the
    caller's next step" — but no caller existed anywhere in the repository, so
    `ExportService.execute` had no production call site and DL-PORT-01 rendered nothing. The format
    conversion, the KMS-encrypted upload, and the row-by-row scope filter were all dead code that
    read as delivered, and `tests/test_scope_call_sites.py` accepted it because its assertion for
    the export surface was that the string `scope_predicate` appears in `execute`'s source.

    Rows stream from the analytics partition; the artefact is written from an iterator, so a large
    export is bounded by one row group rather than by the entity's size.
    """
    service = ExportService(
        environment=environment,
        region_name=region_name,
        artefact_bucket=require_env("EXPORT_ARTEFACT_BUCKET"),
    )
    predicate = _scope_predicate_from_event(event, tenant_code, environment, region_name)
    entity_id = str(event["entity_id"])
    job = service.request_export(
        tenant_code,
        ExportLayer(str(event["layer"])),
        ExportFormat(str(event["export_format"])),
        entity_id,
        requested_by=str(event.get("requested_by") or ""),
        granted_capabilities=frozenset(
            str(capability) for capability in (event.get("granted_capabilities") or [])
        ),
        scope_predicate=predicate,
    )
    _logger.info(
        "export_requested",
        tenant_code=tenant_code,
        job_id=job.job_id,
        layer=job.layer.value,
        export_format=job.export_format.value,
    )

    completed = service.execute(
        job, _export_rows(tenant_code, entity_id, region_name), scope_predicate=predicate
    )
    _logger.info(
        "export_completed",
        tenant_code=tenant_code,
        job_id=completed.job_id,
        row_count=completed.row_count,
        artefact_bytes=completed.artefact_bytes,
    )
    return {
        "job_id": completed.job_id,
        "status": completed.status.value,
        "row_count": completed.row_count,
        "artefact_bytes": completed.artefact_bytes,
    }


def _export_rows(tenant_code: str, entity_id: str, region_name: str) -> Iterator[dict[str, Any]]:
    """Stream the entity's latest analytics partition; empty when nothing has been published."""
    import boto3

    bucket = require_env("ANALYTICS_S3_BUCKET")
    s3 = boto3.client("s3", region_name=region_name)
    uri = latest_partition_uri(s3, bucket, tenant_code, entity_id)
    if uri is None:
        _logger.warning(
            "export_has_no_analytics_partition", tenant_code=tenant_code, entity_id=entity_id
        )
        return
    prefix = uri.removeprefix(f"s3://{bucket}/")
    yield from iter_parquet_records(s3, bucket, prefix)


def _run_deletion(
    event: dict[str, Any], tenant_code: str, environment: str, region_name: str
) -> dict[str, Any]:
    import boto3

    from portability.deletion_workflow import (
        DeletionRequest,
        DeletionSaga,
        DeletionStore,
        cloudwatch_logs_tenant_deleter,
        dynamodb_tenant_item_deleter,
        no_artefacts_deleter,
        s3_prefix_deleter,
        secrets_manager_tenant_deleter,
        serving_store_tenant_deleter,
    )

    # Maker-checker and the typed confirmation are enforced by DeletionRequest itself; constructing
    # it is the authorization check, which is why it happens before any deleter is built.
    request = DeletionRequest(
        tenant_code=tenant_code,
        requested_by=str(event.get("requested_by") or ""),
        approved_by=str(event.get("approved_by") or ""),
        typed_confirmation=str(event.get("typed_confirmation") or ""),
        scope_description=str(event.get("scope_description") or "all customer data"),
        acknowledged_holds=tuple(str(store) for store in (event.get("acknowledged_holds") or [])),
    )

    s3 = boto3.client("s3", region_name=region_name)
    # Every store in `DeletionStore` now has a deleter. Four did until 2026-07-29, and because the
    # saga refuses to certify a deletion whose stores it did not cover, that meant a correctly
    # authorised deletion *always* raised `IncompleteDeletionError` — DL-PORT-04 and SOW §24.7
    # could not succeed. Failing loudly was the right design; the missing deleters were the defect.
    deleters = {
        DeletionStore.S3_RAW: s3_prefix_deleter(
            s3, require_env("RAW_S3_BUCKET"), DeletionStore.S3_RAW
        ),
        DeletionStore.S3_CURATED: s3_prefix_deleter(
            s3, require_env("CURATED_S3_BUCKET"), DeletionStore.S3_CURATED
        ),
        DeletionStore.S3_ANALYTICS: s3_prefix_deleter(
            s3, require_env("ANALYTICS_S3_BUCKET"), DeletionStore.S3_ANALYTICS
        ),
        DeletionStore.S3_GOVERNANCE: s3_prefix_deleter(
            s3, require_env("GOVERNANCE_S3_BUCKET"), DeletionStore.S3_GOVERNANCE
        ),
        DeletionStore.S3_SCHEMA_SNAPSHOTS: s3_prefix_deleter(
            s3, require_env("SCHEMA_SNAPSHOTS_S3_BUCKET"), DeletionStore.S3_SCHEMA_SNAPSHOTS
        ),
        DeletionStore.S3_EXPORTS: s3_prefix_deleter(
            s3, require_env("EXPORT_ARTEFACT_BUCKET"), DeletionStore.S3_EXPORTS
        ),
        DeletionStore.DYNAMODB_TABLES: dynamodb_tenant_item_deleter(
            boto3.resource("dynamodb", region_name=region_name), TENANT_SCOPED_TABLES
        ),
        DeletionStore.SECRETS_MANAGER: secrets_manager_tenant_deleter(
            boto3.client("secretsmanager", region_name=region_name)
        ),
        DeletionStore.SERVING_STORE: serving_store_tenant_deleter(
            _serving_store_container_dropper(environment, region_name)
        ),
        DeletionStore.CLOUDWATCH_LOGS: cloudwatch_logs_tenant_deleter(
            boto3.client("logs", region_name=region_name), _pipeline_log_groups(environment)
        ),
        # DL-05 is deferred, so there is no ML platform and no artefacts. Stated rather than
        # omitted: omitting the store makes the saga refuse to certify forever.
        DeletionStore.ML_ARTEFACTS: no_artefacts_deleter(
            DeletionStore.ML_ARTEFACTS, "DL-05 ML platform is deferred; no artefacts are produced"
        ),
    }
    certificate = DeletionSaga(
        environment=environment, region_name=region_name, deleters=deleters
    ).execute(request)
    _logger.warning(
        "tenant_deletion_certified",
        tenant_code=tenant_code,
        requested_by=request.requested_by,
        approved_by=request.approved_by,
        stores=[step.store.value for step in certificate.steps],
    )
    return {
        "certificate_id": certificate.certificate_id,
        "tenant_code": tenant_code,
        "stores_deleted": len(certificate.steps),
    }


def _pipeline_log_groups(environment: str) -> tuple[str, ...]:
    """Log groups whose streams are tenant-prefixed; the groups themselves are shared."""
    return tuple(
        f"/aws/lambda/edl-{function}-{environment}"
        for function in (
            "extraction",
            "transformation",
            "entity-resolution",
            "analytics-publisher",
            "serving-store-loader",
            "twin-build",
        )
    )


def _serving_store_container_dropper(environment: str, region_name: str) -> Any:
    """
    Resolve a callable that drops the tenant's serving-store container.

    An environment with no serving store configured for the tenant has nothing to drop, and that is
    a genuine zero rather than an incomplete deletion — but it is decided here, once, rather than by
    omitting the store from the deleter map (which would make the saga refuse to certify).
    """
    from serving_store.registry import serving_store_registry
    from serving_store.serving_store_config_repository import ServingStoreConfigRepositoryClient

    def drop(tenant_code: str) -> int:
        configs = ServingStoreConfigRepositoryClient(
            environment=environment, region_name=region_name
        ).list_configs_for_tenant(tenant_code)
        dropped = 0
        for config in configs:
            loader = serving_store_registry.resolve(
                config.target_engine.value,
                secret_arn=config.secret_arn,
                region_name=config.region_name,
                db_host=config.db_host,
                db_port=config.db_port,
            )
            dropped += loader.drop_tenant_container(tenant_code)
        return dropped

    return drop
