"""
Tests for the webhook receiver Lambda (DL-CONN-14).

The load-bearing properties: an unsigned or wrongly-signed payload never reaches the queue, a
replayed provider event is enqueued once, ordering is preserved per tenant/connection/entity,
and no rejection response tells a forger why it failed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import boto3
import pytest
from moto import mock_aws

from conftest import RESOURCE_NAME_ENVIRONMENT
from connector_runtime import webhook_receiver_handler as receiver
from connector_runtime.webhook_signature import (
    SignatureAlgorithm,
    WebhookSignatureError,
    compute_signature,
)

_REGION = "us-east-1"
_SECRET = "webhook-shared-secret"  # nosec B105 — test fixture value
_QUEUE_NAME = "datalake-webhook-ingest-dev.fifo"


class _NullContext:
    """A Lambda context with no remaining-time API, so no watchdog is armed."""


@pytest.fixture(autouse=True)
def _env(monkeypatch: Any) -> None:
    monkeypatch.setenv("AWS_REGION", _REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _REGION)
    monkeypatch.setenv("PLATFORM_ENVIRONMENT", "dev")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")  # nosec B106 — moto stub
    monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", RESOURCE_NAME_ENVIRONMENT["WEBHOOK_DEDUP_TABLE"])


def _create_dedup_table() -> None:
    boto3.client("dynamodb", region_name=_REGION).create_table(
        TableName=RESOURCE_NAME_ENVIRONMENT["WEBHOOK_DEDUP_TABLE"],
        KeySchema=[
            {"AttributeName": "tenant_code", "KeyType": "HASH"},
            {"AttributeName": "provider_event_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "tenant_code", "AttributeType": "S"},
            {"AttributeName": "provider_event_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _create_queue() -> str:
    return boto3.client("sqs", region_name=_REGION).create_queue(
        QueueName=_QUEUE_NAME,
        Attributes={"FifoQueue": "true", "ContentBasedDeduplication": "false"},
    )["QueueUrl"]


def _create_secret(tenant_code: str = "demo", connection_id: str = "dialpad") -> None:
    boto3.client("secretsmanager", region_name=_REGION).create_secret(
        Name=f"{RESOURCE_NAME_ENVIRONMENT['SECRET_PATH_PREFIX']}/tenants/{tenant_code}/connections/{connection_id}/webhook-secret",
        SecretString=json.dumps({"webhook_secret": _SECRET}),
    )


def _dialpad_event(
    body: str = '{"id": "evt-1", "event_type": "call.summary"}',
    *,
    signature: str | None = None,
    tenant_code: str = "demo",
    source_id: str = "dialpad",
    connection_id: str | None = None,
) -> dict[str, Any]:
    signed = (
        signature
        if signature is not None
        else compute_signature(SignatureAlgorithm.HMAC_SHA256_HEX, _SECRET, body)
    )
    path_parameters = {"tenant_code": tenant_code, "source_id": source_id}
    if connection_id:
        path_parameters["connection_id"] = connection_id
    return {
        "pathParameters": path_parameters,
        "headers": {"X-Dialpad-Signature": signed},
        "body": body,
    }


def _queued_messages(queue_url: str) -> list[dict[str, Any]]:
    response = boto3.client("sqs", region_name=_REGION).receive_message(
        QueueUrl=queue_url, MaxNumberOfMessages=10, VisibilityTimeout=0
    )
    return [json.loads(m["Body"]) for m in response.get("Messages", [])]


class TestRequestParsing:
    def test_a_malformed_tenant_code_is_rejected(self) -> None:
        with pytest.raises(receiver.WebhookRejectedError, match="tenant_code"):
            receiver._parse_request(
                {"pathParameters": {"tenant_code": "../etc", "source_id": "dialpad"}}
            )

    def test_a_malformed_source_id_is_rejected(self) -> None:
        with pytest.raises(receiver.WebhookRejectedError, match="source_id"):
            receiver._parse_request(
                {"pathParameters": {"tenant_code": "demo", "source_id": "../../x"}}
            )

    def test_an_oversized_body_is_rejected_before_it_is_hashed(self) -> None:
        with pytest.raises(receiver.WebhookRejectedError, match="size"):
            receiver._parse_request(
                {
                    "pathParameters": {"tenant_code": "demo", "source_id": "dialpad"},
                    "body": "x" * (receiver._MAX_BODY_BYTES + 1),
                }
            )

    def test_a_non_json_body_is_rejected(self) -> None:
        with pytest.raises(receiver.WebhookRejectedError, match="JSON"):
            receiver._parse_request(
                {
                    "pathParameters": {"tenant_code": "demo", "source_id": "dialpad"},
                    "body": "not json",
                }
            )

    def test_headers_are_lowercased_so_provider_casing_does_not_matter(self) -> None:
        request = receiver._parse_request(
            {
                "pathParameters": {"tenant_code": "demo", "source_id": "dialpad"},
                "headers": {"X-Dialpad-Signature": "abc"},
                "body": "{}",
            }
        )
        assert request["headers"]["x-dialpad-signature"] == "abc"

    def test_the_connection_defaults_to_the_source_when_the_path_omits_it(self) -> None:
        request = receiver._parse_request(
            {"pathParameters": {"tenant_code": "demo", "source_id": "dialpad"}, "body": "{}"}
        )
        assert request["connection_id"] == "dialpad"

    def test_an_explicit_connection_is_honoured(self) -> None:
        request = receiver._parse_request(
            {
                "pathParameters": {
                    "tenant_code": "demo",
                    "source_id": "dialpad",
                    "connection_id": "dialpad-west",
                },
                "body": "{}",
            }
        )
        assert request["connection_id"] == "dialpad-west"

    def test_the_event_key_is_stable_for_the_same_provider_event(self) -> None:
        event = {
            "pathParameters": {"tenant_code": "demo", "source_id": "dialpad"},
            "body": '{"id": "evt-1"}',
        }
        assert (
            receiver._parse_request(event)["event_key"]
            == (receiver._parse_request(event)["event_key"])
        )

    def test_the_event_key_differs_across_tenants_for_the_same_provider_event(self) -> None:
        body = '{"id": "evt-1"}'
        first = receiver._parse_request(
            {"pathParameters": {"tenant_code": "demo", "source_id": "dialpad"}, "body": body}
        )
        second = receiver._parse_request(
            {"pathParameters": {"tenant_code": "acme", "source_id": "dialpad"}, "body": body}
        )
        assert first["event_key"] != second["event_key"]


class TestEntityAndEventIdDerivation:
    def test_an_unrecognised_object_type_lands_in_the_per_source_inbox(self) -> None:
        assert receiver._resolve_entity_id("dialpad", {"type": "!!!"}) == "dialpad-webhook"

    def test_dots_and_underscores_normalise_to_hyphens(self) -> None:
        assert (
            receiver._resolve_entity_id("dialpad", {"event_type": "call.summary_v2"})
            == "dialpad-call-summary-v2"
        )

    def test_a_non_dict_payload_still_resolves(self) -> None:
        assert receiver._resolve_entity_id("dialpad", ["a"]) == "dialpad-webhook"

    def test_the_provider_event_id_is_preferred_over_a_content_hash(self) -> None:
        assert receiver._resolve_event_id({"eventId": "evt-9"}, "{}") == "evt-9"

    def test_a_payload_with_no_id_falls_back_to_a_content_hash(self) -> None:
        derived = receiver._resolve_event_id({}, '{"a": 1}')
        assert derived == receiver.sha256_hex('{"a": 1}')

    def test_the_content_hash_differs_for_different_bodies(self) -> None:
        assert receiver._resolve_event_id({}, "a") != receiver._resolve_event_id({}, "b")


@mock_aws
class TestSecretResolution:
    def test_a_plain_string_secret_is_used_as_is(self) -> None:
        boto3.client("secretsmanager", region_name=_REGION).create_secret(
            Name=f"{RESOURCE_NAME_ENVIRONMENT['SECRET_PATH_PREFIX']}/tenants/demo/connections/dialpad/webhook-secret",
            SecretString="raw-secret",
        )
        secret = receiver._webhook_secret(
            {"tenant_code": "demo", "connection_id": "dialpad"}, _REGION
        )
        assert secret == "raw-secret"

    def test_a_json_secret_is_read_from_the_webhook_secret_key(self) -> None:
        _create_secret()
        assert (
            receiver._webhook_secret({"tenant_code": "demo", "connection_id": "dialpad"}, _REGION)
            == _SECRET
        )

    def test_a_missing_secret_fails_closed(self) -> None:
        with pytest.raises(WebhookSignatureError, match="could not be"):
            receiver._webhook_secret({"tenant_code": "demo", "connection_id": "absent"}, _REGION)

    def test_a_json_secret_without_the_expected_key_fails_closed(self) -> None:
        boto3.client("secretsmanager", region_name=_REGION).create_secret(
            Name=f"{RESOURCE_NAME_ENVIRONMENT['SECRET_PATH_PREFIX']}/tenants/demo/connections/dialpad/webhook-secret",
            SecretString=json.dumps({"api_key": "x"}),
        )
        with pytest.raises(WebhookSignatureError, match="webhook_secret"):
            receiver._webhook_secret({"tenant_code": "demo", "connection_id": "dialpad"}, _REGION)

    def test_the_secret_path_is_per_connection_not_per_source(self) -> None:
        _create_secret(tenant_code="demo")
        with pytest.raises(WebhookSignatureError):
            receiver._webhook_secret({"tenant_code": "acme", "connection_id": "dialpad"}, _REGION)


@mock_aws
class TestHandler:
    def setup_method(self, method: object = None) -> None:
        self.queue_url = ""

    def _prepare(self, monkeypatch: Any) -> str:
        _create_dedup_table()
        _create_secret()
        queue_url = _create_queue()
        monkeypatch.setenv("WEBHOOK_INGEST_QUEUE_URL", queue_url)
        return queue_url

    def test_a_correctly_signed_event_is_accepted_and_enqueued(self, monkeypatch: Any) -> None:
        queue_url = self._prepare(monkeypatch)
        response = receiver.lambda_handler(_dialpad_event(), _NullContext())
        assert response["statusCode"] == 202
        messages = _queued_messages(queue_url)
        assert len(messages) == 1
        assert messages[0]["tenant_code"] == "demo"
        assert messages[0]["provider_event_id"] == "evt-1"

    def test_an_unsigned_event_is_refused_and_never_enqueued(self, monkeypatch: Any) -> None:
        queue_url = self._prepare(monkeypatch)
        event = _dialpad_event()
        event["headers"] = {}
        response = receiver.lambda_handler(event, _NullContext())
        assert response["statusCode"] == 401
        assert _queued_messages(queue_url) == []

    def test_a_wrongly_signed_event_is_refused(self, monkeypatch: Any) -> None:
        queue_url = self._prepare(monkeypatch)
        response = receiver.lambda_handler(_dialpad_event(signature="deadbeef"), _NullContext())
        assert response["statusCode"] == 401
        assert _queued_messages(queue_url) == []

    def test_the_rejection_body_does_not_explain_why_the_signature_failed(
        self, monkeypatch: Any
    ) -> None:
        self._prepare(monkeypatch)
        response = receiver.lambda_handler(_dialpad_event(signature="deadbeef"), _NullContext())
        assert json.loads(response["body"]) == {"message": "signature verification failed"}

    def test_a_malformed_request_is_a_400_before_any_aws_call(self, monkeypatch: Any) -> None:
        self._prepare(monkeypatch)
        response = receiver.lambda_handler(
            {"pathParameters": {"tenant_code": "../etc", "source_id": "dialpad"}}, _NullContext()
        )
        assert response["statusCode"] == 400

    def test_the_response_never_echoes_the_payload(self, monkeypatch: Any) -> None:
        self._prepare(monkeypatch)
        response = receiver.lambda_handler(
            _dialpad_event(body='{"id": "evt-1", "secret_note": "leak-me"}'), _NullContext()
        )
        assert "leak-me" not in json.dumps(response)

    def test_a_replayed_provider_event_is_enqueued_once(self, monkeypatch: Any) -> None:
        queue_url = self._prepare(monkeypatch)
        first = receiver.lambda_handler(_dialpad_event(), _NullContext())
        second = receiver.lambda_handler(_dialpad_event(), _NullContext())
        assert first["statusCode"] == 202
        assert second["statusCode"] == 200
        assert len(_queued_messages(queue_url)) == 1

    def test_ordering_is_grouped_per_tenant_connection_and_entity(self, monkeypatch: Any) -> None:
        queue_url = self._prepare(monkeypatch)
        sent: list[dict[str, Any]] = []
        real_client = boto3.client("sqs", region_name=_REGION)

        class _CapturingSqs:
            def send_message(self, **kwargs: Any) -> dict[str, Any]:
                sent.append(kwargs)
                return real_client.send_message(**kwargs)

        monkeypatch.setattr(boto3, "client", _patched_client(_CapturingSqs()))
        receiver.lambda_handler(_dialpad_event(), _NullContext())
        assert sent[0]["MessageGroupId"] == "demo#dialpad#dialpad-call-summary"
        assert sent[0]["MessageDeduplicationId"].startswith("whk-")
        assert queue_url  # the queue the handler was pointed at

    def test_a_source_with_no_signature_spec_is_refused(self, monkeypatch: Any) -> None:
        queue_url = self._prepare(monkeypatch)
        response = receiver.lambda_handler(_dialpad_event(source_id="salesforce"), _NullContext())
        assert response["statusCode"] == 401
        assert _queued_messages(queue_url) == []

    def test_two_tenants_sending_the_same_provider_event_id_both_enqueue(
        self, monkeypatch: Any
    ) -> None:
        queue_url = self._prepare(monkeypatch)
        _create_secret(tenant_code="acme")
        receiver.lambda_handler(_dialpad_event(), _NullContext())
        second = receiver.lambda_handler(_dialpad_event(tenant_code="acme"), _NullContext())
        assert second["statusCode"] == 202
        assert len(_queued_messages(queue_url)) == 2


def _patched_client(sqs_double: Any) -> Any:
    """Return a boto3.client replacement that swaps only the sqs client."""
    real: Any = boto3.client

    def factory(service_name: str, *args: Any, **kwargs: Any) -> Any:
        if service_name == "sqs":
            return sqs_double
        return real(service_name, *args, **kwargs)

    return factory


class TestHubspotStyleSigning:
    def test_hubspot_signs_body_and_timestamp_together(self) -> None:
        from connector_runtime.webhook_signature import (
            spec_for_source,
            verify_webhook_signature,
        )

        spec = spec_for_source("hubspot")
        body = '{"objectType": "contact"}'
        timestamp = str(int(time.time() * 1000))
        signature = compute_signature(spec.algorithm, _SECRET, f"{body}{timestamp}")
        verify_webhook_signature(
            spec=spec,
            secret=_SECRET,
            body=body,
            headers={
                spec.signature_header: signature,
                str(spec.timestamp_header): timestamp,
            },
        )

    def test_a_stale_hubspot_timestamp_is_treated_as_a_replay(self) -> None:
        from connector_runtime.webhook_signature import (
            spec_for_source,
            verify_webhook_signature,
        )

        spec = spec_for_source("hubspot")
        body = "{}"
        timestamp = str(int((time.time() - 86_400) * 1000))
        signature = compute_signature(spec.algorithm, _SECRET, f"{body}{timestamp}")
        with pytest.raises(WebhookSignatureError, match="replay"):
            verify_webhook_signature(
                spec=spec,
                secret=_SECRET,
                body=body,
                headers={
                    spec.signature_header: signature,
                    str(spec.timestamp_header): timestamp,
                },
            )

    def test_signature_comparison_is_not_a_plain_equality_on_the_raw_header(self) -> None:
        from connector_runtime.webhook_signature import (
            spec_for_source,
            verify_webhook_signature,
        )

        spec = spec_for_source("dialpad")
        body = "{}"
        signature = hmac.new(_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
        verify_webhook_signature(
            spec=spec,
            secret=_SECRET,
            body=body,
            headers={spec.signature_header: f"  {signature}  "},
        )
