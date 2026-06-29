# Enterprise Data Lake — Full Pipeline Flow

> **Spec version:** 2.0 | **Last updated:** 2026-06-29

> **Dev status:** ✅ All stages deployed and live. Data flowing end-to-end: Salesforce (Account, Contact) + MySQL RDS (Contracts). Entity resolution and analytics publisher both operational.

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
│  Salesforce CRM ✅  │  NetSuite ERP 🔲  │  MySQL RDS ✅  │  Future connectors  │
└────────────────────────────────┬─────────────────────────────────────────────┘
                                 │ full/incremental extraction (watermark-based)
                                 ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  ORCHESTRATION LAYER                                                         │
│  EventBridge Scheduler → Step Functions (chained 5-stage state machine)     │
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
│  curated/ — domain datasets     │
│  canonical/ — mastered entities │
│  Glue-catalogued, Athena-ready  │
└──────────────┬──────────────────┘
               │
               ▼
┌─────────────────────────────────┐
│  SERVING STORE (MySQL RDS)      │
│  Operational APIs, Applications │
└─────────────────────────────────┘
```

---

## 2. Data Layer Definitions

| Layer | Purpose | Storage | Format | Mutability |
|---|---|---|---|---|
| **Raw** | Exact copy of source data, no transformation | `{env}-edl-raw-layer` S3 | Parquet (large_utf8 columns) | Immutable — Object Lock GOVERNANCE |
| **Curated** | Per-source standardised data with canonical field names, type-cast, quality-checked, PII masked | `{env}-edl-curated-layer` S3 | Parquet (Snappy) | Append-only per run_id partition |
| **Analytics** | Consumption-optimised, Glue-catalogued datasets for Athena/BI. Contains curated domain datasets (`curated/` prefix) and canonical (entity-resolved) outputs (`canonical/` prefix) | `{env}-edl-analytics-layer` S3 | Parquet (Snappy) | Append-only |
| **Serving Store** | Optional operational store for low-latency API and application reads | MySQL RDS (private VPC) | SQL rows | Upsert (REPLACE INTO) |

---

## 3. End-to-End Pipeline Flow

```
EventBridge Scheduler (cron per entity)
          │
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
  │  9. Advance watermark (success only)                     │         │
  │  10. Emit TRANSFORMATION_TRIGGER stage event             │         │
  └──────────────────────┬──────────────────────────────────┘         │
                         │                                             │
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
  │  5. Evaluate quality policy                              │         │
  │  6. Write canonical records to curated layer (Parquet)   │         │
  │  7. Register dataset in Glue Catalog                     │         │
  │  8. Emit lineage record                                  │         │
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
  │     (s3://{analytics-layer}/canonical/...)          │         │
  │  6. Emit match statistics + lineage                      │         │
  └──────────────────────┬──────────────────────────────────┘         │
                         │                                             │
                         ▼                                            │
  ┌─────────────────────────────────────────────────────────┐         │
  │  STAGE D — ANALYTICS LAYER PUBLISH (Lambda)             │         │
  │                                                          │         │
  │  1. Promote curated domain datasets to optimised Parquet  │         │
  │  2. Write consumption-ready Parquet to analytics layer   │         │
  │  3. Register/update Glue Catalog table                   │         │
  │  4. Emit lineage record                                  │         │
  └──────────────────────┬──────────────────────────────────┘         │
                         │                                             │
                         ▼                                            │
  ┌─────────────────────────────────────────────────────────┐         │
  │  STAGE E — SERVING STORE LOAD (Lambda)                  │         │
  │                                                          │         │
  │  1. Read analytics Parquet from S3                       │         │
  │  2. Retrieve DB credentials from Secrets Manager         │         │
  │  3. CREATE TABLE IF NOT EXISTS (schema inferred)         │         │
  │  4. REPLACE INTO (idempotent upsert)                     │         │
  │  5. Emit load metrics                                    │         │
  └──────────────────────┬──────────────────────────────────┘         │
                         │                                             │
                         ▼                                            │
              PIPELINE COMPLETE ◄───────────────────────────────────┘
              (all stages succeeded)
```

---

## 4. Stage-by-Stage Reference

### Stage 1 — Event Scheduling

**Component:** Amazon EventBridge Scheduler  
**Trigger:** Cron expression per source/entity (configured per entity at runtime via `ExtractionScheduleClient`)  
**Purpose:** Fires a Step Functions execution on schedule without any manual intervention.  
**Key behaviour:**
- One schedule per entity (e.g. `salesforce--salesforce-account`, `netsuite--netsuite-customer`)
- Schedules are data — managed at runtime, not in Terraform
- Passes `source_id`, `entity_id`, `environment`, `connector_params` as execution input

**What can go wrong:** Schedule disabled, IAM execution role missing `sfn:StartExecution`, wrong state machine ARN.

---

### Stage 2 — Step Functions Orchestration

**Component:** AWS Step Functions Standard Workflow (staging/prod) or Express Workflow (dev)  
**Purpose:** Chains all five Lambda stages with explicit branching logic, retry policies, and failure routing.  
**Key behaviour:**
- Reads `transformation_blocked` from extraction output — skips stages B–E if breaking drift detected
- Reads `is_publication_blocked` from transformation output — skips entity resolution and downstream if quality blocks publication
- Retries transient Lambda errors with exponential backoff (3 attempts, 10s initial, 2× backoff)
- Terminal failures route to `PipelineFailed` state and enqueue to DLQ

**Branching logic:**

```
Extraction succeeded?
  └─ transformation_blocked=true  → STOP (drift alert fired)
  └─ transformation_blocked=false → Transformation

Transformation succeeded?
  └─ is_publication_blocked=true  → STOP (quality alert fired)
  └─ is_publication_blocked=false → Entity Resolution

Entity Resolution → Analytics Publish → Serving Store Load → COMPLETE
```

---

### Stage 3 — Configuration Load

**Component:** `ConfigurationRepositoryClient` (DynamoDB backend)  
**Purpose:** Loads `EntityExtractionConfig` for the requested source/entity. Validates config before any AWS or source API call is made.  
**Key fields read:** `load_type`, `watermark_field`, `extraction_window_days`, `field_mode`, `include_fields`, `exclude_fields`, `output_format`  
**Failure behaviour:** Raises `ConfigurationNotFoundError` → pipeline aborts, DLQ entry created.

---

### Stage 4 — Credential Retrieval

**Component:** AWS Secrets Manager  
**Purpose:** Retrieves short-lived source credentials (OAuth tokens, API keys, DB passwords). Credentials never appear in code, environment variables, or logs.  
**Secret path pattern:** `{environment}/sources/{source}/credentials`  
**Failure behaviour:** Raises credential error → classified as `DETERMINISTIC_INVALID_CREDENTIALS` → no retry.

---

### Stage 5 — Metadata Discovery

**Component:** Connector-specific metadata client (`SalesforceMetadataDiscoveryClient`, `NetSuiteMetadataAdapter`, `MySqlSchemaIntrospectionClient`)  
**Purpose:** Discovers all queryable fields from the source at runtime — no hardcoded schema. Produces a `FieldContract` used by query builder and schema snapshot.  
**Key output:** `FieldContract` — list of `FieldDescriptor` objects (name, type, precision, nullable, queryable flags)  
**Failure behaviour:** Raises metadata error → pipeline aborts.

---

### Stage 6 — Query Construction

**Component:** `SalesforceSoqlQueryBuilder`, `NetSuiteIncrementalQueryPlanner`, MySQL parameterized query  
**Purpose:** Builds a parameterized extraction query incorporating watermark bounds for incremental loads. Values are **never string-interpolated** — always bound as parameters (SQL injection prevention).  
**Key output:** `QueryContract` — `query_text` with named placeholders + `query_parameters` dict

---

### Stage 7 — Extraction

**Component:** `SalesforceBulkQueryJobController` (Bulk API 2.0), `NetSuiteConnector` (SuiteQL REST), `MySqlRdsConnector` (pymysql)  
**Purpose:** Executes the extraction query, streams records, writes raw Parquet to S3.  
**S3 partition scheme:** `s3://{bucket}/{source}/{entity_id}/extraction_date={YYYY-MM-DD}/run_id={run_id}/data.parquet`  
**Key properties:**
- All column values stored as `large_utf8` strings — no type loss, max compatibility
- Records written in chunks (50,000 per file for large volumes)
- `metadata.json` written alongside each Parquet file

---

### Stage 8 — Schema Snapshot

**Component:** `SchemaSnapshotRepository`  
**Purpose:** Persists the current field schema to S3 as an immutable snapshot after every successful run. Used by drift evaluator to compare against the next run's schema.  
**S3 path:** `s3://{bucket}/{source_id}/{entity_id}/{schema_version}/{extraction_date}.json`  
**Latest pointer:** `latest.json` updated after each write (avoids S3 listing latency).

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
**Purpose:** Advances the watermark to `upper_watermark` of the completed extraction window. Uses optimistic concurrency (DynamoDB `ConditionExpression` on `version`) to prevent concurrent runs from corrupting state.  
**Critical rule:** Watermark advances **only on full success**. Any failure at any earlier stage leaves the watermark unchanged, enabling safe replay.

---

### Stage 12 — Transformation (Raw → Curated)

**Component:** `TransformationPipeline`  
**Purpose:** Reads raw Parquet, applies field mappings, evaluates quality, writes canonical records to curated layer.

**Field mapping system** (see also §5):
- Rule set loaded from S3: `s3://{bucket}/field-mappings/{source_id}/{entity_id}/{version}.json`
- If no rule set exists, records pass through as-is (identity mode — logged as warning)
- Rules applied per record: rename, concat, date_format, cast, boolean, mask

**Quality evaluation:**
- `null_check` — required fields must be non-null
- `range_check` — numeric bounds validation
- `pattern_check` — regex match
- `allowed_values` — enum validation
- `WARNING` severity: publication continues, violations logged
- `BLOCKING` severity: publication halted, downstream paused

**Outputs:**
- Curated Parquet: `s3://{bucket}/curated/{domain}/{entity_id}/curated_date={date}/run_id={run_id}/data.parquet`
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

**Defined entity types:**

| Entity type | Sources merged | Output prefix |
|---|---|---|
| `company` | Salesforce Account + NetSuite Customer | `canonical/company/` |
| `person` | Salesforce Contact | `canonical/person/` |

---

### Stage 14 — Golden Record Publish

**Component:** `GoldenRecordPublisher`, `GoldenRecordSurvivorshipPolicy`, `ResolutionConfigRegistry`  
**Purpose:** Applies survivorship rules to each match cluster to produce one trusted record per real-world entity.  
**Survivorship strategies per field:**
- `SOURCE_PRIORITY` — prefer the value from the highest-ranked source (e.g. NetSuite > Salesforce for `full_name`)
- `MOST_RECENT` — prefer the value with the latest timestamp
- `LONGEST` — prefer the longest non-null string
- `FIRST_NON_NULL` — use first available value in source priority order

**Field provenance tracking:**  
Every golden record includes a `field_provenance` column — a JSON map documenting which source system won for each output field (via the survivorship rules above). This enables instant source attribution queries in Athena or the Serving Store without re-computation. Example queries available in [PLATFORM_FLOW.md: Field Provenance and Source Attribution](PLATFORM_FLOW.md#field-provenance-and-source-attribution).

**System fields automatically added:**
- `golden_id` — deterministic ID stable across re-runs
- `contributing_source_records` — array of source record IDs that formed this golden record
- `survivorship_version` — policy version applied (e.g., "v1")
- `match_run_id` — entity resolution run ID
- `field_provenance` — JSON map of field winners (see above)

**Total: 19 fields** (14 declared output_fields + 5 system fields). See [PLATFORM_FLOW: System Fields in Golden Records](PLATFORM_FLOW.md#system-fields-in-golden-records) for schema table.

**Output schema projection (`output_fields`):**  
Each survivorship policy declares an explicit `output_fields` list. Only those fields appear in the canonical Parquet files — source-internal IDs, duplicate name variants, and system-only fields are excluded. Empty `output_fields` = pass-through (used only in tests).

**Production entry point — `GoldenRecordPublisher.from_registry()`:**
```python
registry = ResolutionConfigRegistry(s3_bucket="dev-edl-curated", region_name="us-east-1")
publisher = GoldenRecordPublisher.from_registry(
    registry=registry,
    entity_type="company",
    analytics_s3_bucket="dev-edl-analytics",
    region_name="us-east-1",
)
```
The registry resolves the `latest` version pointer, loads and caches both JSON configs, and constructs the publisher. No rule set or policy is ever hardcoded in the Lambda handler.

**Key output:** Golden records with `golden_id`, `contributing_source_records`, `survivorship_version`, `match_run_id`, and only the fields declared in `output_fields`. Written to:
```
s3://{analytics-layer}/canonical/{entity_type}/golden_date={date}/run_id={run_id}/golden.parquet
s3://{analytics-layer}/canonical/{entity_type}/match-decisions/{run_id}/decisions.json
```
**Lineage:** Every field traces back to its contributing source record.

---

### Stage 15 — Analytics Layer Publish

**Component:** `AnalyticsLayerPublisher`  
**Purpose:** Promotes curated domain datasets to consumption-optimised Parquet in the analytics layer and registers/updates Glue Catalog tables. The analytics layer (`{env}-edl-analytics-layer`) is the single bucket for all consumption — it holds curated domain datasets under the `curated/` prefix and canonical (entity-resolved) outputs under the `canonical/` prefix.  
**Partition scheme:** `s3://{bucket}/analytics/{domain}/{entity_id}/analytics_date={date}/run_id={run_id}/data.parquet`  
**Consumers:** Athena, QuickSight, ML feature stores, data science notebooks.

---

### Stage 16 — Serving Store Load

**Component:** `ServingStoreLoader`  
**Purpose:** Loads analytics records into a MySQL RDS serving database for BI tools and applications.  
**Key properties:**
- Table schema inferred from Parquet schema — no hardcoded DDL
- `REPLACE INTO` — idempotent upsert, safe to re-run
- All SQL parameterized — no string interpolation of column names or values
- Credentials exclusively from Secrets Manager

---

## 5. Field Mapping System

Field mappings define how source field names and types are transformed into canonical domain model fields at the Raw → Curated stage.

### Config file location (Git)

```
config/field_mappings/
  salesforce/
    salesforce-account/v1.json
    salesforce-contact/v1.json
  netsuite/
    netsuite-customer/v1.json
  mysql-rds/
    mysql-rds-orders/v1.json
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
```

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
  --table-name dev-entity-extraction-config \
  --key '{"source_id":{"S":"salesforce"},"entity_id":{"S":"salesforce-account"}}'

# 3. Field mapping published to S3
aws s3 ls s3://dev-edl-curated-layer/field-mappings/salesforce/salesforce-account/

# 4. Source credentials exist in Secrets Manager (pick the source you run)
aws secretsmanager describe-secret \
  --secret-id dev/sources/salesforce/credentials
aws secretsmanager describe-secret \
  --secret-id dev/sources/netsuite/credentials
aws secretsmanager describe-secret \
  --secret-id dev/sources/mysql-rds/credentials

# 5. Current watermark state
aws dynamodb get-item \
  --table-name dev-watermark-repository \
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
aws s3 ls s3://dev-edl-raw-layer/salesforce/salesforce-account/ --recursive

# Watermark advanced
aws dynamodb get-item \
  --table-name dev-watermark-repository \
  --key '{"source_id":{"S":"salesforce"},"entity_id":{"S":"salesforce-account"}}'

# Schema snapshot written
aws s3 ls s3://dev-edl-schema-snapshots/salesforce/salesforce-account/ --recursive

# No breaking drift (check drift_report)
aws s3 cp s3://dev-edl-schema-snapshots/salesforce/salesforce-account/latest.json -
```

### Post-transformation verification

```bash
# Curated Parquet written
aws s3 ls s3://dev-edl-curated-layer/curated/customer/salesforce-account/ --recursive

# Quality report — check is_publication_blocked=false
aws s3 cp s3://dev-edl-curated-layer/quality-reports/salesforce/salesforce-account/<run_id>/quality-report.json -
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
| Stage 1 — Event Scheduling | Amazon EventBridge Scheduler |
| Stage 2 — Step Functions Orchestration | AWS Step Functions (Standard / Express Workflow) |
| Stage 3 — Configuration Load | Amazon DynamoDB (`{env}-entity-extraction-config`) |
| Stage 4 — Credential Retrieval | AWS Secrets Manager (`{env}/sources/{source}/credentials`) |
| Stage 5 — Metadata Discovery | Source APIs (no AWS; called from Lambda/ECS over VPC) |
| Stage 6 — Query Construction | In-process (no AWS service); ISO-8601 validated |
| Stage 7 — Extraction | AWS Lambda (< 5 M records) or AWS ECS Fargate (≥ 5 M records); Amazon S3 (raw layer write) |
| Stage 8 — Schema Snapshot | Amazon S3 (`{env}-edl-schema-snapshots`) |
| Stage 9 — Drift Evaluation | In-process (no AWS service); writes drift report to Amazon S3 |
| Stage 10 — Raw Layer Write | Amazon S3 (Object Lock GOVERNANCE); CloudWatch metric emit |
| Stage 11 — Watermark Update | Amazon DynamoDB (`{env}-watermark-repository`; conditional put) |
| Stage 12 — Transformation | AWS Lambda or AWS Glue; Amazon S3 (curated layer); AWS Glue Data Catalog |
| Stage 13 — Entity Resolution | AWS Lambda; Amazon S3 (curated source read + analytics write) |
| Stage 14 — Golden Record Publish | AWS Lambda; Amazon S3 (analytics layer `canonical/` prefix) |
| Stage 15 — Analytics Layer Publish | Amazon S3 (analytics layer); AWS Glue Data Catalog |
| Stage 16 — Serving Store Load | Amazon RDS MySQL 8 (private VPC); AWS Secrets Manager |
| All stages | Amazon CloudWatch Logs; Amazon CloudWatch Metrics; AWS X-Ray; Amazon SQS (DLQ) |

### Python Libraries by Component

| Component | Key Libraries |
|---|---|
| Connector Runtime (all connectors) | `boto3`, `pyarrow`, `pydantic` ≥ 2.7, `structlog` ≥ 24.4 |
| Salesforce connector | `requests` (OAuth 2.0 client credentials); Bulk API 2.0 CSV streaming |
| NetSuite connector | `requests` (OAuth 1.0a); SuiteQL REST JSON |
| MySQL RDS connector | `pymysql`; `INFORMATION_SCHEMA` introspection |
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
| EventBridge schedules | Managed at runtime via `extraction_schedule_client.py` | Schedule names follow `{source_id}--{entity_id}` |
