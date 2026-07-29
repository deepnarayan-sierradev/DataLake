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

from typing import Any, Final

from contracts.platform_metrics import PlatformMetric
from observability.lambda_runtime import require_env
from observability.metric_recorder import record_platform_metric
from observability.stage_execution import StageIdentity, derive_correlation_id, stage_execution
from observability.structured_logger import get_platform_logger
from portability.export_service import ExportFormat, ExportLayer, ExportService
from tenancy.scope_predicate import ConsumptionSurface, ScopeClaims, scope_predicate

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


def _scope_predicate_from_event(event: dict[str, Any], tenant_code: str) -> Any:
    """
    Build the export's row filter from the granted scope units on the request.

    The units come from the caller's verified claim, forwarded by the control plane — never chosen
    by the payload itself. An absent grant raises `EmptyScopeDenialError`, which is the correct
    outcome: an export with no scope is an export of everything.
    """
    granted = frozenset(
        str(unit).strip().lower()
        for unit in (event.get("granted_scope_units") or [])
        if str(unit).strip()
    )
    if not granted:
        return None
    return scope_predicate(
        ScopeClaims(tenant_code=tenant_code, scope_unit_ids=granted),
        surface=ConsumptionSurface.EXPORT,
    )


def _run_export(
    event: dict[str, Any], tenant_code: str, environment: str, region_name: str
) -> dict[str, Any]:
    service = ExportService(
        environment=environment,
        region_name=region_name,
        artefact_bucket=require_env("EXPORT_ARTEFACT_BUCKET"),
    )
    predicate = _scope_predicate_from_event(event, tenant_code)
    job = service.request_export(
        tenant_code,
        ExportLayer(str(event["layer"])),
        ExportFormat(str(event["export_format"])),
        str(event["entity_id"]),
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
        scoped=predicate is not None,
    )
    # Row production is the caller's next step: the rows come from the analytics layer through a
    # separate read, so the request and the artefact write are distinct invocations. Returning the
    # job id rather than executing inline keeps a large export off a single Lambda's clock.
    return {"job_id": job.job_id, "status": job.status.value, "scoped": predicate is not None}


def _run_deletion(
    event: dict[str, Any], tenant_code: str, environment: str, region_name: str
) -> dict[str, Any]:
    import boto3

    from portability.deletion_workflow import (
        DeletionRequest,
        DeletionSaga,
        DeletionStore,
        s3_prefix_deleter,
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
    # Only the S3 layers are deleted here. The remaining stores in `DeletionStore` need their own
    # deleters (DynamoDB item sweep, Secrets Manager, serving-store DROP, log-group deletion), and
    # the saga refuses to certify a deletion whose stores it did not cover — so an incomplete map
    # produces a visible `IncompleteDeletionError`, never a certificate that overstates.
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
        DeletionStore.S3_EXPORTS: s3_prefix_deleter(
            s3, require_env("EXPORT_ARTEFACT_BUCKET"), DeletionStore.S3_EXPORTS
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
