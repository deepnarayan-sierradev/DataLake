# Enterprise Data Lake Platform — Step-by-Step Technical Flow

**Version:** 1.0  
**Date:** 2026-06-15  
**Audience:** Engineers, data platform architects, and on-call operators

---

## Table of Contents

1. [Platform Architecture Overview](#1-platform-architecture-overview)
2. [Repository Layout and Module Responsibilities](#2-repository-layout-and-module-responsibilities)
3. [End-to-End Pipeline: Stage-by-Stage Walkthrough](#3-end-to-end-pipeline-stage-by-stage-walkthrough)
   - [Stage 0 — Source Onboarding](#stage-0--source-onboarding)
   - [Stage 1 — Extraction Trigger (EventBridge → Step Functions)](#stage-1--extraction-trigger-eventbridge--step-functions)
   - [Stage 2 — Configuration Load](#stage-2--configuration-load)
   - [Stage 3 — Credential Resolution](#stage-3--credential-resolution)
   - [Stage 4 — Metadata Discovery](#stage-4--metadata-discovery)
   - [Stage 5 — Extraction Query Build](#stage-5--extraction-query-build)
   - [Stage 6 — Data Extraction and Raw Layer Write](#stage-6--data-extraction-and-raw-layer-write)
   - [Stage 7 — Schema Snapshot Persistence](#stage-7--schema-snapshot-persistence)
   - [Stage 8 — Schema Drift Evaluation](#stage-8--schema-drift-evaluation)
   - [Stage 9 — Watermark Advancement](#stage-9--watermark-advancement)
   - [Stage 10 — Transformation Trigger](#stage-10--transformation-trigger)
   - [Stage 11 — Field Mapping and Quality Evaluation](#stage-11--field-mapping-and-quality-evaluation)
   - [Stage 12 — Curated Layer Write](#stage-12--curated-layer-write)
   - [Stage 13 — Entity Resolution and Golden Record](#stage-13--entity-resolution-and-golden-record)
   - [Stage 14 — Analytics Layer Publish](#stage-14--analytics-layer-publish)
   - [Stage 15 — Serving Store Load](#stage-15--serving-store-load)
4. [Failure Handling and Replay](#4-failure-handling-and-replay)
5. [Observability: Logs, Metrics, and Traces](#5-observability-logs-metrics-and-traces)
6. [Governance: Lineage, Classification, and Retention](#6-governance-lineage-classification-and-retention)
7. [Security Controls Per Layer](#7-security-controls-per-layer)
8. [S3 Bucket and Prefix Layout](#8-s3-bucket-and-prefix-layout)
9. [DynamoDB Table Layout](#9-dynamodb-table-layout)
10. [Adding a New Connector](#10-adding-a-new-connector)
11. [Runbook Reference](#11-runbook-reference)

---

## 1. Platform Architecture Overview

```
EventBridge Scheduler
        │
        ▼
 Step Functions State Machine  ◄──── Dead-Letter Queue (replay)
        │
        ├──► Configuration Repository (DynamoDB) ──► EntityExtractionConfig
        ├──► Secrets Manager ──────────────────────► Source Credentials
        │
        ▼
 Connector Runtime (Python Lambda / ECS Task)
        │
        ├──► Source APIs  (Salesforce Bulk 2.0 / NetSuite / MySQL RDS)
        │         │
        │         ▼
        │    S3  ──►  raw/{source_id}/{entity_id}/{date}/part-NNNNN.parquet
        │
        ├──► Schema Snapshot Repository (S3)
        ├──► Schema Drift Evaluator
        └──► Watermark Repository (DynamoDB)
                   │
                   ▼  (success only)
          Transformation Pipeline (Lambda / Glue)
                   │
                   ├──► Field Mapping Registry (S3)
                   ├──► Quality Policy Evaluator
                   ├──► Data Classification Masking
                   └──► Curated Layer (S3 Parquet, Glue Catalog)
                              │
                              ▼
                     Entity Resolution Engine
                              │
                              ▼
                       Golden Record Publisher
                              │
                              ▼
                      Analytics Layer (S3)
                              │
                              ▼
                    Serving Store Loader (RDS / Redshift / DynamoDB)
```

---

## 2. Repository Layout and Module Responsibilities

```
DataLake/
├── contracts/                      Shared Pydantic models and validation
│   ├── entity_configuration_contract.py   EntityExtractionConfig
│   ├── observability_contract.py          StructuredLogEvent, PipelineStage, RunStatus
│   ├── pipeline_stage_contract.py         PipelineStageContract (audit record per stage)
│   └── identifier_policy.py              Stable-ID and Run-ID regex patterns
│
├── connector_runtime/              Extraction engine
│   ├── interfaces/                 Abstract ConnectorInterface, ExtractionRecord
│   ├── adapters/
│   │   ├── salesforce/             SalesforceAuthClient, BulkQueryJobController, RawLayerWriter
│   │   ├── netsuite/               NetSuiteConnector, RawLayerWriter
│   │   └── mysql_rds/              MySqlRdsConnector, RawLayerWriter
│   ├── query_builders/             SOQL query builder (parameterised, ISO-8601 validated)
│   ├── configuration_repository/   DynamoDB-backed EntityExtractionConfig loader
│   ├── run_lifecycle/              RunCoordinator (run_id generation, audit emit, DLQ)
│   ├── certification/              ConnectorCertificationChecklist
│   └── registry.py                 ConnectorRegistry (plugin registration)
│
├── orchestration/
│   ├── step_functions/
│   │   ├── extraction_workflow.py  10-stage extraction pipeline orchestrator
│   │   ├── extraction_retry_policy.py  Per-entity circuit breaker + retry limits
│   │   └── run_replay_controller.py    Replay past extraction windows
│   └── event_bridge/
│       └── extraction_schedule_client.py  Create/update/delete cron schedules
│
├── schema_management/
│   ├── snapshot_repository/        S3-backed immutable field schema snapshots
│   └── drift_evaluation/           Drift classifier (non_breaking / potentially_breaking / breaking)
│
├── watermark_management/
│   └── watermark_repository/       DynamoDB watermark with optimistic concurrency
│
├── transformation/
│   ├── transformation_pipeline.py  Orchestrates mapping → quality → masking → write
│   ├── field_mapping/              S3-backed rule sets (rename, concat, date_format …)
│   ├── quality_evaluation/         Null, pattern, range, enum checks (BLOCKING / WARNING)
│   ├── curated_layer_writer.py     Parquet write to curated S3 prefix + Glue catalog
│   ├── analytics_layer_publisher.py  Promote curated data to analytics layer
│   ├── athena_query_client.py      Poll-based Athena query execution
│   └── serving_store_loader.py     Load curated/analytics data into target DB
│
├── entity_resolution/
│   ├── matching_engine/            RecordBlocker + MatchRuleEngine (deterministic + probabilistic)
│   ├── resolution_config/          ResolutionConfigRegistry — S3-backed loader for match rules + survivorship
│   ├── survivorship_policy.py      SurvivorshipPolicy with output_fields schema projection
│   └── canonical_record_publisher/    GoldenRecordPublisher (from_registry factory + publish)
│
├── governance/
│   ├── data_classification_policy.py   PII/masking policy per entity field
│   ├── data_catalog_registration.py    Glue catalog upsert (create-first / catch-AlreadyExists)
│   ├── lineage_record.py               S3-backed lineage records (extraction + transformation)
│   ├── retention_policy_enforcer.py    S3 Object Lock + legal hold controls
│   └── source_onboarding_registry.py   6-gate onboarding flow (DynamoDB-backed)
│
├── observability/
│   ├── structured_logger.py        structlog JSON logger with PII scrubbing
│   └── metrics_emitter.py          CloudWatch buffered metric emission (flush in batches)
│
├── infrastructure/
│   ├── modules/  (Terraform)
│   │   ├── networking/   VPC, private subnets, VPC endpoints
│   │   ├── storage/      S3 buckets (raw, curated, analytics, schema-snapshots)
│   │   ├── iam/          Service roles (extraction, transformation, entity-resolution …)
│   │   ├── secrets/      Secrets Manager namespaces per source
│   │   ├── metadata_persistence/  DynamoDB tables (watermark, run-audit-log, config)
│   │   └── observability/         CloudWatch, X-Ray, SNS alarm topics
│   └── environments/
│       ├── dev/
│       ├── staging/
│       └── prod/
│
└── scripts/
    ├── seed_entity_config.py       Bootstrap entity configuration records
    └── trigger_extraction.py       Manual extraction trigger for testing
```

---

## 3. End-to-End Pipeline: Stage-by-Stage Walkthrough

### Stage 0 — Source Onboarding

**Code:** `governance/source_onboarding_registry.py`  
**Trigger:** Manual (data platform team) via `SourceOnboardingRegistry`

Before a source can be extracted, it must pass **six sequential gates** stored in DynamoDB:

| Gate | What is validated |
|------|-------------------|
| `SOURCE_REGISTRATION` | `source_id`, owner email, SLA tier, data classification |
| `CREDENTIAL_REGISTRATION` | Secrets Manager entry confirmed; rotation schedule set |
| `ENTITY_MAPPING` | At least one `EntityExtractionConfig` record exists |
| `EXTRACTION_PROFILE` | Dry-run in `dev` succeeded; schema snapshot captured |
| `SECURITY_GOVERNANCE` | Security review passed; classification policy confirmed |
| `ACCEPTANCE_VALIDATION` | Canary run passed; record counts and quality checks confirmed |

A gate that has not been passed blocks extraction activation. Each gate transition is immutably logged. A waiver (skipping a gate) requires a written justification of at least 20 characters.

**Entity configuration record example** (stored in DynamoDB `{env}-entity-extraction-config`):

```json
{
  "source_id": "salesforce",
  "entity_id": "salesforce-account",
  "load_type": "incremental",
  "watermark_field": "SystemModstamp",
  "field_mode": "all",
  "exclude_fields": ["IsDeleted"],
  "extraction_window_days": 1,
  "overlap_window_days": 0,
  "active": true
}
```

---

### Stage 1 — Extraction Trigger (EventBridge → Step Functions)

**Code:** `orchestration/event_bridge/extraction_schedule_client.py`  
**AWS Services:** EventBridge Scheduler → Step Functions State Machine

- EventBridge fires a scheduled rule on the cron expression stored per entity.
- Schedule naming convention: `{source_id}--{entity_id}` (double-hyphen separates source and entity).
- The schedule passes `source_id`, `entity_id`, and optional `connector_params` as the Step Functions input payload.
- The Step Functions state machine (`extraction-orchestration-workflow`) begins execution.
- EventBridge uses a dedicated IAM execution role that has only `sfn:StartExecution` on the specific state machine ARN — no wildcard permissions.

**Schedule payload example:**

```json
{
  "source_id": "salesforce",
  "entity_id": "salesforce-account",
  "connector_params": {}
}
```

---

### Stage 2 — Configuration Load

**Code:** `connector_runtime/configuration_repository/configuration_repository.py`  
**Code:** `orchestration/step_functions/extraction_workflow.py` → `_stage_load_config()`

`ConfigurationRepositoryClient.get_entity_config(source_id, entity_id, environment)` reads the entity configuration from DynamoDB. The table name is `{environment}-entity-extraction-config`.

- The record is validated against the `EntityExtractionConfig` Pydantic model (frozen, `extra='forbid'`).
- Invalid or missing configurations raise immediately — the pipeline does not proceed.
- The `active` flag is checked; inactive entities are skipped.

**Key fields consumed:**

| Field | Purpose |
|-------|---------|
| `load_type` | `full` or `incremental` |
| `watermark_field` | Source timestamp field for delta filtering |
| `field_mode` | `all`, `standard`, `custom`, or `includeOnly` |
| `extraction_window_days` | Max window for a single extraction run |
| `overlap_window_days` | Extra lookback to catch late-arriving updates |

---

### Stage 3 — Credential Resolution

**Code:** `orchestration/step_functions/extraction_workflow.py` → `_stage_resolve_watermark()`  
**AWS Services:** Secrets Manager, Watermark Repository (DynamoDB)

1. **Secrets Manager** is called at runtime to retrieve short-lived source credentials. Credentials are never stored in code, environment variables, or logs.
2. **Watermark Repository** (`WatermarkRepository.get_watermark()`) loads the current watermark from DynamoDB using a strongly-consistent read. Returns `None` on first run.
3. `WatermarkRepository.compute_extraction_window()` calculates:
   - `lower_bound = last_successful_watermark - overlap_window_days`
   - `upper_bound = now`
   - For full loads, the window is `now - extraction_window_days` → `now`

The computed extraction window is carried forward to the query build stage.

---

### Stage 4 — Metadata Discovery

**Code:** `connector_runtime/interfaces/connector_interface.py` → `ConnectorInterface.discover_queryable_fields()`  
**Adapter implementations:**
- Salesforce: `connector_runtime/adapters/salesforce/` — calls Salesforce Describe API
- NetSuite: `connector_runtime/adapters/netsuite/` — calls SuiteQL metadata endpoint
- MySQL RDS: `connector_runtime/adapters/mysql_rds/` — executes `INFORMATION_SCHEMA` queries

Returns a `FieldContract`: a list of `FieldDescriptor` objects (name, data_type, nullable, queryable, is_custom).

The `field_mode` from configuration controls which fields are included:
- `all` — every queryable field
- `standard` — non-custom fields only
- `custom` — custom (`__c`) fields only
- `includeOnly` — only fields listed in `include_fields`

Fields in `exclude_fields` are always removed regardless of `field_mode`.

---

### Stage 5 — Extraction Query Build

**Code:** `connector_runtime/interfaces/connector_interface.py` → `ConnectorInterface.build_extraction_query()`  
**Salesforce specific:** `connector_runtime/query_builders/salesforce_soql_query_builder.py`  
**Bulk job controller:** `connector_runtime/adapters/salesforce/salesforce_bulk_query_job_controller.py`

Builds a parameterised extraction query from the `FieldContract` + watermark bounds.

**Salesforce SOQL example (incremental):**
```sql
SELECT Id, Name, SystemModstamp, BillingCity
FROM Account
WHERE SystemModstamp >= 2026-06-14T00:00:00Z
  AND SystemModstamp <  2026-06-15T00:00:00Z
```

Security control: parameter values are validated against ISO-8601 datetime pattern before string substitution — no raw user input reaches the query string.

Returns a `QueryContract`: the built query string, field list, and extraction metadata.

---

### Stage 6 — Data Extraction and Raw Layer Write

**Code:**  
- `connector_runtime/interfaces/connector_interface.py` → `ConnectorInterface.execute_extraction()`  
- `connector_runtime/adapters/salesforce/salesforce_bulk_query_job_controller.py` — Salesforce Bulk API 2.0  
- `connector_runtime/adapters/*/.*_raw_layer_writer.py` — streaming Parquet write to S3

**How streaming works (memory-efficient):**

```
Source API
    │
    ▼  (batches of records)
execute_extraction() → Iterator[ExtractionRecord]
    │
    ▼  (chunk_size=50,000 records at a time)
write_partition_streaming(record_iter, ...)
    │
    ├── chunk 0 → part-00000.parquet  → S3 put_object
    ├── chunk 1 → part-00001.parquet  → S3 put_object
    └── ...
    │
    ▼
metadata.json sidecar (record_count, schema_fingerprint, run_id, …)
```

Only one chunk is held in memory at any time. This avoids materialising the full dataset into RAM (O(n) → O(chunk_size) memory).

**S3 path written:**
```
s3://{raw-bucket}/raw/{source_id}/{entity_id}/{extraction_date}/
    part-00000.parquet
    part-00001.parquet
    metadata.json
```

**ExtractionRecord structure:**
```python
@dataclass(frozen=True)
class ExtractionRecord:
    payload: dict[str, Any]   # raw field values from source
```

---

### Stage 7 — Schema Snapshot Persistence

**Code:** `schema_management/snapshot_repository/snapshot_repository.py`

After successful extraction, the field schema is persisted as an immutable S3 object:

```
s3://{schema-snapshots-bucket}/schemas/{source_id}/{entity_id}/{schema_version}/{extraction_date}.json
```

**Snapshot record fields:**
- `source_id`, `entity_id`, `schema_version` (SHA-256 fingerprint of field list)
- `captured_at` (ISO-8601 UTC)
- `fields`: list of `FieldSnapshot` (name, data_type, is_nullable, is_queryable, is_custom)

Each snapshot is immutable — versions are never overwritten.

---

### Stage 8 — Schema Drift Evaluation

**Code:** `schema_management/drift_evaluation/drift_evaluator.py`

Compares the current snapshot against the previous successful snapshot.

**Drift classification:**

| Classification | Example | Downstream impact |
|---|---|---|
| `no_drift` | No field changes | Pipeline proceeds normally |
| `non_breaking` | New nullable field added | Pipeline proceeds; transformation layer alerted |
| `potentially_breaking` | Field precision/length changed | Pipeline proceeds; alert raised; manual review recommended |
| `breaking` | Field removed or type changed | Raw written; watermark advanced; **transformation blocked** |

The drift report is written to S3 alongside the schema snapshot. PII field names are never included in drift logs.

If `breaking` drift is detected, `ExtractionWorkflowResult.transformation_blocked = True` and the Step Functions workflow does not trigger transformation for this run.

---

### Stage 9 — Watermark Advancement

**Code:** `watermark_management/watermark_repository/watermark_repository.py`

`WatermarkRepository.advance_watermark(current, new_upper_watermark, run_id)` uses a DynamoDB **conditional put** (`version = :expected`) to prevent race conditions:

- If two extraction runs finish concurrently, only one wins the conditional write.
- The loser receives `WatermarkConcurrencyError`, and the run completes with `partial=True` — no failure, no data loss. The next scheduled run will pick up from the correct watermark.
- The watermark is **never** advanced if the extraction failed or was partial.

**Watermark record (DynamoDB):**
```json
{
  "source_id": "salesforce",
  "entity_id": "salesforce-account",
  "environment": "prod",
  "last_successful_watermark": "2026-06-14T00:00:00+00:00",
  "upper_watermark": "2026-06-15T00:00:00+00:00",
  "run_id": "run-20260615-120000000000-ab12cd34",
  "version": 47
}
```

---

### Stage 10 — Transformation Trigger

**Code:** `orchestration/step_functions/extraction_workflow.py` → final stage emit  
**Code:** `transformation/transformation_pipeline.py`

If extraction completed successfully and no breaking drift was detected, the Step Functions workflow triggers the transformation pipeline, passing the raw S3 prefix, run metadata, and schema fingerprint.

The transformation pipeline is intentionally decoupled from extraction — it runs in a separate Lambda/Glue job context.

---

### Stage 11 — Field Mapping and Quality Evaluation

**Code:**  
- `transformation/field_mapping/field_mapping_registry.py`  
- `transformation/quality_evaluation/quality_policy_evaluator.py`

**Field Mapping:**  
A `FieldMappingRuleSet` is loaded from S3 (`s3://{mapping-bucket}/{source_id}/{entity_id}/mapping.json`). If no rule set exists, identity mapping (pass-through) is used.

Each rule maps one or more source fields to a canonical field with a transformation:

| Transformation | Example |
|---|---|
| `RENAME` | `Account_Name__c` → `account_name` |
| `CONCAT` | `[FirstName, LastName]` → `full_name` |
| `DATE_FORMAT` | `"2026-06-15T00:00:00Z"` → `"2026-06-15"` |
| `CONSTANT` | Always writes a fixed value |
| `DROP` | Field excluded from curated output |

Missing required fields increment the `mapping_failures` counter. Records with mapping failures are excluded from the curated output.

**Quality Evaluation:**  
Checks are run against canonical records before curated write:

| Check type | Severity | Effect |
|---|---|---|
| `NullCheck` | `BLOCKING` | Blocks curated publication for this run |
| `PatternCheck` | `BLOCKING` / `WARNING` | Blocks or warns on pattern mismatch |
| `RangeCheck` | `WARNING` | Warns on out-of-range numeric values |
| `EnumCheck` | `BLOCKING` | Blocks records with invalid enum values |

Regex patterns are pre-compiled once before the record loop (not per-record recompile).

If any `BLOCKING` check fails, `TransformationResult.is_publication_blocked = True` — curated write is skipped, and a quality report is written to S3.

---

### Stage 12 — Curated Layer Write

**Code:**  
- `transformation/curated_layer_writer.py`  
- `governance/data_classification_policy.py` → `FieldMaskingApplier`  
- `governance/data_catalog_registration.py`  
- `governance/lineage_record.py`

**Data masking** is applied before any write to the curated layer:

| Masking strategy | Applied to |
|---|---|
| `REDACT` | PII fields not needed downstream |
| `PARTIAL_MASK` | Show last 4 chars (e.g. credit card) |
| `TOKENISE` | HMAC-SHA256 deterministic pseudonym (keyed) |
| `HASH` | SHA-256 (irreversible, for join keys) |
| `FULL_MASK` | Replace with `***` |

**Curated S3 path:**
```
s3://{curated-bucket}/curated/{domain}/{entity_id}/{curated_date}/part-NNNNN.parquet
```

**Glue Data Catalog** is updated with the curated dataset spec using a create-first / catch-`AlreadyExistsException` pattern to prevent TOCTOU races.

**Lineage record** is written to the governance bucket:
```
s3://{governance-bucket}/lineage/{entity_id}/{run_id}/transformation-lineage.json
```

---

### Stage 13 — Entity Resolution and Golden Record

**Code:**  
- `entity_resolution/matching_engine/record_blocker.py`  
- `entity_resolution/matching_engine/match_rule_engine.py`  
- `entity_resolution/resolution_config/resolution_config_registry.py`  
- `entity_resolution/canonical_record_publisher/canonical_record_publisher.py`  
- `entity_resolution/survivorship_policy.py`

**Config-driven — all rules loaded from S3:**  
Match rules and survivorship policies are never hardcoded in Python. `ResolutionConfigRegistry` loads versioned JSON configs from S3 at runtime:

```
s3://{curated-bucket}/entity-resolution/{entity_type}/match_rules_{version}.json
s3://{curated-bucket}/entity-resolution/{entity_type}/survivorship_{version}.json
s3://{curated-bucket}/entity-resolution/{entity_type}/latest.json
```

Local source files live in `config/entity_resolution/`. Published to S3 via `seed_entity_resolution_configs.py`. Two entity types are defined:

| Entity type | Sources merged | Key match rule | Canonical output |
|---|---|---|---|
| `company` | Salesforce Account + NetSuite Customer | email-exact (deterministic) + name-country-fuzzy (probabilistic, threshold 0.85) | `canonical/company/` |
| `person` | Salesforce Contact | email-exact (deterministic) + name-account-fuzzy (probabilistic, threshold 0.88) | `canonical/person/` |

**Blocking (performance optimisation):**  
Before pairwise matching, records are partitioned into blocks so that matching is only done within each block:

| Blocking strategy | Key computed on |
|---|---|
| `EMAIL_DOMAIN` | Domain part of email address |
| `PHONE_NORMALIZED` | Digits-only phone number |
| `NAME_FIRST3` | First 3 chars of normalised name |
| `RECORD_ID_PREFIX` | First 6 chars of source record ID |

Blocks exceeding the `max_block_size` threshold are subdivided to prevent O(n²) matching.

**`output_fields` schema projection:**  
The `SurvivorshipPolicy` carries an `output_fields` tuple. After survivorship, `GoldenRecordSurvivorshipPolicy.resolve()` projects the canonical record to only those fields — excluding source-internal IDs, duplicate name columns, and other noise. Empty `output_fields` = pass-through (backward compatible).

**Production entry point:**
```python
registry = ResolutionConfigRegistry(s3_bucket="dev-edl-curated", region_name="us-east-1")
publisher = GoldenRecordPublisher.from_registry(
    registry=registry,
    entity_type="company",
    analytics_s3_bucket="dev-edl-analytics",
    region_name="us-east-1",
)
```

**Survivorship policy** selects the most authoritative value for each canonical field across cluster members. Golden records are written to `s3://{analytics-bucket}/canonical/{entity_type}/golden_date={date}/run_id={run_id}/golden.parquet`.

Each golden record includes a **`field_provenance`** column — a JSON map documenting which source system won for each output field. This enables full source attribution without drilling into S3.

---

### Field Provenance and Source Attribution

**What it is:**  
Every golden record carries a `field_provenance` dict mapping each output field to the source_id that won (via survivorship rules). This is embedded directly in the Parquet row and serialized to JSON.

**Example: Company entity (merges Salesforce Account + NetSuite Customer)**

Golden record row:
```json
{
  "golden_id": "acme-001",
  "full_name": "Acme Corp",
  "annual_revenue": 5000000,
  "credit_limit": 100000,
  "industry": "Technology",
  "field_provenance": {
    "full_name": "netsuite",
    "email_address": "netsuite",
    "annual_revenue": "salesforce",
    "employee_count": "salesforce",
    "credit_limit": "netsuite",
    "industry": "salesforce",
    "billing_country": "netsuite"
  },
  "contributing_source_records": ["sf-account-001", "ns-customer-042"],
  "survivorship_version": "v1",
  "match_run_id": "run-20260617-company-matching-001"
}
```

**Query in Athena (Analytics Layer):**
```sql
-- See which source won for each field on a company
SELECT
    golden_id,
    full_name,
    annual_revenue,
    industry,
    json_extract_scalar(field_provenance, '$.full_name') AS full_name_source,
    json_extract_scalar(field_provenance, '$.annual_revenue') AS annual_revenue_source,
    json_extract_scalar(field_provenance, '$.industry') AS industry_source,
    json_extract_scalar(field_provenance, '$.credit_limit') AS credit_limit_source
FROM canonical_company
WHERE golden_id = 'acme-001';
```

Result:
```
golden_id  | full_name  | annual_revenue | industry      | full_name_source | annual_revenue_source | industry_source | credit_limit_source
acme-001   | Acme Corp  | 5000000        | Technology    | netsuite         | salesforce            | salesforce      | netsuite
```

**Query in Serving Store (MySQL RDS):**
```sql
-- Query provenance from the operational database
SELECT
    golden_id,
    full_name,
    annual_revenue,
    JSON_EXTRACT(field_provenance, '$.full_name') AS full_name_source,
    JSON_EXTRACT(field_provenance, '$.annual_revenue') AS annual_revenue_source,
    JSON_EXTRACT(field_provenance, '$.credit_limit') AS credit_limit_source
FROM canonical_company
WHERE golden_id = 'acme-001';
```

**Audit query: How many fields came from each source?**
```sql
-- Athena
SELECT
    golden_id,
    full_name,
    -- Count how many fields were won by each source
    size(filter(map_values(field_provenance), x -> x = 'salesforce')) AS salesforce_field_count,
    size(filter(map_values(field_provenance), x -> x = 'netsuite')) AS netsuite_field_count
FROM canonical_company
WHERE golden_id = 'acme-001';
```

**Why this matters:**
- BI tools can instantly see which data source is authoritative for each field
- Data quality teams can identify when unexpected sources win (e.g., Salesforce winning on credit_limit is unusual)
- Audit compliance: full source attribution is queryable without S3 drilling
- No re-computation: survivorship logic is not re-run at query time — the answer is stored in the row

---

### System Fields in Golden Records

Beyond the **14 `output_fields`** defined in the survivorship policy, every golden record automatically includes **5 system fields**:

| Field | Type | Purpose |
|-------|------|---------|
| `golden_id` | string | Deterministic ID derived from hashing contributing source records. Stable across re-runs. |
| `contributing_source_records` | array[string] | IDs of all source records that matched/clustered into this golden record. |
| `survivorship_version` | string | Policy version applied (e.g., "v1" from `survivorship_v1.json`). Enables policy evolution tracking. |
| `match_run_id` | string | Run ID of the entity resolution matching run. Links golden record to match audit trail. |
| `field_provenance` | JSON object | Maps each of the 14 output_fields to winning source_id. `{"full_name": "netsuite", "annual_revenue": "salesforce", ...}` |

**Total fields per golden record: 19** (14 output_fields + 5 system fields)

**Example golden record row:**
```json
{
  "golden_id": "acme-001",
  "full_name": "Acme Corp",
  "annual_revenue": 5000000,
  "email_address": "billing@acme.com",
  "... (10 more output_fields) ...",
  "field_provenance": {"full_name": "netsuite", "annual_revenue": "salesforce", ...},
  "contributing_source_records": ["sf-account-001", "ns-customer-042"],
  "survivorship_version": "v1",
  "match_run_id": "run-20260617-company-matching-001"
}
```

**Partition keys** (not columns, but part of S3 path):
- `golden_date=YYYY-MM-DD` — date the golden record was published
- `run_id={run_id}` — Step Functions execution ID

S3 path: `s3://{analytics-bucket}/canonical/{entity_type}/golden_date={date}/run_id={run_id}/golden.parquet`

---

### Stage 14 — Analytics Layer Publish

**Code:** `transformation/analytics_layer_publisher.py`

Promotes curated and golden record datasets to the analytics layer with Athena-compatible partition layout:

```
s3://{analytics-bucket}/analytics/{domain}/{entity_id}/year=YYYY/month=MM/day=DD/
```

Partition metadata is registered in the Glue catalog for Athena query support.

---

### Stage 15 — Serving Store Load

**Code:** `transformation/serving_store_loader.py`

Loads analytics data into the target serving database (RDS, Redshift, or DynamoDB) for operational applications and APIs requiring low-latency reads. Uses upsert semantics with the canonical entity primary key. Supports full-replace and incremental-merge load modes.

> **Note:** Athena is the query engine for the Analytics layer (BI tools, ad-hoc SQL, dashboards). Serving Store is a separate optional target for application workloads that cannot absorb Athena query latency.

---

## 4. Failure Handling and Replay

**Code:**  
- `connector_runtime/run_lifecycle/run_lifecycle.py` → `enqueue_dlq_entry()`  
- `orchestration/step_functions/extraction_retry_policy.py` → per-entity circuit breaker  
- `orchestration/step_functions/run_replay_controller.py`

### Circuit Breaker

Each `(source_id, entity_id)` pair has an independent circuit breaker:
- Tracks consecutive failures.
- Opens after a configurable threshold.
- Open circuit causes immediate `CircuitOpenError` — prevents hammering a failing source.
- Circuit resets on a successful run or explicit `reset_circuit()` call.

### Dead-Letter Queue

On terminal pipeline failure:
1. `RunCoordinator.enqueue_dlq_entry()` sends a JSON message to `{environment}-extraction-dlq`.
2. The message contains `run_id`, `source_id`, `entity_id`, `failed_stage`, `error_message` (scrubbed of credentials/PII), `enqueued_at`.
3. The watermark is **not** advanced.

### Replay

`RunReplayController.replay_extraction_window()` re-runs a past extraction window:
- Uses the historical watermark record (does not advance watermark).
- Writes to the same S3 prefix (idempotent — S3 put_object replaces existing objects).
- Useful for backfills and incident recovery.

---

## 5. Observability: Logs, Metrics, and Traces

**Code:**  
- `observability/structured_logger.py` — structlog JSON logger  
- `observability/metrics_emitter.py` — CloudWatch buffered emitter

### Structured Logging

Every log event is a JSON object. Example extraction stage log:

```json
{
  "timestamp": "2026-06-15T12:00:01.234Z",
  "level": "info",
  "event": "extraction_stage_complete",
  "run_id": "run-20260615-120000000000-ab12cd34",
  "source_id": "salesforce",
  "entity_id": "salesforce-account",
  "stage": "extraction",
  "status": "success",
  "duration_ms": 4823,
  "record_count": 12500
}
```

**PII/credential scrubbing:** Patterns matching `password=`, `token:`, `Bearer `, `AKIA...`, `api_key=` are replaced with `[REDACTED]` before any log emission. Hard rejection in `StructuredLogEvent` prevents sensitive values reaching the log stream.

### CloudWatch Metrics

Metrics are buffered in `CloudWatchMetricsEmitter._pending` and flushed in batches of 1,000:

| Metric name | Description |
|---|---|
| `RecordsExtracted` | Count of raw records written per run |
| `RecordsFailed` | Count of extraction or quality failures |
| `RetryCount` | Number of retries attempted |
| `WatermarkLagSeconds` | Time delta between now and the current watermark |
| `SchemaDriftCount` | Number of drift events detected |

### structlog Context Vars

At the start of each extraction run, `structlog.contextvars.bind_contextvars(run_id=..., source_id=..., entity_id=...)` binds context that flows through every log call in the run — no manual parameter threading. Cleared in `finally` to prevent cross-run contamination.

---

## 6. Governance: Lineage, Classification, and Retention

### Data Lineage

`LineageEmitter` writes a compact JSON lineage record to S3 for every extraction and transformation run:

```json
{
  "run_id": "run-20260615-120000000000-ab12cd34",
  "pipeline_stage": "extraction",
  "source_id": "salesforce",
  "entity_id": "salesforce-account",
  "source_nodes": [{"system": "salesforce", "s3_path": "s3://...", "layer": "source"}],
  "target_node": {"system": "raw_layer", "s3_path": "s3://raw/salesforce/...", "layer": "raw"},
  "record_count": 12500,
  "captured_at": "2026-06-15T12:00:05Z"
}
```

### Data Classification

`EntityClassificationPolicy` maps source fields to sensitivity levels and masking strategies. Applied automatically before any curated or analytics layer write.

### Retention

`RetentionPolicyEnforcer` uses S3 Object Lock (GOVERNANCE mode):
- Raw layer: 7 years retention (regulatory default).
- Curated layer: 3 years.
- Analytics layer: 1 year (queryable window; archive to Glacier after).
- Legal hold: placed/lifted via explicit governance-role API call — protected even from bucket owners.

---

## 7. Security Controls Per Layer

| Layer | Encryption | Access | Network |
|---|---|---|---|
| Raw (S3) | SSE-KMS | Extraction service role (write); transformation role (read) | VPC endpoint only |
| Curated (S3) | SSE-KMS | Transformation role (write); analytics role (read) | VPC endpoint only |
| Analytics (S3) | SSE-KMS | Analytics role (write); BI/ML consumers (read, prefix-scoped) | VPC endpoint only |
| Secrets Manager | AWS-managed KMS | Per-source extraction role (GetSecretValue only) | VPC endpoint only |
| DynamoDB (watermark, config) | AWS-managed encryption | Extraction role (GetItem, PutItem, UpdateItem on specific table) | VPC endpoint only |
| CloudWatch Logs | AWS-managed | All service roles (PutLogEvents on specific log group) | VPC endpoint |

All IAM roles are resource-scoped — no `Resource: "*"` permissions.

---

## 8. S3 Bucket and Prefix Layout

```
{env}-edl-raw/
└── raw/
    └── {source_id}/
        └── {entity_id}/
            └── {extraction_date}/          # YYYY-MM-DD
                ├── part-00000.parquet
                ├── part-00001.parquet
                └── metadata.json

{env}-edl-curated/
└── curated/
    └── {domain}/
        └── {entity_id}/
            └── {curated_date}/             # YYYY-MM-DD
                ├── part-00000.parquet
                └── part-00001.parquet
└── entity-resolution/              # resolution config (match rules + survivorship)
    └── {entity_type}/
        ├── match_rules_{version}.json  # e.g. match_rules_v1.json
        ├── survivorship_{version}.json # e.g. survivorship_v1.json
        └── latest.json                 # {"match_rules_version": "v1", ...}

{env}-edl-analytics/
└── analytics/
    └── {domain}/
        └── {entity_id}/
            └── year={YYYY}/month={MM}/day={DD}/
                └── part-NNNNN.parquet

{env}-edl-schema-snapshots/
└── schemas/
    └── {source_id}/
        └── {entity_id}/
            └── {schema_fingerprint}/
                └── {extraction_date}.json

{env}-edl-governance/
└── lineage/
    └── {entity_id}/
        └── {run_id}/
            ├── extraction-lineage.json
            └── transformation-lineage.json
```

---

## 9. DynamoDB Table Layout

| Table | Primary Key | Sort Key | Purpose |
|---|---|---|---|
| `{env}-entity-extraction-config` | `source_id` | `entity_id` | Entity extraction configuration |
| `{env}-watermark-repository` | `source_id` | `entity_id` | Watermark + optimistic version |
| `{env}-run-audit-log` | `run_id` | `stage` | Immutable per-stage audit trail |
| `{env}-source-onboarding` | `source_id` | `gate` | Source onboarding gate states |

---

## 10. Adding a New Connector

To add a new source (e.g., Dynamics 365):

1. **Create adapter directory:**
   ```
   connector_runtime/adapters/dynamics_365/
       __init__.py
       dynamics_365_connector.py         implements ConnectorInterface
       dynamics_365_raw_layer_writer.py  implements write_partition_streaming()
   ```

2. **Register the connector:**
   ```python
   # In your application bootstrap / Lambda handler
   from connector_runtime.registry import ConnectorRegistry
   from connector_runtime.adapters.dynamics_365 import Dynamics365Connector

   registry = ConnectorRegistry()
   registry.register("dynamics-365")(Dynamics365Connector)
   ```

3. **Seed entity configuration:**
   ```python
   # scripts/seed_entity_config.py
   # Add config record for each dynamics-365 entity
   ```

4. **Create EventBridge schedule:**
   ```python
   client = ExtractionScheduleClient(...)
   client.create_or_update_schedule(
       source_id="dynamics-365",
       entity_id="dynamics-365-contact",
       schedule_expression="cron(0 2 * * ? *)",  # 02:00 UTC daily
       connector_params={},
   )
   ```

5. **Run certification checklist:**
   ```python
   checklist = ConnectorCertificationChecklist(connector)
   result = checklist.run()
   assert result.all_passed
   ```

No changes to orchestration, transformation, governance, or observability modules are required.

---

## 11. Runbook Reference

| Scenario | Action |
|---|---|
| Extraction failed — DLQ message received | Use `RunReplayController.replay_extraction_window()` with the `run_id` from the DLQ message |
| Breaking schema drift — transformation blocked | Review drift report in S3, update field mapping rule set, manually approve transformation trigger |
| Watermark concurrency error | Automatic: run completes with `partial=True`, next scheduled run picks up from correct watermark |
| Circuit breaker open for a source | Investigate source availability; call `ExtractionRetryPolicy.reset_circuit(source_id, entity_id)` after remediation |
| New entity to add (same source) | Add `EntityExtractionConfig` record to DynamoDB, create EventBridge schedule — no code change required |
| Compliance legal hold required | Call `RetentionPolicyEnforcer.place_legal_hold(bucket, key)` with governance role credentials |
| Manual Athena query on curated data | Use `AthenaQueryClient.execute_query()` with the Glue catalog database name |
