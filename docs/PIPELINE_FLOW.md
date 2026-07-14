# Enterprise Data Lake — Full Pipeline Flow

> **Last updated:** 2026-07-14

> **Current status:** Salesforce, MySQL RDS, Sage Intacct, and Sage X3 connectors are fully
> implemented; Salesforce and MySQL RDS have real credentials in dev and have run end-to-end.
> NetSuite is fully implemented but its Secrets Manager credential is still an empty shell —
> never invoked in dev. The serving store (Stage 16) is fully implemented but not yet deployed to
> any environment. For exactly what's deployed and running where, see `docs/PLATFORM_STATUS.md` —
> this document describes the pipeline's design and mechanics, not its current live/dormant status
> per stage.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Data Layer Definitions](#2-data-layer-definitions)
3. [End-to-End Pipeline Flow](#3-end-to-end-pipeline-flow)
4. [Stage-by-Stage Reference](#4-stage-by-stage-reference)
   - [Stage 1 — Event Scheduling](#stage-1--event-scheduling)
   - [Stage 2 — Step Functions Orchestration](#stage-2--step-functions-orchestration)
   - [Stage 3 — Configuration Load](#stage-3--configuration-load)
   - [Stage 4 — Credential Retrieval](#stage-4--credential-retrieval)
   - [Stage 5 — Metadata Discovery](#stage-5--metadata-discovery)
   - [Stage 6 — Query Construction](#stage-6--query-construction)
   - [Stage 7 — Extraction](#stage-7--extraction)
   - [Stage 8 — Schema Snapshot](#stage-8--schema-snapshot)
   - [Stage 9 — Schema Drift Evaluation](#stage-9--schema-drift-evaluation)
   - [Stage 10 — Raw Layer Write](#stage-10--raw-layer-write)
   - [Stage 11 — Watermark Update](#stage-11--watermark-update)
   - [Stage 12 — Transformation (Raw → Curated)](#stage-12--transformation-raw--curated)
   - [Stage 13 — Entity Resolution](#stage-13--entity-resolution)
   - [Stage 14 — Golden Record Publish](#stage-14--golden-record-publish)
   - [Stage 15 — Analytics Layer Publish](#stage-15--analytics-layer-publish)
   - [Stage 16 — Serving Store Load](#stage-16--serving-store-load)
5. [Field Mapping System](#5-field-mapping-system)
6. [Entity Resolution Config System](#6-entity-resolution-config-system)
7. [Failure Handling and Replay](#7-failure-handling-and-replay)
8. [Version Control and Rollback](#8-version-control-and-rollback)
9. [Manual Trigger Checklist](#9-manual-trigger-checklist)
10. [Pre-Deployment Verification](#10-pre-deployment-verification)
11. [Technology Reference](#11-technology-reference)

---

## 1. Architecture Overview

The Enterprise Data Lake platform ingests data from multiple source systems (Salesforce, NetSuite, MySQL RDS), transforms it through three distinct data lake layers, resolves cross-source entity identity, and delivers trusted canonical records to analytics and serving stores.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  SOURCE SYSTEMS                                                              │
│  Salesforce CRM ✅  │  NetSuite ERP 🟡  │  MySQL RDS ✅  │  Sage ERP (Intacct + X3) ✅  │
└────────────────────────────────┬─────────────────────────────────────────────┘
                     (🟡 = code-complete, not yet confirmed activated — see Dev status above)
                                 │ full/incremental extraction (watermark-based)
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  ORCHESTRATION LAYER                                                         │
│  EventBridge Scheduler → SQS FIFO Queue → Pipeline Trigger Lambda           │
│  → Step Functions (chained 5-stage state machine)                            │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 │
              ┌──────────────────┼──────────────────────┐
              ▼                  ▼                       ▼
     Config (DynamoDB)    Watermark (DynamoDB)   Credentials (Secrets Mgr)
              │
              ▼
┌─────────────────────────────────┐
│  S3 RAW LAYER                   │
│  Immutable, append-only         │
│  Source field names preserved   │
│  Parquet + Object Lock          │
└──────────────┬──────────────────┘
               │ field mapping (v1.json per source/entity)
               │ quality evaluation
               ▼
┌─────────────────────────────────┐
│  S3 CURATED LAYER               │
│  Standardised per-source        │
│  Canonical field names          │
│  Quality-checked, masked PII    │
└──────────────┬──────────────────┘
               │ entity resolution (cross-source matching)
               │ survivorship policy → golden records
               │ lineage records emitted
               ▼
┌─────────────────────────────────┐
│  S3 ANALYTICS LAYER             │
│  canonical/ — golden records,   │
│    one Parquet file per run_id  │
│  analytics/ — same golden       │
│    records, ER-internal fields  │
│    stripped, BI-facing, one     │
│    file per day (overwritten)   │
│  Glue-catalogued, Athena-ready  │
└──────────────┬──────────────────┘
               │
               ▼
┌─────────────────────────────────┐
│  SERVING STORE (MySQL/Postgres/ │
│  SQL Server/Azure SQL) — coded, │
│  not yet deployed. Operational  │
│  APIs, Applications             │
└─────────────────────────────────┘
```

---

## 2. Data Layer Definitions

| Layer | Purpose | Storage | Format | Mutability |
|---|---|---|---|---|
| **Raw** | Exact copy of source data, no transformation | `edl-raw-{account_id}` S3 | Parquet (large_utf8 columns) | Immutable — Object Lock GOVERNANCE |
| **Curated** | Per-source standardised data with canonical field names, type-cast, quality-checked, PII masked. For incremental entities with `primary_key_field` set, each partition holds the **full current state** (SCD Type 1 merge — not just the day's delta) | `edl-curated-{account_id}` S3 | Parquet (Snappy) | Full-load entities: append-only per run_id. Incremental entities with merge: full snapshot per run_id (overwrites previous state within the partition) |
| **Analytics** | Consumption-optimised, Glue-catalogued datasets for Athena/BI. Two distinct prefixes, both tenant-prefixed (`{tenant_code}/...`): `canonical/{entity_type}/` — golden/mastered records straight from entity resolution, one Parquet file per run under a `run_id=` partition; `analytics/{entity_type}/` — the same golden records with internal entity-resolution-only fields stripped, republished by the Analytics Layer Publish stage as the BI-facing dataset | `edl-analytics-{account_id}` S3 | Parquet (Snappy) | `canonical/`: append-only, one file per `run_id`. `analytics/`: **not** append-only — partitioned only by `analytics_date=`, no `run_id`, so a second run on the same UTC day overwrites the first run's file for that day |
| **Serving Store** | Optional operational store for low-latency API and application reads. Code-complete (`serving_store/` module, four engine adapters), not yet deployed in any environment | MySQL RDS, PostgreSQL, SQL Server, or Azure SQL (database/schema-per-tenant; private VPC for platform-provisioned engines, BYO-DB for Azure SQL) | SQL rows | Idempotent hash-diff upsert per engine (`_row_hash`/`_synced_at` columns) — first sync per tenant/entity is a full backfill |

### Multi-tenancy — the canonical isolation model

Every stage is tenant-scoped via a `tenant_code` parameter — default `"demo"`, sourced from
`contracts.identifier_policy.DEFAULT_TENANT_CODE` and validated on every call via
`validate_tenant_code()` / `TENANT_CODE_PATTERN`. **This is the single canonical reference for how
tenant isolation actually works, layer by layer** — every other doc in this repo that discusses
tenant isolation (the incident runbook, the glossary, the developer guide) links here rather than
re-deriving its own version.

| Layer | Isolation mechanism | Genuinely enforced, or app-level only? |
|---|---|---|
| S3 — raw layer | `{source}/{entity_id}/...` — **not tenant-prefixed today** | Not isolated — a real, open gap (see `docs/KNOWN_GAPS_AND_ROADMAP.md`) |
| S3 — curated layer | `{tenant_code}/curated/{domain}/{entity_id}/...` | App-level (write-path convention); no S3 bucket-policy `Condition` enforces it yet |
| S3 — analytics layer | `{tenant_code}/analytics/{entity_type}/...` and `{tenant_code}/canonical/{entity_type}/...` | App-level, same caveat |
| S3 — schema snapshots | `{tenant_code}/{source_id}/{entity_id}/{schema_version}/...` | App-level, same caveat |
| DynamoDB — `entity_type_registry` | Hash key is `tenant_code` itself | **Genuinely key-level isolated** |
| DynamoDB — `watermark_repository` | Hash key is `tenant_scoped_key(tenant_code, source_id)` | **Genuinely key-level isolated** |
| DynamoDB — `entity_extraction_config` | Key is `(source_id, entity_id)`; `tenant_code` is a plain attribute | App-level guard only (`_enforce_tenant_match`) — mismatches 409, not a silent leak |
| DynamoDB — `run_audit_log` | Key is `(run_id, stage)`, not tenant-keyed | App-level guard only — reads for another tenant's `run_id` return 404, not 403 |
| DynamoDB — `source_onboarding_registry` | Key is `source_id` only | Models sources, not per-tenant state — no tenant dimension by design |
| Secrets Manager | One shared credential per connector type (`edl/sources/{source_id}/credentials`) | **Not isolated at all** — same secret across every tenant using that connector |
| Control-plane API | `_authorize_path_tenant` cross-checks the path's `tenant_code` against the JWT claim | App-level only, fails closed (401/403) |
| Glue / Athena | Two shared databases (`edl_curated`, `edl_analytics`); table names prefixed `{tenant_code}_{entity_type}` | **Not isolated at all** — naming convention only, no per-tenant database/LF-Tags/data-cell filters |
| Serving store | Database-per-tenant (MySQL) / schema-per-tenant (Postgres, SQL Server, Azure SQL), enforced by the database engine's own GRANT model | **Genuinely isolated** at the credential level — see Stage 16 below for the separate network-reachability gap |

Where the state machine threads tenant identity through: `infrastructure/modules/orchestration/main.tf`
passes `"tenant_code.$" = "$.tenant_code"` explicitly into the Transformation, Entity Resolution,
Analytics Publish, and Serving Store Load task `Parameters`; both the entity resolution and
analytics publisher Lambda handlers treat `tenant_code` as a **required** Step Functions input
field — `_validate_event()` raises `ValueError` and fails the run closed if it's missing or
malformed, rather than silently defaulting to another tenant's identity.

For everything still open in this model (no IAM enforcement anywhere, Secrets Manager sharing,
the raw-layer gap, Glue/Athena's wildcard grant), see `docs/KNOWN_GAPS_AND_ROADMAP.md`. Regression
coverage: `tests/test_tenant_isolation.py`. Incident response: `docs/PRODUCTION_INCIDENT_RUNBOOK.md`
→ "Suspected Cross-Tenant Data Incident."

---

## 3. End-to-End Pipeline Flow

```
EventBridge Scheduler (cron per entity)
          │
          ▼
SQS FIFO Queue  ── absorbs burst; exactly-once per entity per tick
  (EdlPipelineTrigger.fifo)
          │  (drains via Pipeline Trigger Lambda, reserved_concurrency=50)
          ▼
Step Functions: START EXECUTION
          │
          ├─────────────────────────────────────────────────────────────┐
          │                                                             │
          ▼                                                             │
  ┌─────────────────────────────────────────────────────────┐         │
  │  STAGE A — EXTRACTION WORKFLOW (Lambda)                  │         │
  │                                                          │         │
  │  1. Load entity config from DynamoDB                     │         │
  │  2. Retrieve source credentials from Secrets Manager     │         │
  │  3. Discover queryable fields (metadata API)             │         │
  │  4. Build parameterized extraction query                 │         │
  │  5. Execute extraction → write raw Parquet to S3         │         │
  │  6. Persist schema snapshot to S3                        │         │
  │  7. Evaluate schema drift vs previous snapshot           │         │
  │  8. Validate raw record count                            │         │
  │  9. Advance watermark (full success) — or partial         │         │
  │     watermark + checkpoint audit rec. on timeout warning  │         │
  │  10. Emit TRANSFORMATION_TRIGGER stage event             │         │
  └──────────────────────┬──────────────────────────────────┘         │
                         │                                             │
       LambdaTimeoutWarning raised? (mid-run checkpoint)               │
                    │ yes → ExtractionCheckpointed (Succeed,           │
                    │        non-fatal). Partial watermark already     │
                    │        committed; remaining window NOT           │
                    │        processed. Auto-resume NOT implemented —  │
                    │        needs a manual re-trigger.                │
                    │ no ↓                                             │
           transformation_blocked=true?                               │
                    │ yes → END (drift alert sent)                    │
                    │ no ↓                                             │
          ▼                                                            │
  ┌─────────────────────────────────────────────────────────┐         │
  │  STAGE B — TRANSFORMATION PIPELINE (Lambda)             │         │
  │                                                          │         │
  │  1. Load raw Parquet records from S3                     │         │
  │  2. Load field mapping rule set from S3                  │         │
  │  3. Apply field mappings (rename/cast/concat/mask)       │         │
  │  4. Apply PII masking (classification policy)            │         │
  │  5. Evaluate quality policy (on delta records only)      │         │
  │  6. SCD Type 1 merge with previous curated state         │         │
  │     (only for incremental entities with primary_key_field)│         │
  │  7. Write full current-state Parquet to curated layer    │         │
  │  8. Register dataset in Glue Catalog                     │         │
  │  9. Emit lineage record                                  │         │
  └──────────────────────┬──────────────────────────────────┘         │
                         │                                             │
           quality_blocked=true?                                      │
                    │ yes → END (alert sent, raw preserved)           │
                    │ no ↓                                             │
          ▼                                                            │
  ┌─────────────────────────────────────────────────────────┐         │
  │  STAGE C — ENTITY RESOLUTION (Lambda)                   │         │
  │                                                          │         │
  │  1. Load curated records for all sources of entity type  │         │
  │  2. Apply matching rules (deterministic + probabilistic) │         │
  │  3. Apply survivorship policy (which source wins/field)  │         │
  │  4. Produce golden records                               │         │
  │  5. Write canonical (mastered) records to analytics S3 layer           │         │
  │     (s3://{analytics-layer}/{tenant_code}/canonical/...)          │         │
  │  6. Emit match statistics + lineage                      │         │
  └──────────────────────┬──────────────────────────────────┘         │
                         │                                             │
                         ▼                                            │
  ┌─────────────────────────────────────────────────────────┐         │
  │  STAGE D — ANALYTICS LAYER PUBLISH (Lambda)             │         │
  │                                                          │         │
  │  1. Read golden records from Stage C's canonical_prefix  │         │
  │  2. Strip entity-resolution-internal fields (_record_id, │         │
  │     _source_id, contributing_source_records, etc.)      │         │
  │  3. Write BI-facing Parquet to analytics/ (one file per  │         │
  │     day — no run_id; overwrites same-day re-runs)        │         │
  │  4. Register/update Glue Catalog table + day's partition │         │
  │  5. No lineage record is emitted at this stage today     │         │
  │     (see Stage 15 reference below)                       │         │
  └──────────────────────┬──────────────────────────────────┘         │
                         │                                             │
                         ▼                                            │
  ┌─────────────────────────────────────────────────────────┐         │
  │  STAGE E — SERVING STORE LOAD (Lambda)                  │         │
  │  code-complete, not yet deployed in any environment      │         │
  │                                                          │         │
  │  1. Read analytics Parquet from S3                       │         │
  │  2. Retrieve DB credentials from Secrets Manager         │         │
  │  3. CREATE TABLE IF NOT EXISTS (schema inferred)         │         │
  │  4. Engine-specific upsert via hash-diff (idempotent)     │         │
  │  5. Emit load metrics                                    │         │
  └──────────────────────┬──────────────────────────────────┘         │
                         │                                             │
                         ▼                                            │
              PIPELINE COMPLETE ◄───────────────────────────────────┘
              (all stages succeeded)

  ── Failure path (any Stage A–E Task, retries exhausted) ──────────────
  {Stage}Failed (Fail state)
          │
          ▼
  SQS DLQ (EdlExtractionFailureDlq)
          │
          ▼
  dlq_processor Lambda (event source mapping, batch_size=1)
          │  1. Validate DLQ message (Pydantic)
          │  2. Write audit record (RunStatus.FAILED) to run audit log
          │  3. Emit SNS alert (ALERT_SNS_TOPIC_ARN)
          │  4. Optional auto-replay → re-invoke Step Functions
          │     (AUTO_REPLAY env var, default false — operator-driven)
          ▼
  Operator reviews → manual replay via scripts/trigger_extraction.py --is-replay
```

---

## 4. Stage-by-Stage Reference

### Stage 1 — Event Scheduling

**Component:** Amazon EventBridge Scheduler → Amazon SQS FIFO Queue → Pipeline Trigger Lambda  
**Trigger:** Cron expression per source/entity (configured per entity at runtime via `ExtractionScheduleClient`)  
**Purpose:** Fires extraction runs on schedule without manual intervention. The SQS FIFO queue absorbs simultaneous schedule fires (80–100 entities at launch) and feeds them to the Pipeline Trigger Lambda at a controlled rate, preventing Lambda concurrency spikes.  
**Key behaviour:**
- One EventBridge schedule per entity (e.g. `salesforce--salesforce-account`)
- Schedules target the SQS FIFO trigger queue, **not** Step Functions directly
- Message group ID = `{source_id}-{entity_id}` — per-entity ordering; one active execution per entity at a time
- Content-based deduplication: duplicate fires within 5-minute window are dropped automatically
- Pipeline Trigger Lambda (`reserved_concurrent_executions=50`) starts Step Functions executions at a controlled rate from the queue
- Schedules are data — managed at runtime via `ExtractionScheduleClient`, not in Terraform
- Passes `source_id`, `entity_id`, `environment`, `connector_params` as Step Functions execution input

**What can go wrong:** Schedule disabled; IAM EventBridge role missing `sqs:SendMessage` on trigger queue; SQS visibility timeout < Lambda timeout (causes duplicate execution starts); trigger Lambda reserved concurrency exhausted (messages accumulate in queue, drain resumes automatically).

---

### Stage 2 — Step Functions Orchestration

**Component:** AWS Step Functions Standard Workflow (staging/prod) or Express Workflow (dev)  
**Purpose:** Chains all five Lambda stages with explicit branching logic, retry policies, and failure routing.  
**Key behaviour:**
- Reads `transformation_blocked` from extraction output — skips stages B–E if breaking drift detected
- Reads `is_publication_blocked` from transformation output — skips entity resolution and downstream if quality blocks publication
- Retries transient Lambda errors with exponential backoff (3 attempts, 10s initial, 2× backoff)
- Terminal failures route to a per-stage `Fail` state (`ExtractionFailed`, `TransformationFailed`, `EntityResolutionFailed`, `AnalyticsPublishFailed`) and enqueue to DLQ
- The `ExecuteExtraction` state's `Catch` block matches `LambdaTimeoutWarning` (a mid-run checkpoint) *before* the generic `States.ALL` catch-all — first match wins in ASL, so a checkpoint does **not** fall through to `ExtractionFailed`/the DLQ. It routes instead to a terminal `ExtractionCheckpointed` `Succeed` state: non-fatal, partial watermark already committed, remaining window not yet processed. Automatic resume from a checkpoint is **not yet implemented** (documented as a gap in `extraction_workflow.py`'s own module docstring) — it needs a manual re-trigger.
- DLQ messages (`EdlExtractionFailureDlq`) are consumed by the `dlq_processor` Lambda, which writes a `RunStatus.FAILED` audit record, emits an SNS alert, and optionally auto-replays (`AUTO_REPLAY` env var, default `false`)

**Branching logic:**

```
Extraction raises LambdaTimeoutWarning (mid-run checkpoint)?
  └─ yes → ExtractionCheckpointed (Succeed, non-fatal) — partial watermark
           committed, remaining window NOT processed. Auto-resume NOT
           implemented (documented gap) — needs a manual re-trigger.
  └─ no  → continue below

Extraction succeeded?
  └─ transformation_blocked=true  → STOP (drift alert fired)
  └─ transformation_blocked=false → Transformation

Transformation succeeded?
  └─ is_publication_blocked=true  → STOP (quality alert fired)
  └─ curated_s3_prefix=null       → STOP as success (0 records extracted — TransformationCompleteNoRecords)
  └─ otherwise                    → Entity Resolution

Entity Resolution → Analytics Publish → Serving Store Load → COMPLETE

Any stage's Task fails after retries exhausted (States.ALL)?
  └─ yes → {Stage}Failed (Fail state) → SQS DLQ → dlq_processor Lambda
           → audit record + SNS alert → optional auto-replay (default off)
```

---

### Stage 3 — Configuration Load

**Component:** `ConfigurationRepositoryClient` (DynamoDB backend)  
**Purpose:** Loads `EntityExtractionConfig` for the requested source/entity. Validates config before any AWS or source API call is made.  
**Key fields read:** `load_type`, `watermark_field`, `extraction_window_days`, `field_mode`, `include_fields`, `exclude_fields`, `output_format`, `primary_key_field`, `soft_delete_field`  
**Tenant scoping:** `load_config()` takes a `tenant_code` parameter (default `demo`); the loaded record's own `tenant_code` is cross-checked against the caller's — see [Multi-tenancy](#multi-tenancy).  
**Failure behaviour:** Raises `ConfigurationNotFoundError` → pipeline aborts, DLQ entry created.

---

### Stage 4 — Credential Retrieval

**Component:** AWS Secrets Manager  
**Purpose:** Retrieves short-lived source credentials (OAuth tokens, API keys, DB passwords). Credentials never appear in code, environment variables, or logs.  
**Secret path pattern:** `edl/sources/{source}/credentials`  
**Failure behaviour:** Raises credential error → classified as `DETERMINISTIC_INVALID_CREDENTIALS` → no retry.

---

### Stage 5 — Metadata Discovery

**Component:** Connector-specific metadata client (`SalesforceMetadataDiscoveryClient`, `NetSuiteMetadataAdapter`, `MySqlSchemaIntrospectionClient`, `SageIntacctMetadataClient`, `X3MetadataClient`)  
**Purpose:** Discovers all queryable fields from the source at runtime — no hardcoded schema. Produces a `FieldContract` used by query builder and schema snapshot.  
**Key output:** `FieldContract` — list of `FieldDescriptor` objects (name, type, precision, nullable, queryable flags)  
**Failure behaviour:** Raises metadata error → pipeline aborts.

---

### Stage 6 — Query Construction

**Component:** `SalesforceSoqlQueryBuilder`, `NetSuiteIncrementalQueryPlanner`, MySQL parameterized query, `SageIntacctQueryEngine` (JSON-POST), `X3QueryEngine` (OData v4 GET)  
**Purpose:** Builds a parameterized extraction query incorporating watermark bounds for incremental loads. Values are **never string-interpolated** — always bound as parameters (SQL injection prevention).  
**Key output:** `QueryContract` — `query_text` with named placeholders + `query_parameters` dict

---

### Stage 7 — Extraction

**Component:** `SalesforceBulkQueryJobController` (Bulk API 2.0), `NetSuiteConnector` (SuiteQL REST), `MySqlRdsConnector` (pymysql), `SageConnector` (Strategy pattern — dispatches to Intacct JSON-POST or X3 OData v4 GET based on `connector_params.sage_product`)  
**Purpose:** Executes the extraction query, streams records, writes raw Parquet to S3.  
**S3 partition scheme:** `s3://{bucket}/{source}/{entity_id}/extraction_date={YYYY-MM-DD}/run_id={run_id}/data.parquet` — **not tenant-prefixed** today; this is a real, tracked gap (see [Multi-tenancy](#multi-tenancy)), not an oversight.  
**Key properties:**
- All column values stored as `large_utf8` strings — no type loss, max compatibility
- Records written in chunks (50,000 per file for large volumes)
- `metadata.json` written alongside each Parquet file

---

### Stage 8 — Schema Snapshot

**Component:** `SchemaSnapshotRepository`  
**Purpose:** Persists the current field schema to S3 as an immutable snapshot after every successful run. Used by drift evaluator to compare against the next run's schema.  
**S3 path:** `s3://{bucket}/{tenant_code}/{source_id}/{entity_id}/{schema_version}/{extraction_date}.json` — tenant-prefixed (default `tenant_code="demo"`); see [Multi-tenancy](#multi-tenancy).  
**Latest pointer:** `{tenant_code}/{source_id}/{entity_id}/latest.json` updated after each write (avoids S3 listing latency).

---

### Stage 9 — Schema Drift Evaluation

**Component:** `SchemaDriftEvaluator`  
**Purpose:** Compares current schema snapshot against the previous one. Produces a `DriftReport` with field-level change classification.  

| Classification | Meaning | Pipeline action |
|---|---|---|
| `NO_DRIFT` | Schema unchanged | Continue normally |
| `NON_BREAKING` | New nullable field added | Continue, alert downstream consumers |
| `POTENTIALLY_BREAKING` | Precision/scale/length changed | Continue, alert |
| `BREAKING` | Field removed, type changed, non-nullable field added | **Stop pipeline**, alert, raw data preserved |

---

### Stage 10 — Raw Layer Write

**Component:** `*RawLayerWriter` per source  
**Purpose:** Validates extracted record count, writes S3 partition audit record.  
**Guarantees:** Object Lock GOVERNANCE mode — files cannot be overwritten or deleted during retention period. Every run produces a unique `run_id` partition.

---

### Stage 11 — Watermark Update

**Component:** `WatermarkRepository`  
**Purpose:** Advances the watermark to `upper_watermark` of the completed extraction window. Uses optimistic concurrency (DynamoDB `ConditionExpression` on `version`) to prevent concurrent runs from corrupting state. `get_watermark()` / `advance_watermark()` both take a `tenant_code` parameter (default `demo`) — see [Multi-tenancy](#multi-tenancy).  
**Critical rule:** Watermark advances on full success — but that is only half the invariant now. A mid-run **checkpoint** (`LambdaTimeoutWarning` — see [Stage 2](#stage-2--step-functions-orchestration)) also commits a **partial** watermark advance plus a distinct `'{run_id}-partN'` audit record, even though the extraction did not fully complete. Any true *failure* (as opposed to a checkpoint) at any earlier stage leaves the watermark unchanged, enabling safe replay.

---

### Stage 12 — Transformation (Raw → Curated)

**Component:** `TransformationPipeline`  
**Purpose:** Reads raw Parquet, applies field mappings, evaluates quality, and writes canonical records to the curated layer. For incremental entities with `primary_key_field` set, performs an SCD Type 1 merge to ensure the curated partition always holds the full current state.

**Field mapping system** (see also §5):
- Rule set loaded from S3: `s3://{bucket}/field-mappings/{source_id}/{entity_id}/{version}.json`
- If no rule set exists, records pass through as-is (identity mode — logged as warning)
- Rules applied per record: rename, concat, date_format, cast, boolean, mask

**Quality evaluation (runs on delta records only):**
- `null_check` — required fields must be non-null
- `range_check` — numeric bounds validation
- `pattern_check` — regex match
- `allowed_values` — enum validation
- `WARNING` severity: publication continues, violations logged
- `BLOCKING` severity: publication halted, downstream paused

**SCD Type 1 merge (incremental entities with `primary_key_field` set):**
- After quality check, the transformation loads the previous curated partition for this entity.
- Delta records (today's extraction) are merged into the previous state using `primary_key_field` as the upsert key.
- The **full merged result** (not just the delta) is written to the new curated partition.
- This ensures entity resolution always sees complete data regardless of extraction granularity.
- **Tombstone soft-delete:** When `soft_delete_field` is `None` (default), deleted records are **never removed** — they persist in the curated layer with their deletion flag (e.g. `is_deleted=True`). BI queries filter `WHERE is_deleted = false` to see only active records.
- If `soft_delete_field` is set to a canonical field name, records where that field is truthy are physically removed from the merged state.
- Full-load entities (`primary_key_field=None`) are unaffected — pipeline behaves identically to before.

**Outputs:**
- Curated Parquet: `s3://{bucket}/{tenant_code}/curated/{domain}/{entity_id}/curated_date={date}/run_id={run_id}/data.parquet` — tenant-prefixed via `CuratedLayerWriter.write()`'s `tenant_code` parameter (default `demo`); see [Multi-tenancy](#multi-tenancy)
- Quality report: `s3://{bucket}/quality-reports/{source_id}/{entity_id}/{run_id}/quality-report.json`
- Glue Catalog table registered

---

### Stage 13 — Entity Resolution

**Component:** `EntityResolutionEngine`, `MatchRuleEngine`, `ResolutionConfigRegistry`  
**Purpose:** Matches records for the same entity type across multiple source systems. Answers: "Is Salesforce Account SF:001 the same company as NetSuite Customer NS:C-4421?"  
**Matching strategies:** Deterministic (exact ID match, email match) and probabilistic (name similarity, address normalisation)  
**Input:** Curated records from all sources for one entity type  
**Output:** Match clusters — groups of source records that represent the same real-world entity

**Config-driven matching (no hardcoded rules):**  
Match rules and survivorship policies are loaded at runtime from S3 via `ResolutionConfigRegistry`. No match threshold, field weight, or source priority is hardcoded in Python.

```
s3://{curated-bucket}/entity-resolution/{entity_type}/match_rules_{version}.json
s3://{curated-bucket}/entity-resolution/{entity_type}/survivorship_{version}.json
s3://{curated-bucket}/entity-resolution/{entity_type}/latest.json  ← version pointer
```

**Defined entity types** (`entity_resolution/entity_type_registry.py`, `ENTITY_ID_TO_TYPE` / `ENTITY_TYPE_SOURCES` — these are seed defaults for the `demo` tenant; other tenants can register their own via `EntityTypeRegistryClient.register_entity_type()` with no redeploy required):

| Entity type | Sources merged | Output prefix |
|---|---|---|
| `company` | Salesforce Account + NetSuite Customer + Sage Intacct Customer + Sage X3 Customer | `{tenant_code}/canonical/company/` |
| `person` | Salesforce Contact | `{tenant_code}/canonical/person/` |
| `supplier` | Sage Intacct Vendor + Sage X3 Supplier | `{tenant_code}/canonical/supplier/` |
| `ar_invoice` | Sage Intacct AR Invoice | `{tenant_code}/canonical/ar_invoice/` |
| `ap_bill` | Sage Intacct AP Bill | `{tenant_code}/canonical/ap_bill/` |
| `contract` | MySQL RDS Contracts | `{tenant_code}/canonical/contract/` |
| `opportunity` | Salesforce Opportunity | `{tenant_code}/canonical/opportunity/` |
| `sales-contract` | Salesforce Contract | `{tenant_code}/canonical/sales-contract/` |
| `contract-term` | MySQL RDS ContractTerms | `{tenant_code}/canonical/contract-term/` |

`sales-contract` and `contract` are deliberately kept as separate entity types rather than merged, even though both represent "contracts" — Salesforce Contract and MySQL RDS Contracts share no common key, and forcing a fuzzy name/account match to merge them risks silently combining unrelated records (see the comment above `ENTITY_ID_TO_TYPE` in `entity_type_registry.py`).

---

### Stage 14 — Golden Record Publish

**Component:** `GoldenRecordPublisher` (`entity_resolution/canonical_record_publisher/canonical_record_publisher.py`), `GoldenRecordSurvivorshipPolicy`, `ResolutionConfigRegistry`  
**Purpose:** Applies survivorship rules to each match cluster to produce one trusted record per real-world entity.

**Known duplication (tracked, not yet cleaned up):** there are **two** near-identical implementations of this class — `entity_resolution/canonical_record_publisher/canonical_record_publisher.py` and `entity_resolution/golden_record_publisher/golden_record_publisher.py` — both defining a `GoldenRecordPublisher` class with the same `publish()` logic. `entity_resolution/entity_resolution_pipeline_handler.py` (the actual Step Functions Lambda handler) imports from **`canonical_record_publisher`** — that is the file on the live code path. `golden_record_publisher.py` is not wired into the Lambda handler; treat it as legacy/duplicate rather than a second production path. The shared logic both files used to duplicate (Parquet-list flattening, decision-audit serialisation, lineage emission) was since extracted into `entity_resolution/publishing_shared.py`, but the two top-level classes themselves were not yet consolidated.

**Survivorship strategies per field:**
- `source_priority` — prefer the value from the highest-ranked source (e.g. `sage` > `netsuite` > `salesforce` for `credit_limit` in the `company` survivorship policy — see the concrete example in [§6](#6-entity-resolution-config-system))
- `most_recent` — prefer the value with the latest timestamp (e.g. `last_modified_date`)
- `first_non_null` — the policy-wide `default_strategy`; use first available value in source priority order

**Field provenance tracking:**  
Every golden record includes a `field_provenance` column — a JSON map documenting which source system won for each output field (via the survivorship rules above). This enables instant source attribution queries in Athena (e.g. `json_extract_scalar(field_provenance, '$.full_name')`) or the Serving Store without re-computation.

**System fields automatically added:**
- `golden_id` — deterministic ID stable across re-runs (derived from `source_field` + `entity_type` + sorted contributing IDs via `stable_cluster_id()`)
- `contributing_source_records` — array of source record IDs that formed this golden record
- `survivorship_version` — policy version applied (e.g., "v1")
- `match_run_id` — entity resolution run ID
- `field_provenance` — JSON map of field winners (see above)

**Total field count varies by entity type** — it is always `len(output_fields)` (from the survivorship policy JSON) + the 5 system fields above. For `company`, `output_fields` currently declares 14 fields → 19 total; `supplier` declares 9 → 14 total. Check the specific entity type's `config/entity_resolution/{entity_type}/survivorship_v1.json` rather than assuming a fixed number.

**Output schema projection (`output_fields`):**  
Each survivorship policy declares an explicit `output_fields` list. Only those fields appear in the canonical Parquet files — source-internal IDs, duplicate name variants, and system-only fields are excluded. Empty `output_fields` = pass-through (used only in tests).

**Production entry point — `GoldenRecordPublisher.from_registry()`:**
```python
registry = ResolutionConfigRegistry(s3_bucket="edl-curated-087972550871", region_name="us-east-1")
publisher = GoldenRecordPublisher.from_registry(
    registry=registry,
    entity_type="company",
    analytics_s3_bucket="edl-analytics-087972550871",
    region_name="us-east-1",
)
```
The registry resolves the `latest` version pointer, loads and caches both JSON configs, and constructs the publisher. No rule set or policy is ever hardcoded in the Lambda handler.

**Key output:** Golden records with `golden_id`, `contributing_source_records`, `survivorship_version`, `match_run_id`, and only the fields declared in `output_fields`. Written to (tenant-prefixed):
```
s3://{analytics-layer}/{tenant_code}/canonical/{entity_type}/golden_date={date}/run_id={run_id}/golden.parquet
s3://{analytics-layer}/{tenant_code}/canonical/{entity_type}/match-decisions/{run_id}/decisions.json
```
**Lineage:** `entity_resolution/publishing_shared.py::emit_golden_record_lineage()` writes an `ENTITY_RESOLUTION` lineage record — but only when both `GOVERNANCE_S3_BUCKET` and `curated_s3_bucket` were supplied to the publisher (best-effort; skipped silently otherwise). Every field traces back to its contributing source record via `field_provenance` + `contributing_source_records`.

---

### Stage 15 — Analytics Layer Publish

**Component:** `analytics_publisher/analytics_publisher_handler.py`  
**Purpose:** Reads the **golden records** written by Stage 14 (not curated domain data directly — despite the Step Functions state's Terraform comment describing it as reading "golden records and curated datasets," the handler's actual business logic only reads `canonical_prefix`; `curated_s3_prefix` is validated on the event but is not otherwise used by this stage today), strips entity-resolution-internal fields, and republishes the result as the BI-facing analytics dataset with a Glue Catalog table registered/updated.

**Fields stripped before the BI-facing write** (`_INTERNAL_FIELDS_TO_DROP`): `_record_id`, `_source_id`, `contributing_source_records`, `survivorship_version`, `match_run_id`, `field_provenance`. `golden_id` is deliberately **kept** — it is the stable join key across entity types.

**Partition scheme (tenant-prefixed):** `s3://{bucket}/{tenant_code}/analytics/{entity_type}/analytics_date={date}/data.parquet` — note there is **no `run_id` in this path**. Unlike every other layer in this pipeline, a second run for the same entity type on the same UTC day **overwrites** the prior run's file, because nothing in the key disambiguates them. This is a real behavioural asymmetry versus the `canonical/` layer (which does partition by `run_id`), not a documentation inconsistency — confirm against `analytics_publisher_handler.py`'s `analytics_prefix` construction before assuming otherwise.

**Glue Catalog:** one table per `(tenant_code, entity_type)`, named `{tenant_code_with_hyphens_as_underscores}_{entity_type}` (Glue/Athena table names only allow `[a-z0-9_]`), registered via `governance.data_catalog_registration.DataCatalogRegistrationClient`. The day's Hive partition (`analytics_date=`) is registered explicitly via `glue_client.create_partition()` / `update_partition()` so `MSCK REPAIR TABLE` is not required after every run. Catalog/partition registration failures are logged as warnings and do **not** fail the pipeline — the Parquet is already written and directly queryable via its S3 path even if cataloguing fails.  
**Consumers:** Athena, QuickSight, ML feature stores, data science notebooks.

---

### Stage 16 — Serving Store Load

**Status:** code-complete (ruff/mypy/tests/`terraform validate` clean in all three environments), **not yet deployed anywhere** — no `terraform apply` has been run for it, so no RDS instance, Lambda, or IAM role exists in any AWS account today. The Step Functions `LoadServingStore` state stays on its `Pass` branch until it is.  
**Component:** `serving_store/serving_store_loader_handler.py`, dispatching to `serving_store/loaders/` via `ServingStoreLoaderRegistry` (`serving_store/registry.py`) — same adapter+registry pattern as `connector_runtime`'s source connectors. Each loader implements `serving_store/interfaces/loader_interface.py::ServingStoreLoaderInterface`.  
**Engines:** `mysql_rds_loader.py`, `postgresql_loader.py`, `sqlserver_loader.py` (also serves `azure_sql` — same T-SQL dialect). Onboarding (which tenant/entity_type pairs load, into which engine) is config-driven via `serving_store/serving_store_config_repository.py::ServingStoreConfigRepositoryClient`, backed by a new `EdlServingStoreConfig` DynamoDB table keyed by `tenant_code` + `entity_type` — the analytics-layer entity type (e.g. `company`), not a source-level `entity_id`, since one entity_type's analytics dataset can be fed by several contributing sources (e.g. `salesforce-account` + `netsuite-customer` both feed `company`).  
**Purpose:** Loads analytics records into a relational serving database for BI tools and applications.  
**Key properties:**
- Table schema inferred from Parquet schema — no hardcoded DDL
- Idempotent hash-diff incremental upsert — a `_row_hash`/`_synced_at` column pair; first sync per tenant/entity is an automatic full backfill, later runs only touch changed rows
- All SQL parameterized — no string interpolation of column names or values
- Tenant isolation via the database engine's own GRANT model (not application-level filtering), since BI tools connect directly: one database per tenant for MySQL, one schema per tenant for PostgreSQL/SQL Server/Azure SQL
- Two credential tiers per tenant in Secrets Manager: the loader's own writer credential, and a separate read-only reader credential (`edl/serving-store/{tenant_code}/{engine}/reader-credentials`) handed to the tenant's BI-tool connection
- Azure SQL is always tenant-supplied (BYO-DB) — Azure resources are never platform-provisioned by this AWS-based Terraform

---

## 5. Field Mapping System

Field mappings define how source field names and types are transformed into canonical domain model fields at the Raw → Curated stage.

### Config file location (Git)

```
config/field_mappings/
  salesforce/
    salesforce-account/v1.json
    salesforce-contact/v1.json
    salesforce-contract/v1.json
    salesforce-opportunity/v1.json
  netsuite/
    netsuite-customer/v1.json
  mysql-rds/
    mysql-rds-contracts/v1.json
    mysql-rds-contractterms/v1.json
  sage/
    sage-intacct-customer/v1.json
    sage-intacct-vendor/v1.json
    sage-intacct-arinvoice/v1.json
    sage-intacct-apbill/v1.json
    sage-x3-customer/v1.json
    sage-x3-supplier/v1.json
```

### S3 location (runtime)

```
s3://{curated-bucket}/field-mappings/{source_id}/{entity_id}/{version}.json
s3://{curated-bucket}/field-mappings/{source_id}/{entity_id}/latest.json  ← pointer
```

### JSON structure

```json
{
  "source_id": "salesforce",
  "entity_id": "salesforce-account",
  "mapping_version": "v1",
  "rules": [
    {
      "source_fields": ["Id"],
      "canonical_field": "account_id",
      "transformation": "rename",
      "transformation_params": {},
      "missing_field_behavior": "raise_error"
    }
  ]
}
```

### Available transformations

| transformation | params | purpose |
|---|---|---|
| `rename` | — | direct field rename |
| `concat` | `separator` (default `" "`) | join multiple source fields |
| `date_format` | `input_format`, `output_format` | reformat date strings |
| `cast` | `type`: `string`/`integer`/`decimal`/`float`/`boolean` | type coercion |
| `mask` | `visible_chars` (default `"4"`) | PII masking, keep last N chars |

### Version selection

- `"latest"` (default): reads `latest.json` pointer — points to highest version published
- Explicit: set `mapping_version="v1"` in `TransformationContext` to pin a specific version
- Rollback: republish `v1` rule set via `FieldMappingRegistryClient.publish_rule_set()` to reset `latest.json`

### Publish command

```bash
# Publish all mappings to dev
python scripts/seed_field_mappings.py --environment dev --region us-east-1

# Publish single entity
python scripts/seed_field_mappings.py --environment dev \
  --source-id salesforce --entity-id salesforce-account
```

---

## 6. Entity Resolution Config System

Entity resolution match rules and survivorship policies are managed as **versioned JSON config files** — analogous to field mapping configs but for entity identity.

### Config file location (Git)

```
config/entity_resolution/
  company/
    match_rules_v1.json     ← who is the same company? (email-exact + name-country-fuzzy)
    survivorship_v1.json    ← output schema + per-field source priority
  person/
    match_rules_v1.json     ← who is the same person? (email-exact + name-account-fuzzy)
    survivorship_v1.json    ← output schema + per-field source priority
  supplier/
    match_rules_v1.json     ← deterministic on vendor_id; fuzzy name for cross-source (Intacct vs X3)
    survivorship_v1.json    ← output schema + Intacct-preferred contact fields
  ar_invoice/
    match_rules_v1.json     ← deterministic on invoice_id (Intacct sole source — pass-through)
    survivorship_v1.json    ← AR invoice output schema
  ap_bill/
    match_rules_v1.json     ← deterministic on bill_id (Intacct sole source — pass-through)
    survivorship_v1.json    ← AP bill output schema
  contract/
    match_rules_v1.json     ← MySQL RDS Contracts (sole source — pass-through)
    survivorship_v1.json    ← contract output schema
  opportunity/
    match_rules_v1.json     ← Salesforce Opportunity (sole source — pass-through)
    survivorship_v1.json    ← opportunity output schema
  sales-contract/
    match_rules_v1.json     ← Salesforce Contract (sole source — pass-through)
    survivorship_v1.json    ← sales-contract output schema
  contract-term/
    match_rules_v1.json     ← MySQL RDS ContractTerms (sole source — pass-through)
    survivorship_v1.json    ← contract-term output schema
```
Not independently verified for this document: whether `contract`, `opportunity`, `sales-contract`, and `contract-term` have actually been seeded to a live environment via `seed_entity_resolution_configs.py` — see the Dev status note at the top of this document.

### S3 location (runtime)

```
s3://{curated-bucket}/entity-resolution/{entity_type}/match_rules_{version}.json
s3://{curated-bucket}/entity-resolution/{entity_type}/survivorship_{version}.json
s3://{curated-bucket}/entity-resolution/{entity_type}/latest.json  ← {"match_rules_version": "v1", "survivorship_version": "v1"}
```

### Match rules JSON structure

```json
{
  "entity_type": "company",
  "rule_set_version": "v1",
  "blocking": {
    "key_type": "email_domain",
    "source_field": "email_address",
    "max_block_size": 500
  },
  "rules": [
    {
      "rule_id": "email-exact",
      "strategy": "deterministic",
      "fields": [{ "field_name": "email_address", "normalise": true }]
    },
    {
      "rule_id": "name-country-fuzzy",
      "strategy": "probabilistic",
      "match_threshold": 0.85,
      "fields": [
        { "field_name": "full_name",       "weight": 0.70, "similarity_kind": "jaro_winkler" },
        { "field_name": "billing_country", "weight": 0.30, "similarity_kind": "exact" }
      ]
    }
  ]
}
```

### Survivorship JSON structure

```json
{
  "entity_type": "company",
  "policy_version": "v1",
  "output_fields": [
    "full_name", "email_address", "phone_number", "annual_revenue",
    "employee_count", "credit_limit", "billing_country", "billing_state",
    "industry", "is_active", "created_date", "last_modified_date"
  ],
  "default_strategy": "first_non_null",
  "attribute_rules": [
    { "canonical_field": "full_name",       "strategy": "source_priority", "source_priority": ["netsuite", "salesforce"] },
    { "canonical_field": "annual_revenue",  "strategy": "most_recent",     "timestamp_field": "last_modified_date" }
  ]
}
```

### Available matching strategies

| strategy | params | purpose |
|---|---|---|
| `deterministic` | `fields[]` with `normalise` | exact match on normalised key fields |
| `probabilistic` | `fields[]` with `weight` + `similarity_kind`; `match_threshold` | weighted similarity scoring |

### Available similarity kinds

| similarity_kind | algorithm |
|---|---|
| `exact` | normalised exact match |
| `jaro_winkler` | Jaro-Winkler string similarity |
| `token_set` | Jaccard similarity of word token sets |

### Blocking strategies

| key_type | key computed from |
|---|---|
| `email_domain` | domain part of email address |
| `phone_normalized` | digits-only phone prefix |
| `name_first3` | first 3 chars of normalised name |
| `record_id_prefix` | first N chars of source record ID |

### Version selection

- `"latest"` (default): reads `latest.json` pointer — points to highest published version
- Explicit: set `match_rules_version="v2"` in `ResolutionConfigRegistry.load()` to pin a version
- Rollback: republish old `match_rules_v1.json` and update `latest.json`

### Publish / seed command

```bash
# Publish all entity resolution configs to dev
python scripts/seed_entity_resolution_configs.py --environment dev --region us-east-1

# Publish single entity type
python scripts/seed_entity_resolution_configs.py --environment dev --entity-type company
```

---

## 7. Failure Handling and Replay

| Failure type | Classification | Retry behaviour | DLQ |
|---|---|---|---|
| Network timeout | `TRANSIENT_NETWORK` | 3 retries, exponential backoff | After all retries exhausted |
| API throttle | `API_THROTTLE` | 3 retries, exponential backoff | After all retries exhausted |
| Invalid credentials | `INVALID_CREDENTIALS` | No retry (deterministic) | Immediately |
| Breaking schema drift | `SCHEMA_MISMATCH` | No retry | Immediately |
| Quality blocking violation | Quality blocker | No retry | Alert only, no DLQ |
| Watermark concurrency conflict | Concurrency | No retry | Returns `PARTIAL_SUCCESS` |
| Mid-run Lambda timeout (checkpoint) | `LambdaTimeoutWarning` (non-fatal) | N/A — routes to terminal `ExtractionCheckpointed` Succeed state, not a retry | No DLQ — partial watermark + `'{run_id}-partN'` audit record already committed; auto-resume not implemented, needs manual re-trigger |

**DLQ processing:** Messages landing in `EdlExtractionFailureDlq` are consumed by the **`dlq_processor`** Lambda (`orchestration/dlq_processor/dlq_processor_handler.py`, SQS event source mapping with `batch_size=1`). It validates the message body (Pydantic), writes a `RunStatus.FAILED` audit record to the run audit log table, emits an SNS notification to the platform alerts topic (run_id, source_id, entity_id, failure_reason), and — only if `AUTO_REPLAY=true` (default `false`) — re-invokes the Step Functions state machine to replay the failed run. With auto-replay off (the default), an operator reviews the DLQ message and replays manually per the command below.

**Replay a failed run:**

```bash
python scripts/trigger_extraction.py \
  --source-id salesforce --entity-id salesforce-account \
  --environment dev \
  --is-replay \
  --replay-of-run-id run-20260615-120000000000-ab12cd34
```

---

## 8. Version Control and Rollback

| Artefact | Version format | Where stored | How to rollback |
|---|---|---|---|
| Field mapping | `v{n}` (e.g. `v1`, `v2`) | Git + S3 | Republish `v1` to reset `latest.json` |
| Entity resolution match rules | `v{n}` | `config/entity_resolution/` (Git) + S3 | Republish old JSON + update `latest.json` |
| Survivorship policy + output schema | `v{n}` | `config/entity_resolution/` (Git) + S3 | Republish old JSON + update `latest.json` |
| Entity config | `config_version` string | DynamoDB | `put_item` old record |
| Schema snapshot | SHA-256 fingerprint | S3 (immutable) | N/A — read-only history |
| Watermark | DynamoDB `version` counter | DynamoDB | Manual override only (ops procedure) |

---

## 9. Manual Trigger Checklist

Use this checklist when triggering stages manually (dev, debugging, replay).

### Pre-flight checks

```bash
# 1. Verify AWS identity
aws sts get-caller-identity

# 2. Entity config exists in DynamoDB
aws dynamodb get-item \
  --table-name EdlEntityExtractionConfig \
  --key '{"source_id":{"S":"salesforce"},"entity_id":{"S":"salesforce-account"}}'

# 3. Field mapping published to S3
aws s3 ls s3://edl-curated-087972550871/field-mappings/salesforce/salesforce-account/

# 4. Source credentials exist in Secrets Manager (pick the source you run)
aws secretsmanager describe-secret \
  --secret-id edl/sources/salesforce/credentials
aws secretsmanager describe-secret \
  --secret-id edl/sources/netsuite/credentials
aws secretsmanager describe-secret \
  --secret-id edl/sources/mysql-rds/credentials

# 5. Current watermark state
aws dynamodb get-item \
  --table-name EdlWatermarkRepository \
  --key '{"source_id":{"S":"salesforce"},"entity_id":{"S":"salesforce-account"}}'
```

### Trigger extraction

```bash
python scripts/trigger_extraction.py \
  --source-id salesforce \
  --entity-id salesforce-account \
  --environment dev \
  --param object_name=Account
```

### Post-extraction verification

```bash
# Raw files written
aws s3 ls s3://edl-raw-087972550871/salesforce/salesforce-account/ --recursive

# Watermark advanced
aws dynamodb get-item \
  --table-name EdlWatermarkRepository \
  --key '{"source_id":{"S":"salesforce"},"entity_id":{"S":"salesforce-account"}}'

# Schema snapshot written — path is tenant-prefixed (default tenant: demo)
aws s3 ls s3://edl-schema-snapshots-087972550871/demo/salesforce/salesforce-account/ --recursive

# No breaking drift (check drift_report) — path is tenant-prefixed (default tenant: demo)
aws s3 cp s3://edl-schema-snapshots-087972550871/demo/salesforce/salesforce-account/latest.json -
```

### Post-transformation verification

```bash
# Curated Parquet written — path is tenant-prefixed (default tenant: demo)
aws s3 ls s3://edl-curated-087972550871/demo/curated/customer/salesforce-account/ --recursive

# Quality report — check is_publication_blocked=false
aws s3 cp s3://edl-curated-087972550871/quality-reports/salesforce/salesforce-account/<run_id>/quality-report.json -
```

---

## 10. Pre-Deployment Verification

Before deploying to staging or prod, verify all of the following:

- [ ] Terraform plan reviewed and approved by two engineers
- [ ] `terraform validate` passes with no errors
- [ ] All CI gates pass: ruff → mypy → pytest (≥80%) → bandit → pip-audit → checkov → terraform validate
- [ ] Field mapping JSON files committed to Git under `config/field_mappings/`
- [ ] Entity resolution config JSON files committed to Git under `config/entity_resolution/`
- [ ] `seed_field_mappings.py` run against target environment (dry-run first)
- [ ] `seed_entity_resolution_configs.py` run against target environment (dry-run first)
- [ ] `seed_entity_config.py` run against target environment (dry-run first)
- [ ] Source credentials created in Secrets Manager for target environment
- [ ] Sage credentials created: `edl/sources/sage/intacct/credentials` and `edl/sources/sage/x3/credentials`
- [ ] NAT Gateway public IPs added to Salesforce/NetSuite IP allowlists
- [ ] CloudWatch alarms reviewed and SNS alert email set
- [ ] DLQ URL verified accessible by replay operator role
- [ ] At least one full extraction + transformation run verified in staging before prod promotion

---

## 11. Technology Reference

This section maps each pipeline stage to the exact tools, AWS services, Python libraries, and infrastructure components it depends on.

### AWS Services by Stage

| Stage | AWS Service(s) |
|---|---|
| Stage 1 — Event Scheduling | Amazon EventBridge Scheduler; Amazon SQS FIFO (pipeline trigger queue); AWS Lambda (pipeline trigger) |
| Stage 2 — Step Functions Orchestration | AWS Step Functions (Standard / Express Workflow) |
| Stage 3 — Configuration Load | Amazon DynamoDB (`EdlEntityExtractionConfig`) |
| Stage 4 — Credential Retrieval | AWS Secrets Manager (`edl/sources/{source}/credentials`) |
| Stage 5 — Metadata Discovery | Source APIs (no AWS; called from Lambda/ECS over VPC) |
| Stage 6 — Query Construction | In-process (no AWS service); ISO-8601 validated |
| Stage 7 — Extraction | AWS Lambda (< 5 M records) or AWS ECS Fargate (≥ 5 M records); Amazon S3 (raw layer write) |
| Stage 8 — Schema Snapshot | Amazon S3 (`edl-schema-snapshots-{account_id}`) |
| Stage 9 — Drift Evaluation | In-process (no AWS service); writes drift report to Amazon S3 |
| Stage 10 — Raw Layer Write | Amazon S3 (Object Lock GOVERNANCE); CloudWatch metric emit |
| Stage 11 — Watermark Update | Amazon DynamoDB (`EdlWatermarkRepository`; conditional put) |
| Stage 12 — Transformation | AWS Lambda or AWS Glue; Amazon S3 (curated layer); AWS Glue Data Catalog |
| Stage 13 — Entity Resolution | AWS Lambda; Amazon S3 (curated source read + analytics write) |
| Stage 14 — Golden Record Publish | AWS Lambda; Amazon S3 (analytics layer `canonical/` prefix) |
| Stage 15 — Analytics Layer Publish | AWS Lambda; Amazon S3 (analytics layer `analytics/` prefix, read from `canonical/`); AWS Glue Data Catalog (table + partition registration) |
| Stage 16 — Serving Store Load | Amazon RDS (MySQL, PostgreSQL, or SQL Server; private VPC) or tenant-supplied Azure SQL; AWS Secrets Manager — code-complete, not yet deployed in any environment |
| DLQ Processing (failure path) | AWS Lambda (`dlq_processor`); Amazon SQS (`EdlExtractionFailureDlq`, event source mapping `batch_size=1`); Amazon DynamoDB (run audit log); Amazon SNS (platform alerts topic); AWS Step Functions (optional auto-replay) |
| All stages | Amazon CloudWatch Logs; Amazon CloudWatch Metrics; AWS X-Ray; Amazon SQS (DLQ) |

### Python Libraries by Component

| Component | Key Libraries |
|---|---|
| Connector Runtime (all connectors) | `boto3`, `pyarrow`, `pydantic` ≥ 2.7, `structlog` ≥ 24.4 |
| Salesforce connector | `requests` (OAuth 2.0 client credentials); Bulk API 2.0 CSV streaming |
| NetSuite connector | `requests` (OAuth 1.0a); SuiteQL REST JSON |
| MySQL RDS connector | `pymysql`; `INFORMATION_SCHEMA` introspection |
| Sage connector (Intacct) | `requests` (OAuth 2.0 client credentials); JSON-POST pagination with `ia::meta.next` cursor |
| Sage connector (X3) | `requests` (OAuth 2.0 client credentials); OData v4 GET with `@odata.nextLink` cursor then `$skip`-based fallback |
| Watermark / Schema / Config repositories | `boto3` (DynamoDB / S3); `pydantic` |
| Transformation pipeline | `pyarrow` (Parquet I/O); `boto3`; `re` (pre-compiled patterns) |
| Entity resolution | `rapidfuzz` or custom Jaro-Winkler / Jaccard implementation |
| Observability | `structlog`, `boto3` CloudWatch |
| Infrastructure as Code | Terraform ≥ 1.8 (AWS Provider ~> 5.0) |

### Data Formats

| Format | Stage produced | Compression |
|---|---|---|
| **Apache Parquet** | Raw write (Stage 7), Curated write (Stage 12), Analytics write (Stages 14–15) | Snappy (curated/analytics); uncompressed large_utf8 (raw) |
| **JSON** | Schema snapshot (Stage 8), drift report (Stage 9), quality report (Stage 12), lineage record (Stage 12), golden match decisions (Stage 14) | None (human-readable) |
| **DynamoDB Item** | Config (Stage 3), watermark (Stage 11), audit log (all stages) | DynamoDB-native |

### Security Controls Applied Per Stage

| Stage | Security control |
|---|---|
| Stage 4 — Credential Retrieval | Secrets Manager; credentials held in memory only; never logged (structlog PII scrubber) |
| Stage 6 — Query Construction | Parameterised queries only; ISO-8601 validation on watermark values (SQL injection prevention) |
| Stage 7 — Extraction | S3 Object Lock GOVERNANCE; SSE-KMS; TLS 1.2+; VPC-only egress |
| Stage 12 — Transformation | HMAC-SHA256 tokenisation; SHA-256 hash; REDACT / PARTIAL_MASK / FULL_MASK applied before any write |
| All stages | IAM least-privilege roles; no wildcard `Action:*`; VPC Endpoints for all AWS service access |

### Entity Resolution Algorithms

| Algorithm | Purpose | Implementation |
|---|---|---|
| **Deterministic exact match** | Email, CRM ID, ERP reference codes | String normalisation + equality |
| **Jaro-Winkler similarity** | Name matching (handles abbreviations, transpositions) | Weighted probabilistic scoring |
| **Jaccard token-set similarity** | Company name matching (word-level) | Token overlap ratio |
| **Blocking** | Reduce comparison space before scoring | Email domain, phone prefix, name prefix, record ID prefix |

### Infrastructure as Code Reference

| Resource type | Terraform module | Key outputs |
|---|---|---|
| VPC, subnets, NAT, VPC Endpoints | `infrastructure/modules/networking/` | VPC ID, subnet IDs, endpoint IDs |
| S3 buckets (all layers) | `infrastructure/modules/storage/` | Bucket names, ARNs, Object Lock config |
| KMS key | `infrastructure/modules/kms/` | Key ARN (used as SSE key across all resources) |
| IAM roles (5 service roles + CI/CD) | `infrastructure/modules/iam/` | Role ARNs |
| Secrets Manager secrets | `infrastructure/modules/secrets/` | Secret ARNs, rotation schedules |
| DynamoDB tables | `infrastructure/modules/metadata_persistence/` | Table names, GSI names |
| CloudWatch, SNS, X-Ray | `infrastructure/modules/observability/` | Log group names, alarm ARNs, SNS topic ARN |
| Step Functions state machine | `infrastructure/modules/orchestration/` | State machine ARN |
| EventBridge schedules | Managed at runtime via `extraction_schedule_client.py` | Schedule names follow `{source_id}--{entity_id}`; target is SQS FIFO trigger queue (not Step Functions directly) |
