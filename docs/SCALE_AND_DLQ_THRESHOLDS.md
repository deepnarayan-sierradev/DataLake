# Scale targets and DLQ alarm thresholds

**Agreed 2026-07-29.** This is the single place the platform's sizing numbers live. Terraform
reads its thresholds from `infrastructure/modules/orchestration/per_stage_dlq.tf`'s
`dlq_alarm_defaults`; this document is the derivation behind those numbers. If you change one,
change both — a threshold nobody can justify is a threshold nobody will trust when it fires.

---

## The target

Development today has a handful of entities in `dev`. The numbers below are the **12-month
production** target, which is what the configuration is sized for.

| Dimension | Value |
|---|---|
| Tenants (prod, 12 months) | 10–20 |
| Sources per tenant | 5–12 |
| Entities (tables) per source | 100+ |
| Stages per pipeline run | 6 (extraction → transformation → entity resolution → analytics publish → twin build → serving store load) |

### Derived volume at 20 tenants

| Sources/tenant | Entities total | Runs/day | Stage executions/day | DLQ arrivals/day @0.5% |
|---|---|---|---|---|
| 5 | 10,000 | 10,000 | 60,000 | ~50 |
| 12 | 24,000 | 24,000 | 144,000 | ~120 |

So the design point is **~120 DLQ arrivals/day, ~20 per stage per day**. Everything below follows
from that number.

---

## Why `threshold = 0` on DLQ depth had to change

Until 2026-07-29 every per-stage DLQ alarm was `ApproximateNumberOfMessagesVisible > 0`, with the
rationale "a non-empty stage DLQ is a real failure that has already happened, so the threshold is
zero rather than a tolerance."

That is **correct in dev** — near-zero volume, any message is genuinely news — and **wrong in
prod**, where at ~120 arrivals/day the queue is essentially never empty. The alarm would sit in
ALARM permanently, would not clear until someone drained the queue, and would stop carrying
information. An alarm that never clears is as uninformative as one that never fires.

That is why the thresholds are now keyed by `environment` rather than being a constant.

**Depth is also the weakest available signal.** It cannot distinguish "1,000 messages arriving and
draining fine" from "one message stuck for three days". It is retained only as a capacity guard.

---

## The three signals

| Tier | Metric | Question it answers | SLO? |
|---|---|---|---|
| **Neglect** | `ApproximateAgeOfOldestMessage` | Is anything being ignored? | **Yes** — this is the SLO |
| **Spike** | `NumberOfMessagesSent` (Sum) | Is the failure rate abnormal right now? | No |
| **Backlog** | `ApproximateNumberOfMessagesVisible` | Are we failing to keep up? | No — capacity guard |

The neglect alarm self-clears when the queue drains, which a depth alarm does not. It is also the
one with a business meaning: it *is* the time-to-notice guarantee.

All three use `treat_missing_data = "notBreaching"` — an empty queue publishes no data, and that is
the healthy state. This is deliberately the **opposite** of the G6 absence alarms, where silence
means a control stopped running and therefore breaches.

### Stage latency classes

The neglect threshold is selected by a stage's `latency_class`, declared alongside its queue:

| Class | Stages | Rationale |
|---|---|---|
| `blocking` | extraction, transformation, entity_resolution | Downstream stages cannot run until this succeeds |
| `downstream` | analytics_publish, serving_store_load, twin_build, workflow_action | Consumes the golden record; a delay does not miss the business day |
| `realtime` | webhook_ingest, writeback | Near-real-time paths where lag is visible to the source system |

### Values

| Threshold | dev | staging | prod | Derivation |
|---|---|---|---|---|
| `oldest_blocking_seconds` | 900 | 1800 | **3600** | Same-business-day notice for a stage that blocks the pipeline |
| `oldest_downstream_seconds` | 3600 | 7200 | **14400** | 4h still lands inside the business day |
| `oldest_realtime_seconds` | 300 | 600 | **900** | 15 min is already a visible ingress lag |
| `arrival_spike_per_period` (5 min) | 0 | 10 | **50** | Baseline ≈20/stage/day ≈0.07 per 5-min period; 50 is a burst, not routine failure |
| `backlog_depth` | 0 | 200 | **2000** | ~100 days of normal accumulation — triage has stopped, or something is systemic |

**Replace the spike thresholds with a CloudWatch anomaly-detection band once ~2 weeks of real prod
baseline exists.** A static number will drift as tenants onboard; the value above is a
cold-start default, not a steady-state one.

Override per environment with the `dlq_alarm_overrides` map rather than editing the module, so the
sized defaults stay in one place.

---

## Per-tenant visibility without an alarm explosion

One shared DLQ per stage means one tenant's broken credential floods it and hides everyone else.
But per-entity alarms do not scale: 9 stages × 100 entities × 20 tenants ≈ 18,000 alarms, well past
the 5,000-per-account default.

The resolution:

