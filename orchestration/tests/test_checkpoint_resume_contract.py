"""
The checkpoint-resume contract between the extraction Lambda and the state machine (L14).

Two problems shared one solution:

- a provider's `Retry-After` was absorbed by sleeping inside the Lambda, which is billed
  wall-clock inside a 900-second budget — a throttling provider consumed the invocation doing
  nothing and then died at the timeout mid-entity;
- a checkpoint routed to a terminal `Succeed`, so the remaining window was never processed without
  a manual re-trigger.

The state machine now catches the checkpoint, waits for free in a `Wait` state, and re-invokes
extraction. These tests assert the Python half of that contract and the ASL half's structure —
the parts that must agree for the loop to work at all.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final

import pytest

from connector_runtime.rate_limiting import (
    MAX_IN_LAMBDA_SLEEP_SECONDS,
    ResumeAfterBackoffRequired,
    RetryAfterRateLimitPolicy,
)
from orchestration.step_functions.extraction_workflow import LambdaTimeoutWarning

_ORCHESTRATION_TF: Final[Path] = (
    Path(__file__).resolve().parent.parent.parent
    / "infrastructure"
    / "modules"
    / "orchestration"
    / "main.tf"
)


class TestPolicyHandsLongWaitsToTheStateMachine:
    def test_a_wait_above_the_threshold_raises_instead_of_sleeping(self) -> None:
        slept: list[float] = []
        policy = RetryAfterRateLimitPolicy(connection_id="hubspot", sleep=slept.append)
        policy.observe({"Retry-After": "45", "x-edl-response-status": "429"})
        with pytest.raises(ResumeAfterBackoffRequired) as caught:
            policy.acquire()
        assert slept == []
        assert caught.value.retry_after_seconds > MAX_IN_LAMBDA_SLEEP_SECONDS

    def test_the_signal_names_the_connection_so_one_source_does_not_stall_others(self) -> None:
        policy = RetryAfterRateLimitPolicy(connection_id="dialpad-west", sleep=lambda _: None)
        policy.observe({"Retry-After": "600", "x-edl-response-status": "429"})
        with pytest.raises(ResumeAfterBackoffRequired) as caught:
            policy.acquire()
        assert caught.value.connection_id == "dialpad-west"

    def test_a_short_wait_is_absorbed_rather_than_costing_a_state_transition(self) -> None:
        slept: list[float] = []
        policy = RetryAfterRateLimitPolicy(connection_id="hubspot", sleep=slept.append)
        policy.observe({"Retry-After": "1", "x-edl-response-status": "429"})
        policy.acquire()
        assert slept


class TestCheckpointCarriesWhatTheMachineNeeds:
    def _checkpoint(self, retry_after: float) -> LambdaTimeoutWarning:
        return LambdaTimeoutWarning(
            "checkpointed",
            run_id="run-1",
            partial_run_id="run-1-part1",
            source_id="hubspot",
            entity_id="hubspot-contact",
            records_written=1_000,
            checkpoint_watermark="2026-07-28T00:00:00+00:00",
            reason="rate_limit_backoff",
            retry_after_seconds=retry_after,
        )

    def test_the_resume_payload_is_json_serialisable(self) -> None:
        # It travels as the exception message and is parsed by States.StringToJson, so anything
        # that fails to serialise breaks the loop at runtime rather than at deploy.
        payload = self._checkpoint(30.0).to_resume_payload()
        assert json.loads(json.dumps(payload)) == payload

    def test_the_payload_carries_the_wait_the_machine_must_honour(self) -> None:
        assert self._checkpoint(30.0).to_resume_payload()["retry_after_seconds"] == 30.0

    def test_a_record_count_checkpoint_carries_no_wait(self) -> None:
        # Resume immediately: there is no provider asking us to slow down.
        assert self._checkpoint(0.0).to_resume_payload()["retry_after_seconds"] == 0.0

    def test_the_payload_names_the_resume_watermark_for_the_operator(self) -> None:
        # Not used to resume — the committed watermark does that — but an operator reading the
        # execution history needs to see where it got to.
        payload = self._checkpoint(0.0).to_resume_payload()
        assert payload["resume_watermark"] == "2026-07-28T00:00:00+00:00"
        assert payload["records_written"] == 1_000


class TestStateMachineHalfOfTheContract:
    """
    The ASL is HCL, so these are structural assertions on the rendered definition.

    They exist because the Python and ASL halves must agree on field names: a rename on either
    side produces a runtime failure inside a Step Functions execution, which is the slowest
    possible place to discover it.
    """

    @pytest.fixture(scope="class")
    def definition(self) -> str:
        return _ORCHESTRATION_TF.read_text(encoding="utf-8")

    def test_the_checkpoint_is_caught_and_parsed_rather_than_terminating(
        self, definition: str
    ) -> None:
        assert '"LambdaTimeoutWarning"' in definition
        assert "ParseCheckpoint" in definition
        assert "States.StringToJson($.checkpoint.Cause)" in definition

    def test_a_wait_state_absorbs_the_provider_delay(self, definition: str) -> None:
        assert "WaitForRateLimit" in definition
        assert 'SecondsPath = "$.resume.retry_after_seconds"' in definition

    def test_the_wait_reads_the_same_field_the_payload_writes(self, definition: str) -> None:
        # The one agreement that cannot be checked by any type system.
        payload_field = "retry_after_seconds"
        assert (
            payload_field
            in LambdaTimeoutWarning(
                "m",
                run_id="r",
                partial_run_id="r",
                source_id="s",
                entity_id="e",
                records_written=0,
                checkpoint_watermark="",
                reason="x",
            ).to_resume_payload()
        )
        assert f"$.resume.{payload_field}" in definition

    def test_the_loop_is_bounded(self, definition: str) -> None:
        # An unbounded resume loop against a permanently-throttling provider would spin forever
        # and bury the problem in execution history.
        assert "ExtractionResumeExhausted" in definition
        assert "max_extraction_resume_attempts" in definition

    def test_the_attempt_counter_is_incremented_and_seeded(self, definition: str) -> None:
        assert "States.MathAdd($.resume_attempts, 1)" in definition
        trigger = (
            Path(__file__).resolve().parent.parent
            / "pipeline_trigger"
            / "pipeline_trigger_handler.py"
        ).read_text(encoding="utf-8")
        assert '"resume_attempts": 0' in trigger, (
            "the state machine increments $.resume_attempts, so the trigger must seed it — "
            "States.MathAdd on a missing field fails the execution"
        )

    def test_the_resumed_task_receives_the_pinned_config(self, definition: str) -> None:
        # A resumed invocation must run under the same configuration version as the first, or the
        # two halves of one logical run disagree about a definition (DL-CFG-01).
        parse_block = re.search(r"ParseCheckpoint = \{.*?\n      \}", definition, re.DOTALL)
        assert parse_block is not None
        assert "pinned_config_versions" in parse_block.group(0)
