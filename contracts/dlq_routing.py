"""
Which dead-letter queue a failed stage belongs to (DL-OPS-09, gap register items 20-21).

`RunCoordinator.enqueue_dlq_entry` has always taken a `failed_stage` argument and always
ignored it: `_resolve_dlq_url` hardcoded `EdlExtractionFailureDlq`. Its only production
caller was the extraction workflow, so **five of six pipeline stages enqueued to no queue
at all** — transformation, entity resolution, analytics publish, twin build and
serving-store load failures reached the Step Functions history and the audit table, and
nothing else. There was nothing to replay from and nothing for an alarm to observe, which
is why the nine `EdlStageDlq-*` queues sized and alarmed on 2026-07-29 were inert: correct
thresholds on queues no code could ever write to.

The argument already carried what was needed. This module is the mapping, in one place, so
a stage cannot be routed to the wrong queue or to none:

- `DlqStage` matches `local.pipeline_stages` in
  `infrastructure/modules/orchestration/per_stage_dlq.tf` key for key, asserted both ways
  by `observability/tests/test_dlq_routing_reconciliation.py` — in the same bidirectional
  style as the alarm/emitter reconciliation. No queue without a producer, no producer
  without a queue.
- `NOT_REPLAYABLE` is an affirmative "this stage has no queue, deliberately", not an absent
  value. A tenant deletion is the example: replaying it is meaningless, and the certificate
  already records the attempt. Stating it keeps a handler that simply forgot a build error.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from contracts.observability_contract import PipelineStage


class DlqStage(StrEnum):
    """A stage with its own replay queue; values match the Terraform `pipeline_stages` keys."""

    EXTRACTION = "extraction"
    TRANSFORMATION = "transformation"
    ENTITY_RESOLUTION = "entity_resolution"
    ANALYTICS_PUBLISH = "analytics_publish"
    SERVING_STORE_LOAD = "serving_store_load"
    TWIN_BUILD = "twin_build"
    WORKFLOW_ACTION = "workflow_action"
    WEBHOOK_INGEST = "webhook_ingest"
    WRITEBACK = "writeback"

    # Not a queue. A stage whose failure must not be automatically replayed — a deletion, an
    # export — declares this rather than leaving the field unset, so "no queue" is on the record.
    NOT_REPLAYABLE = "not_replayable"


# The legacy single queue, still created and still consumed. Extraction writes to it *as well as*
# its own per-stage queue until the processor's per-stage event-source mappings are applied and
# observed — switching a live failure path in one step leaves no way to compare the two.
LEGACY_EXTRACTION_DLQ: Final[str] = "EdlExtractionFailureDlq"


def dlq_queue_name(stage: DlqStage) -> str:
    """
    The SQS queue name for a stage, matching the Terraform name exactly.

    Terraform builds it as `title(replace(key, "_", " "))` with spaces stripped, so
    `entity_resolution` becomes `EdlStageDlq-EntityResolution`. Reproduced here rather than
    hand-listed: a hand-listed map is how a name drifts from the resource it addresses.
    """
    if stage is DlqStage.NOT_REPLAYABLE:
        raise ValueError(
            "DlqStage.NOT_REPLAYABLE has no queue by design; do not resolve a name for it."
        )
    camel = "".join(part.capitalize() for part in stage.value.split("_"))
    return f"EdlStageDlq-{camel}"


# Every `PipelineStage` a run can fail at, mapped to the queue a replay would restart from. The
# extraction workflow's fine-grained stages all restart extraction, because that is the unit of
# replay — there is no way to resume from "query build" alone.
DLQ_STAGE_BY_PIPELINE_STAGE: Final[dict[PipelineStage, DlqStage]] = {
    PipelineStage.CONFIGURATION_LOAD: DlqStage.EXTRACTION,
    PipelineStage.CREDENTIAL_RETRIEVAL: DlqStage.EXTRACTION,
    PipelineStage.METADATA_DISCOVERY: DlqStage.EXTRACTION,
    PipelineStage.QUERY_BUILD: DlqStage.EXTRACTION,
    PipelineStage.EXTRACTION: DlqStage.EXTRACTION,
    PipelineStage.RAW_WRITE: DlqStage.EXTRACTION,
    PipelineStage.SCHEMA_SNAPSHOT: DlqStage.EXTRACTION,
    PipelineStage.SCHEMA_DRIFT_EVALUATION: DlqStage.EXTRACTION,
    PipelineStage.WATERMARK_UPDATE: DlqStage.EXTRACTION,
    PipelineStage.TRANSFORMATION: DlqStage.TRANSFORMATION,
    PipelineStage.CURATED_PUBLISH: DlqStage.TRANSFORMATION,
    PipelineStage.ENTITY_RESOLUTION: DlqStage.ENTITY_RESOLUTION,
    PipelineStage.GOLDEN_RECORD_PUBLISH: DlqStage.ENTITY_RESOLUTION,
    PipelineStage.ANALYTICS_PUBLISH: DlqStage.ANALYTICS_PUBLISH,
    PipelineStage.TARGET_DB_LOAD: DlqStage.SERVING_STORE_LOAD,
    # Lifecycle stages: a failure to enqueue a DLQ entry must not enqueue a DLQ entry, and a replay
    # that fails to start is the operator's signal, not a queued message.
    PipelineStage.REPLAY_INITIATION: DlqStage.NOT_REPLAYABLE,
    PipelineStage.DLQ_ENQUEUE: DlqStage.NOT_REPLAYABLE,
    PipelineStage.RUN_COMPLETION: DlqStage.NOT_REPLAYABLE,
}


def dlq_stage_for(pipeline_stage: PipelineStage) -> DlqStage:
    """Route a pipeline stage to its replay queue; an unmapped stage is a build-time omission."""
    try:
        return DLQ_STAGE_BY_PIPELINE_STAGE[pipeline_stage]
    except KeyError as exc:
        raise KeyError(
            f"PipelineStage {pipeline_stage.value!r} has no DLQ routing. Add it to "
            "DLQ_STAGE_BY_PIPELINE_STAGE — a stage with no route fails silently into no queue."
        ) from exc


REPLAYABLE_STAGES: Final[frozenset[DlqStage]] = frozenset(
    stage for stage in DlqStage if stage is not DlqStage.NOT_REPLAYABLE
)
