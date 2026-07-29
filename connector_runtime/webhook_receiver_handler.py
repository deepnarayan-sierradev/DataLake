"""
Webhook receiver Lambda (DL-CONN-14).

Verifies the provider signature, deduplicates by provider event id in a short-TTL DynamoDB
table, enqueues to the existing SQS FIFO path with
`MessageGroupId = tenant_code#connection_id#entity_id`, and never processes inline.

Security (OWASP A03, A08, A09): signature verification is mandatory and fails closed;
bucket, table, and queue names come from `require_env`, never from the request; the response
body never echoes the payload.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError

from connector_runtime.webhook_signature import (
    WebhookSignatureError,
    sha256_hex,
    spec_for_source,
    verify_webhook_signature,
)
from contracts.identifier_policy import (
    STABLE_ID_PATTERN,
    TENANT_CODE_PATTERN,
    validate_stable_id,
)
from contracts.platform_metrics import PlatformMetric
from observability.lambda_runtime import require_env
from observability.metrics_emitter import CloudWatchMetricsEmitter
from observability.stage_execution import StageIdentity, derive_correlation_id, stage_execution
from observability.structured_logger import get_platform_logger
from tenancy.connection_keys import resolve_connection_id

_logger = get_platform_logger(__name__)

_DEDUP_TABLE_NAME: Final[str] = "EdlWebhookEventDedup"
_DEDUP_TTL_SECONDS: Final[int] = 48 * 3_600

# Reject an oversized body before hashing it (OWASP A05 — resource exhaustion).
_MAX_BODY_BYTES: Final[int] = 1_048_576


class WebhookRejectedError(Exception):
    """Raised when a webhook request is malformed; surfaces as a 400 to the provider."""


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """API Gateway proxy entry point for provider webhooks."""
    region_name = require_env("AWS_REGION")
    environment = require_env("PLATFORM_ENVIRONMENT")
    queue_url = require_env("WEBHOOK_INGEST_QUEUE_URL")

    metrics = CloudWatchMetricsEmitter(region_name=region_name)
    try:
        request = _parse_request(event)
    except WebhookRejectedError as exc:
        metrics.emit_metric(PlatformMetric.INPUT_VALIDATION_FAILURES, environment=environment)
        metrics.flush()
        _logger.warning("webhook_request_rejected", error=str(exc))
        return _response(400, "invalid request")

    metrics.set_tenant_context(request["tenant_code"])
    identity = StageIdentity(
        tenant_code=request["tenant_code"],
        source_id=request["source_id"],
        entity_id=request["entity_id"],
        run_id=request["event_key"],
        environment=environment,
        stage="webhook_ingest",
        correlation_id=derive_correlation_id(request["event_key"]),
        connection_id=request["connection_id"],
    )

    with stage_execution(identity, region_name=region_name, lambda_context=context) as execution:
        try:
            _verify_signature(request, region_name)
        except WebhookSignatureError as exc:
            execution.emit(PlatformMetric.WEBHOOK_SIGNATURE_FAILURES)
            _logger.warning(
                "webhook_signature_verification_failed",
                source_id=request["source_id"],
                error=str(exc),
            )
            # A specific reason would help an attacker tune a forgery attempt.
            return _response(401, "signature verification failed")

        if _already_seen(request, region_name):
            execution.emit(PlatformMetric.WEBHOOK_EVENTS_RECEIVED, 0.0)
            _logger.info("webhook_event_replay_ignored", provider_event_id=request["event_id"])
            return _response(200, "duplicate ignored")

        _enqueue(request, queue_url, region_name)
        execution.emit(PlatformMetric.WEBHOOK_EVENTS_RECEIVED)
        _logger.info(
            "webhook_event_enqueued",
            source_id=request["source_id"],
            connection_id=request["connection_id"],
            entity_id=request["entity_id"],
        )
        return _response(202, "accepted")


# ---------------------------------------------------------------------------
# Private
# ---------------------------------------------------------------------------


def _parse_request(event: dict[str, Any]) -> dict[str, Any]:
    """Validate the proxy event into a flat, already-checked request dict (OWASP A03)."""
    path_parameters = event.get("pathParameters") or {}
    headers = {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}
    body = event.get("body") or ""
    if len(body.encode("utf-8")) > _MAX_BODY_BYTES:
        raise WebhookRejectedError("webhook body exceeds the permitted size")

    tenant_code = str(path_parameters.get("tenant_code", ""))
    source_id = str(path_parameters.get("source_id", ""))
    connection_id = str(path_parameters.get("connection_id", "") or source_id)
    if not TENANT_CODE_PATTERN.match(tenant_code):
        raise WebhookRejectedError("tenant_code is malformed")
    if not STABLE_ID_PATTERN.match(source_id):
        raise WebhookRejectedError("source_id is malformed")
    validate_stable_id(connection_id, "connection_id")

    try:
        payload = json.loads(body) if body else {}
    except (json.JSONDecodeError, ValueError) as exc:
        raise WebhookRejectedError("webhook body is not valid JSON") from exc

    entity_id = _resolve_entity_id(source_id, payload)
    event_id = _resolve_event_id(payload, body)

    return {
        "tenant_code": tenant_code,
        "source_id": source_id,
        "connection_id": resolve_connection_id(source_id, connection_id),
        "entity_id": entity_id,
        "event_id": event_id,
        "event_key": f"whk-{sha256_hex(f'{tenant_code}:{connection_id}:{event_id}')[:24]}",
        "headers": headers,
        "body": body,
        "payload": payload,
    }


def _resolve_entity_id(source_id: str, payload: Any) -> str:
    """
    Derive the target entity from the provider payload, defaulting to a per-source inbox.

    A provider naming an object type we do not recognise still lands somewhere auditable
    rather than being dropped.
    """
    candidate = ""
    if isinstance(payload, dict):
        candidate = str(
            payload.get("subscriptionType")
            or payload.get("objectType")
            or payload.get("event_type")
            or payload.get("type")
            or ""
        )
    normalised = candidate.strip().lower().replace(".", "-").replace("_", "-")
    entity_id = f"{source_id}-{normalised}" if normalised else f"{source_id}-webhook"
    if not STABLE_ID_PATTERN.match(entity_id):
        return f"{source_id}-webhook"
    return entity_id


def _resolve_event_id(payload: Any, body: str) -> str:
    """Provider event id where offered, else a content hash so dedup still works."""
    if isinstance(payload, dict):
        for key in ("eventId", "event_id", "id", "messageId"):
            if payload.get(key):
                return str(payload[key])
    return sha256_hex(body)


def _verify_signature(request: dict[str, Any], region_name: str) -> None:
    spec = spec_for_source(request["source_id"])
    secret = _webhook_secret(request, region_name)
    verify_webhook_signature(
        spec=spec,
        secret=secret,
        body=request["body"],
        headers=request["headers"],
    )


def _webhook_secret(request: dict[str, Any], region_name: str) -> str:
    """Per-connection webhook secret; never logged, never returned."""
    secret_id = (
        f"edl/tenants/{request['tenant_code']}/connections/"
        f"{request['connection_id']}/webhook-secret"
    )
    client = boto3.client("secretsmanager", region_name=region_name)
    try:
        response = client.get_secret_value(SecretId=secret_id)
    except ClientError as exc:
        raise WebhookSignatureError(
            f"Webhook secret for connection {request['connection_id']!r} could not be "
            f"retrieved: {exc.response['Error']['Code']}"
        ) from None
    raw = response.get("SecretString") or ""
    if not raw:
        raise WebhookSignatureError("Webhook secret is present but empty.")
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw
    value = parsed.get("webhook_secret") if isinstance(parsed, dict) else None
    if not value:
        raise WebhookSignatureError("Webhook secret JSON has no 'webhook_secret' key.")
    return str(value)


def _already_seen(request: dict[str, Any], region_name: str) -> bool:
    """Conditional write on the dedup table; a lost race is treated as a duplicate."""
    table_name = os.environ.get("WEBHOOK_DEDUP_TABLE") or _DEDUP_TABLE_NAME
    table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)
    try:
        table.put_item(
            Item={
                "tenant_code": request["tenant_code"],
                "provider_event_id": f"{request['connection_id']}#{request['event_id']}",
                "received_at": int(time.time()),
                "expires_at": int(time.time()) + _DEDUP_TTL_SECONDS,
            },
            ConditionExpression=(
                "attribute_not_exists(tenant_code) AND attribute_not_exists(provider_event_id)"
            ),
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return True
        raise
    return False


def _enqueue(request: dict[str, Any], queue_url: str, region_name: str) -> None:
    """FIFO enqueue grouped per tenant/connection/entity so ordering holds per stream."""
    sqs = boto3.client("sqs", region_name=region_name)
    group_id = f"{request['tenant_code']}#{request['connection_id']}#{request['entity_id']}"
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(
            {
                "tenant_code": request["tenant_code"],
                "source_id": request["source_id"],
                "connection_id": request["connection_id"],
                "entity_id": request["entity_id"],
                "provider_event_id": request["event_id"],
                "payload": request["payload"],
            },
            separators=(",", ":"),
        ),
        MessageGroupId=group_id,
        MessageDeduplicationId=request["event_key"],
    )


def _response(status_code: int, message: str) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"message": message}),
    }
