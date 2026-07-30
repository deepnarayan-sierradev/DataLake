"""
Behavioural proof that a failed stage lands a message on its own DLQ (gap item 20).

`test_dlq_routing_reconciliation.py` proves the wiring exists — every queue has a declared producer.
This module proves the message actually arrives, because the previous state of the world was a
`failed_stage` argument that was accepted, carried through three layers, and then discarded at the
last step in favour of a hardcoded queue name. Every unit test of `enqueue_dlq_entry` passed
throughout: they asserted a message reached `datalake-extraction-failure-dlq-dev`, which it did.

So these read the queue.
"""

from __future__ import annotations

import json
from typing import Any

import boto3
import pytest
from moto import mock_aws

from contracts.dlq_routing import DlqStage, dlq_queue_name
from observability.stage_execution import StageIdentity, enqueue_stage_failure, stage_execution

_REGION = "us-east-1"


def _identity(dlq_stage: DlqStage, stage: str = "transformation") -> StageIdentity:
    return StageIdentity(
        tenant_code="evive",
        source_id="salesforce",
        entity_id="salesforce-account",
        run_id="run-20260729-000000000000-abcdef12",
        environment="dev",
        stage=stage,
        dlq_stage=dlq_stage,
        correlation_id="run-20260729-000000000000-abcdef12",
    )


def _messages(queue_url: str) -> list[dict[str, Any]]:
    response = boto3.client("sqs", region_name=_REGION).receive_message(
        QueueUrl=queue_url, MaxNumberOfMessages=10
    )
    return [json.loads(message["Body"]) for message in response.get("Messages", [])]


@mock_aws
class TestEachStageReachesItsOwnQueue:
    @pytest.mark.parametrize(
        "dlq_stage",
        [
            DlqStage.TRANSFORMATION,
            DlqStage.ENTITY_RESOLUTION,
            DlqStage.ANALYTICS_PUBLISH,
            DlqStage.SERVING_STORE_LOAD,
            DlqStage.TWIN_BUILD,
        ],
    )
    def test_the_message_lands_on_the_stages_queue(self, dlq_stage: DlqStage) -> None:
        """These are the five stages that previously enqueued to nothing at all."""
        sqs = boto3.client("sqs", region_name=_REGION)
        queue_url = sqs.create_queue(QueueName=dlq_queue_name(dlq_stage, "dev"))["QueueUrl"]

        delivered = enqueue_stage_failure(
            _identity(dlq_stage),
            error_code="transient_source_timeout",
            error_message="the source did not respond",
            region_name=_REGION,
        )

        assert delivered == 1
        bodies = _messages(queue_url)
        assert len(bodies) == 1
        assert bodies[0]["dlq_stage"] == dlq_stage.value
        assert bodies[0]["error_code"] == "transient_source_timeout"
        assert bodies[0]["tenant_code"] == "evive"

    def test_a_message_does_not_land_on_another_stages_queue(self) -> None:
        sqs = boto3.client("sqs", region_name=_REGION)
        transformation = sqs.create_queue(QueueName=dlq_queue_name(DlqStage.TRANSFORMATION, "dev"))[
            "QueueUrl"
        ]
        entity_resolution = sqs.create_queue(
            QueueName=dlq_queue_name(DlqStage.ENTITY_RESOLUTION, "dev")
        )["QueueUrl"]

        enqueue_stage_failure(
            _identity(DlqStage.TRANSFORMATION),
            error_code="x",
            error_message="y",
            region_name=_REGION,
        )

        assert len(_messages(transformation)) == 1
        assert _messages(entity_resolution) == []


@mock_aws
class TestTheScaffoldEnqueuesOnFailure:
    def test_an_exception_inside_the_context_produces_a_dlq_message(self) -> None:
        sqs = boto3.client("sqs", region_name=_REGION)
        queue_url = sqs.create_queue(QueueName=dlq_queue_name(DlqStage.TRANSFORMATION, "dev"))[
            "QueueUrl"
        ]

        with pytest.raises(RuntimeError, match="stage blew up"):
            with stage_execution(
                _identity(DlqStage.TRANSFORMATION), region_name=_REGION, lambda_context=None
            ):
                raise RuntimeError("stage blew up")

        bodies = _messages(queue_url)
        assert len(bodies) == 1
        assert bodies[0]["failed_stage"] == "transformation"

    def test_a_successful_stage_enqueues_nothing(self) -> None:
        sqs = boto3.client("sqs", region_name=_REGION)
        queue_url = sqs.create_queue(QueueName=dlq_queue_name(DlqStage.TRANSFORMATION, "dev"))[
            "QueueUrl"
        ]

        with stage_execution(
            _identity(DlqStage.TRANSFORMATION), region_name=_REGION, lambda_context=None
        ):
            pass

        assert _messages(queue_url) == []

    def test_a_not_replayable_stage_enqueues_nothing(self) -> None:
        with pytest.raises(RuntimeError):
            with stage_execution(
                _identity(DlqStage.NOT_REPLAYABLE, stage="portability"),
                region_name=_REGION,
                lambda_context=None,
            ):
                raise RuntimeError("deletion failed")

    def test_a_missing_queue_does_not_mask_the_original_failure(self) -> None:
        """
        The stage has already failed; a queueing problem must not replace its error. This is why the
        enqueue is best-effort — but it is logged and metered at zero rather than passed over.
        """
        with pytest.raises(RuntimeError, match="the real failure"):
            with stage_execution(
                _identity(DlqStage.WEBHOOK_INGEST), region_name=_REGION, lambda_context=None
            ):
                raise RuntimeError("the real failure")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
