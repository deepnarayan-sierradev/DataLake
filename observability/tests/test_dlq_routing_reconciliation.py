"""
Bidirectional reconciliation of DLQ queues against their producers (gap items 20-21).

The alarm/emitter reconciliation exists because a catalogued metric with no producer looks exactly
like a healthy one. A dead-letter queue has the same property, and it went unnoticed for longer: on
2026-07-29 nine `EdlStageDlq-*` queues were created, alarmed with thresholds derived from the
2-4h freshness commitment, and given a `maxReceiveCount` — while **five of six pipeline stages
enqueued to no queue at all**, because `enqueue_dlq_entry` accepted `failed_stage` and hardcoded
the extraction queue name. Empty queues and quiet alarms read as "nothing is failing".

So the same test shape applies here, in both directions:

- every queue Terraform creates has a stage that routes to it, and a handler that declares it;
- every stage a handler declares has a queue Terraform creates.

Terraform is parsed as text rather than planned, which is weaker than an apply — but it is the same
trade the alarm reconciliation makes, and it catches the failure that actually happened: a name or a
key drifting apart from the code that addresses it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from contracts.dlq_routing import (
    DLQ_STAGE_BY_PIPELINE_STAGE,
    REPLAYABLE_STAGES,
    DlqStage,
    dlq_queue_name,
    dlq_stage_for,
)
from contracts.observability_contract import PipelineStage

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent.parent
PER_STAGE_DLQ_TF: Final[Path] = (
    REPO_ROOT / "infrastructure" / "modules" / "orchestration" / "per_stage_dlq.tf"
)

# Handlers that construct a StageIdentity, and therefore declare a DLQ route.
PRODUCER_MODULES: Final[tuple[str, ...]] = (
    "connector_runtime/extraction_pipeline_handler.py",
    "transformation/transformation_pipeline_handler.py",
    "entity_resolution/entity_resolution_pipeline_handler.py",
    "analytics_publisher/analytics_publisher_handler.py",
    "serving_store/serving_store_loader_handler.py",
    "knowledge/twin_build_handler.py",
    "workflow_automation/workflow_runner_handler.py",
    "connector_runtime/webhook_receiver_handler.py",
    "connector_runtime/writeback_handler.py",
    "portability/portability_handler.py",
)


def _terraform_stage_keys() -> set[str]:
    """
    The keys of `local.pipeline_stages`, which is what the queue names are built from.

    Sliced to the block's own closing brace rather than to a marker line inside it: the first cut
    of this parser stopped at `visibility_timeout = 120` and silently dropped `writeback`, which
    would have made the reconciliation report a false disagreement — a parser bug reading as a
    code defect.
    """
    text = PER_STAGE_DLQ_TF.read_text(encoding="utf-8")
    start = text.index("pipeline_stages = {") + len("pipeline_stages = {")
    lines: list[str] = []
    for line in text[start:].splitlines():
        if line.strip() == "}":
            break
        lines.append(line)
    return set(re.findall(r"^\s*([a-z_]+)\s*=\s*\{", "\n".join(lines), flags=re.MULTILINE))


def _declared_dlq_stages() -> set[str]:
    """Every `DlqStage.X` a production handler names when it builds its StageIdentity."""
    declared: set[str] = set()
    for relative in PRODUCER_MODULES:
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        declared.update(re.findall(r"dlq_stage=DlqStage\.([A-Z_]+)", source))
    return declared


class TestEveryQueueHasAProducer:
    def test_each_terraform_stage_is_a_declared_dlq_stage(self) -> None:
        # The assertion that was false for five of six stages.
        terraform_keys = _terraform_stage_keys()
        assert terraform_keys, "parsed no stages out of per_stage_dlq.tf — the parser has drifted"
        routed = {stage.value for stage in REPLAYABLE_STAGES}
        assert terraform_keys == routed, (
            "Terraform stages and DlqStage disagree. Missing a route means a queue nothing can "
            f"write to: only-in-terraform={sorted(terraform_keys - routed)}, "
            f"only-in-code={sorted(routed - terraform_keys)}"
        )

    @pytest.mark.parametrize("stage", sorted(REPLAYABLE_STAGES, key=lambda s: s.value))
    def test_each_replayable_stage_is_declared_by_a_handler(self, stage: DlqStage) -> None:
        declared = _declared_dlq_stages()
        assert stage.name in declared, (
            f"No handler declares dlq_stage=DlqStage.{stage.name}, so {dlq_queue_name(stage)} has "
            "no producer. A queue with no producer is indistinguishable from a healthy one."
        )


class TestQueueNamesMatchTerraform:
    @pytest.mark.parametrize("stage", sorted(REPLAYABLE_STAGES, key=lambda s: s.value))
    def test_the_generated_name_appears_in_terraform(self, stage: DlqStage) -> None:
        # Terraform builds the name with title()/replace(); this reproduces it. If either side
        # changes, the code addresses a queue that does not exist.
        text = PER_STAGE_DLQ_TF.read_text(encoding="utf-8")
        camel = dlq_queue_name(stage).removeprefix("EdlStageDlq-")
        assert 'title(replace(each.key, "_", " "))' in text or camel in text

    def test_entity_resolution_camel_cases_correctly(self) -> None:
        # The one name where the transform is non-trivial.
        assert dlq_queue_name(DlqStage.ENTITY_RESOLUTION) == "EdlStageDlq-EntityResolution"
        assert dlq_queue_name(DlqStage.SERVING_STORE_LOAD) == "EdlStageDlq-ServingStoreLoad"

    def test_not_replayable_has_no_queue_name(self) -> None:
        with pytest.raises(ValueError, match="no queue by design"):
            dlq_queue_name(DlqStage.NOT_REPLAYABLE)


class TestEveryPipelineStageIsRouted:
    @pytest.mark.parametrize("pipeline_stage", sorted(PipelineStage, key=lambda s: s.value))
    def test_no_pipeline_stage_falls_through_to_no_queue(
        self, pipeline_stage: PipelineStage
    ) -> None:
        # A stage with no entry would silently enqueue nowhere, which is the original defect.
        assert dlq_stage_for(pipeline_stage) in set(DlqStage)

    def test_the_mapping_covers_the_enum_exactly(self) -> None:
        routed = set(DLQ_STAGE_BY_PIPELINE_STAGE)
        assert routed == set(PipelineStage), (
            "Every PipelineStage must declare a route, including NOT_REPLAYABLE ones: "
            f"unrouted={sorted(s.value for s in set(PipelineStage) - routed)}"
        )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