- **9 stage queues**, not per-tenant queues — per-tenant would be 9 × T queues *and* 9 × T event
  source mappings.
- The DLQ processor emits a custom metric dimensioned `(Stage, TenantCode)`. Alarm on the **stage
  aggregate**; use the tenant dimension for dashboards and triage.
- A **"one tenant dominates"** alarm — one tenant exceeding ~80% of arrivals in a window with ≥20
  messages — because "one tenant is broken" and "the platform is broken" are different runbooks and
  are otherwise indistinguishable.

Total: ~30 alarms, **flat in tenant count**. Metric cardinality is 9 × T time series; at T=20 that
is 180, which is fine. Do not add `entity_id` as a dimension.

**Status:** the stage-aggregate alarms are implemented. The `(Stage, TenantCode)` metric and the
dominance alarm are **not yet implemented** — they need a producer in the DLQ processor, and the
alarm↔emitter reconciliation test is bidirectional, so the metric and its alarm must land together.

---

## DLQ processor sizing

| Setting | Before | dev | staging | prod | Reason |
|---|---|---|---|---|---|
| `batch_size` | 1 | 1 | 10 | **10** | The old justification was "clear per-message audit trail", but the audit trail is one DynamoDB row per message regardless of batch size — the two were conflated. At the target, a bad deploy failing one tenant's ~1,200 entities is 1,200 invocations at batch_size 1. `ReportBatchItemFailures` is enabled, so a partial failure re-drives only the failed messages. |
| `maximum_batching_window_in_seconds` | — | 20 | 20 | 20 | Lets a batch fill without adding meaningful latency to a message that is already a recorded failure |
| Reserved concurrency | **none** | 5 | 10 | **20** | Unbounded before 2026-07-29, so a flood could consume account concurrency and starve the pipeline it is trying to help. The pipeline trigger reserved 50; this reserved nothing. |

**Still outstanding:** the processor publishes one SNS notification *per message*. At the target,
one tenant's 1,200 entities failing means 1,200 pages. It should publish a digest per
`(stage, tenant)` per window — or stop publishing entirely and let these alarms be the single
notification path, since there are currently two paths for the same event.

---

## Known gaps this sizing exposed

These are recorded rather than fixed, and each is a real gap:

1. **Five of six pipeline stages enqueue nothing to any DLQ.**
   `RunCoordinator.enqueue_dlq_entry(...)` accepts a `failed_stage` argument but hardcodes
   `_DLQ_NAME = "EdlExtractionFailureDlq"`, and its only production caller is
   `orchestration/step_functions/extraction_workflow.py`. Transformation, entity resolution,
   analytics publish, twin build and serving store load failures reach the Step Functions
   execution history and the audit table, but no queue.

2. **The nine per-stage DLQs have no producer and no consumer.** The processor's event source
   mapping binds only to the single legacy `extraction_failure_dlq`, and no environment consumes
   the `stage_dlq_arns` output. Until (1) is fixed, the per-stage alarms above cannot fire and
   `maxReceiveCount = 3` never counts — it only decrements on *receive*.

3. **`maxReceiveCount` on a DLQ presumes a replaying consumer.** Today's processor records and
   notifies but never re-drives, so `EdlStageReplayExhausted` will stay empty even once (1) and (2)
   are fixed.

4. **Scheduled runs bypass the burst buffer.** `EdlPipelineTrigger.fifo` exists to absorb
   simultaneous schedule fires, but `scripts/seed_schedules.py` sets the schedule target to the
   **state machine ARN**, so EventBridge Scheduler calls `StartExecution` directly. The queue is
   fed only by the control-plane manual trigger route. Either point schedules at the queue or
   delete the queue and its Lambda — do not keep documenting a buffer that is not in the path.

5. **Concurrency wall.** Seeded crons are fixed times (`cron(0 2 * * ? *)`). At 20 tenants that is
   10,000–24,000 `StartExecution` calls in one minute, against an account-level token-bucket
   throttle — and those throttles surface as failed *scheduler* invocations, which land in the
   scheduler's own retry path and are **invisible in every DLQ dashboard above**. If extraction
   averages ~10 minutes, concurrent extraction Lambdas alone approach the default 1,000
   per-region concurrency limit before any other stage takes its share.

   Mitigations, in value order: **jitter the cron deterministically** from a hash of
   `{tenant}#{source}#{entity}` across a 4-hour window (240 one-minute slots → ~100 entities/min
   instead of 24,000 in one minute); request a concurrency limit increase and **partition it with
   reserved concurrency per function**; widen the window or stagger by tenant if the increase is
   refused.

---

## The one number still to be decided

The `oldest_blocking_seconds = 3600` value assumes **same-business-day** time-to-notice. If a
tenant SLA promises tighter curated-data freshness — say 4 hours — that threshold drops to roughly
15 minutes, and the jitter window has to shrink with it, which pushes directly back on the
concurrency plan in gap 5. That trade-off is a product decision, not an engineering one.
