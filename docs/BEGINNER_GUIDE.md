# Enterprise Data Lake Platform — Beginner's Guide

**Audience:** New developers, QA engineers, or anyone wanting to understand what this platform does and how it works — no prior AWS experience assumed.

---

## The Problem This Platform Solves

Your company stores business data in three completely different systems:

| System | What's in it | API type |
|---|---|---|
| **Salesforce** | Customers, contacts, opportunities | REST / SOQL |
| **NetSuite ERP** | Financials, invoices, vendors | REST |
| **MySQL RDS** | Orders, transactions, inventory | SQL |

Getting data out of all three for reporting or machine learning requires:
- Different credentials and APIs for each system
- Manual scripts that break when the source schema changes
- No history — you lose yesterday's data once today runs
- No quality checks — bad data silently reaches dashboards

**This platform automates all of it** — safely, incrementally, every night — and stores clean, versioned, auditable copies in AWS that anyone can query with standard SQL.

---

## Visual Diagrams

| Diagram | Description |
|---|---|
| [docs/architecture_diagram.svg](architecture_diagram.svg) | Full AWS architecture — all zones, modules, data flow arrows |
| [docs/functional_flow_beginner.svg](functional_flow_beginner.svg) | 9-step beginner walkthrough with code + config references |
| [architecture/enterprise_data_lake_architecture.svg](../architecture/enterprise_data_lake_architecture.svg) | Original detailed architecture diagram |

---

## Step-by-Step: How One Extraction Run Works

### Step 1 — Schedule Trigger (Every Night at 2 AM)

AWS **EventBridge Scheduler** fires a cron job — like a digital alarm clock — for each entity. There is one schedule per entity, staggered to avoid hitting the source systems at the same time.

```
salesforce-account  →  02:00 UTC daily
salesforce-contact  →  02:15 UTC daily
netsuite-customer   →  03:00 UTC daily
mysql-rds-orders    →  04:00 UTC daily
```

EventBridge sends a message to Step Functions saying: *"Time to run extraction for `salesforce / salesforce-account`."*

**Code:** `orchestration/event_bridge/`  
**Configure schedules:** `python scripts/trigger_extraction.py --create-schedule`  
**Infrastructure:** `infrastructure/modules/orchestration/`

---

### Step 2 — Step Functions (The Workflow Manager)

AWS **Step Functions** receives the trigger and acts like a project manager. It calls the extraction Lambda function and watches the result.

**If the Lambda fails** (e.g. Salesforce API is temporarily slow), Step Functions automatically retries:

```
1st failure  →  wait 30 seconds  →  retry
2nd failure  →  wait 2 minutes   →  retry
3rd failure  →  send to Dead-Letter Queue (DLQ) + alert ops team
```

The retry logic is built into the Step Functions state machine — no code changes needed to adjust retry behaviour.

**Code:** `orchestration/step_functions/extraction_workflow.py`  
**Infrastructure:** `infrastructure/modules/orchestration/`

---

### Step 3 — Load Configuration ("What Should I Extract?")

The Lambda function starts by reading the entity's configuration record from **DynamoDB**. This config tells the extractor exactly what to do:

```python
{
    "source_id":             "salesforce",
    "entity_id":             "salesforce-account",
    "load_type":             "incremental",    # or "full"
    "watermark_field":       "SystemModstamp", # timestamp field on the source
    "extraction_window_days": 1,               # max window per run
    "watermark_overlap_hours": 1,              # catch late-arriving records
    "field_mode":            "all",            # extract all fields
    "exclude_fields":        ["IsDeleted"],    # always skip these
    "output_format":         "parquet",
    "active":                True
}
```

**Code:** `connector_runtime/configuration_repository/configuration_repository.py`  
**Set up configs:** `python scripts/seed_entity_config.py --environment dev`

---

### Step 4 — Watermark Check ("Where Did We Last Stop?")

For incremental entities, the platform needs to know: *what's already been extracted?*

The **Watermark** is a timestamp bookmark stored in DynamoDB. It records the upper bound of the last fully successful extraction run. Only records with a `SystemModstamp` newer than the watermark are pulled this run — this is called **delta sync**.

```
Last run completed:    2026-06-14 02:00 UTC  (stored as watermark)
This run window:       2026-06-14 01:00 UTC  →  2026-06-15 02:00 UTC
                       (1 hr overlap to catch records with delayed timestamps)
```

If extraction fails partway through, the watermark is **not advanced** — the next run replays the same window, ensuring no records are skipped.

> **Full-load entities** (e.g. `salesforce-account` in default config) re-extract everything on each run and don't use a watermark.

**Code:** `watermark_management/watermark_repository/watermark_repository.py`

---

### Step 5 — Data Extraction ("Pull Records from Source")

The Lambda selects the correct **adapter** based on `source_id` and calls it to extract records.

Each adapter implements the same interface (`ConnectorInterface`) with these steps:
1. Fetch credentials live from **AWS Secrets Manager** (never stored in code)
2. Discover queryable fields from the source's metadata API (no hardcoded field lists)
3. Build a query for the watermark window (SOQL / REST query / SQL `WHERE updated_at > ?`)
4. Stream records in pages of ~2,000 records at a time

> **Why streaming?** Loading 500,000 records into memory at once would crash Lambda. Streaming processes each page, converts it to Parquet, writes it to S3, then discards it — so memory stays flat regardless of dataset size.

| Source | Query language | Adapter location |
|---|---|---|
| Salesforce | SOQL (`SELECT ... FROM Account WHERE SystemModstamp > ...`) | `connector_runtime/adapters/salesforce/` |
| NetSuite | REST API with filter params | `connector_runtime/adapters/netsuite/` |
| MySQL RDS | SQL (`SELECT ... WHERE updated_at > ?`) | `connector_runtime/adapters/mysql_rds/` |

**Code:** `connector_runtime/adapters/`  
**Credential format:** see [docs/DEPLOYMENT_GUIDE.md — Step 6.1](DEPLOYMENT_GUIDE.md#step-61--populate-source-credentials-in-secrets-manager)  
**Set credentials (all connectors):**
- `aws secretsmanager put-secret-value --secret-id dev/sources/salesforce/credentials ...`
- `aws secretsmanager put-secret-value --secret-id dev/sources/netsuite/credentials ...`
- `aws secretsmanager put-secret-value --secret-id dev/sources/mysql-rds/credentials ...`

**How to run Salesforce now (both ways):**

1. True local execution (runs handler on your laptop, uses real AWS + Salesforce):

```bash
source .venv/bin/activate
export AWS_ACCESS_KEY_ID=YOUR_DEV_ACCESS_KEY
export AWS_SECRET_ACCESS_KEY=YOUR_DEV_SECRET_KEY
export AWS_REGION=us-east-1
export RAW_S3_BUCKET=dev-raw-layer
export SCHEMA_SNAPSHOT_S3_BUCKET=dev-schema-snapshots
aws sts get-caller-identity
aws secretsmanager describe-secret --secret-id dev/sources/salesforce/credentials
python -c "from connector_runtime.extraction_pipeline_handler import lambda_handler; event={'source_id':'salesforce','entity_id':'salesforce-account','environment':'dev','connector_params':{'object_name':'Account'},'is_replay':False}; print(lambda_handler(event, None))"
```

2. AWS-backed execution (trigger from local; extraction runs in deployed Lambda):

```bash
source .venv/bin/activate
export AWS_PROFILE=dev
export AWS_REGION=us-east-1
aws sts get-caller-identity
python scripts/trigger_extraction.py \
  --source-id salesforce \
  --entity-id salesforce-account \
  --environment dev \
  --region us-east-1 \
  --param object_name=Account
```

For fuller troubleshooting and preflight details, see [docs/LOCAL_DEV_SETUP.md](LOCAL_DEV_SETUP.md#7-run-salesforce-connector-locally-two-ways).

---

### Step 6 — Raw Layer ("The Vault")

Extracted records are written as **Parquet files** to the S3 Raw Layer. This layer is the permanent, unmodified copy of what the source sent.

```
s3://dev-edl-raw/
└── salesforce/
    └── salesforce-account/
        └── 2026/
            └── 06/
                └── 15/
                    └── run-20260615-020012345678-a3f9c1d2.parquet
```

**Key properties of the Raw Layer:**
- **WORM-locked for 30 days** — files cannot be overwritten or deleted (S3 Object Lock)
- **KMS-encrypted** at rest with a dedicated CMK
- **Immutable** — transformations never modify raw files; they read and produce new files
- **7-year retention** for audit and compliance

> **Why Parquet?** Parquet is a columnar format that compresses well (often 10× smaller than CSV) and is natively supported by Athena, Spark, and pandas.

**Code:** `connector_runtime/adapters/salesforce/salesforce_raw_layer_writer.py`

---

### Step 7 — Schema Drift Check ("Did the Source Change Its Structure?")

After writing raw data, the platform takes a **schema snapshot** — a JSON record of every field name and type seen this run — and compares it to the snapshot from the previous run.

If the source system's team made changes (e.g. renamed a field, changed a number to a string), this detects it immediately.

**Drift classification:**

| Classification | Example | Effect |
|---|---|---|
| `NO_DRIFT` | Schema identical to last run | Proceed normally |
| `NON_BREAKING` | New optional field added | Proceed + alert downstream teams |
| `POTENTIALLY_BREAKING` | Field precision changed (INT→BIGINT) | Proceed + alert downstream teams |
| `BREAKING` | Field removed, or type changed (string→number) | **Transformation paused** + ops alerted |

When drift is `BREAKING`, raw data is still written (it can be replayed later), but the transformation step is halted until an engineer reviews and approves the change.

**Code:** `schema_management/drift_evaluation/drift_evaluator.py`  
**Snapshots stored at:** `s3://dev-edl-schema-snapshots/{source}/{entity}/{run_id}/`

---

### Step 8 — Transformation ("Clean, Rename, Validate")

A second Lambda pipeline reads raw Parquet files and produces clean, canonical data:

**Sub-step 8a — Field Mapping**  
Source fields are renamed to canonical business names using rules loaded from S3:

```json
{ "source_fields": ["Account_Name__c"], "canonical_field": "account_name", "transformation": "rename" }
{ "source_fields": ["AnnualRevenue"],   "canonical_field": "annual_revenue_usd", "transformation": "cast",
  "transformation_params": { "type": "decimal" } }
{ "source_fields": ["FirstName", "LastName"], "canonical_field": "full_name", "transformation": "concat",
  "transformation_params": { "separator": " " } }
```

Available transformations: `rename` · `cast` · `concat` · `date_format` · `mask`

**Sub-step 8b — Quality Evaluation**  
Each record is checked against quality rules: required fields present, values in valid ranges, regex patterns (e.g. email format), referential integrity. Records failing a `RAISE_ERROR` rule are quarantined (not written to curated layer).

**Sub-step 8c — PII Masking**  
Fields classified as sensitive (e.g. SSN, credit card) are masked before writing to the curated layer. Classification is defined in `governance/data_classification_policy.py`.

**Sub-step 8d — Curated Layer Write**  
Canonical records are written as Parquet to the Curated Layer (`s3://dev-edl-curated/`). A lineage record is written to S3 Governance, and the Glue Data Catalog is updated so Athena can query the new data immediately.

**Code:** `transformation/transformation_pipeline.py`  
**Field mapping location:** `s3://dev-edl-mapping-config/field-mappings/{source}/{entity}/latest.json`  
**How to publish a mapping:** see [docs/DEPLOYMENT_GUIDE.md — Section 7](DEPLOYMENT_GUIDE.md#7-field-mapping-configuration--where-and-how)

---

### Step 9 — Serving ("Ready for Business Use")

After the curated layer is updated:

**Amazon Athena** — BI tools and data analysts run standard SQL directly against S3 Parquet files. No database server to maintain; you pay only per query.

```sql
SELECT account_name, annual_revenue_usd
FROM salesforce_account
WHERE created_date >= '2026-01-01'
ORDER BY annual_revenue_usd DESC
LIMIT 100;
```

**Entity Resolution** — Records for the same real-world entity (e.g. the same customer appearing in Salesforce AND NetSuite) are matched and merged into a single **golden record** using the matching engine and survivorship policy.

Two entity types are declared today:
- **`company`** — merges Salesforce Account + NetSuite Customer into `canonical/company/` in the analytics layer
- **`person`** — normalises Salesforce Contact into `canonical/person/`

Match rules (which fields determine a match, and with what threshold) and survivorship policies (which source wins per field, what the output schema contains) are **declarative JSON configs** — not hardcoded in Python. They live in `config/entity_resolution/` (Git) and are published to S3 before the pipeline runs. The `ResolutionConfigRegistry` loads them at runtime. Adding a new entity type or changing a rule is a config file change, not a code deployment.

**Serving Store (RDS / DynamoDB, optional)** — For use cases that require sub-second operational reads. Pre-aggregated or canonical records are loaded via upsert for predictable low-latency access by internal APIs and microservices. Not required if Athena query latency is acceptable for your consumers — BI tools and reporting should query the Analytics layer through Athena, not the Serving Store.

**Downstream consumers:**
- BI tools (Tableau, Looker) via Athena JDBC driver
- ML pipelines (SageMaker) reading curated Parquet directly from S3
- Internal APIs reading from the Serving Store

**Code:** `transformation/athena_query_client.py` · `entity_resolution/` · `transformation/serving_store_loader.py`

---

## What Happens When Something Goes Wrong

### Lambda fails (transient error — API timeout, throttle)

Step Functions automatically retries with exponential backoff (up to 3 attempts). No manual action needed.

### Lambda fails 3 times (persistent error)

The failed run input is sent to the **SQS Dead-Letter Queue (DLQ)**. CloudWatch alarms fire and the ops team is emailed. Engineers inspect the DLQ message, fix the root cause, and replay by re-submitting the message to Step Functions with `is_replay: true`.

### Schema breaking drift detected

Transformation is automatically paused. An engineer reviews the drift report in S3, updates the field mapping rules, and re-triggers the transformation for the affected run.

### A past run needs to be re-run

Set `is_replay: true` and `replay_of_run_id: "run-xxx"` in the Step Functions input. The watermark is NOT advanced for replays — so re-running is always safe and idempotent.

---

## Querying Source Attribution (Field Provenance)

When you have a golden record with fields from multiple sources, you can instantly see which source won for each field — no S3 drilling needed.

**Example: Company entity (Salesforce + NetSuite)**

Every golden record includes a `field_provenance` JSON column documenting source attribution:

```sql
-- Athena: Query the Analytics Layer
SELECT
    golden_id,
    full_name,
    annual_revenue,
    industry,
    -- Extract source attribution for key fields
    json_extract_scalar(field_provenance, '$.full_name') AS full_name_source,
    json_extract_scalar(field_provenance, '$.annual_revenue') AS annual_revenue_source,
    json_extract_scalar(field_provenance, '$.industry') AS industry_source,
    json_extract_scalar(field_provenance, '$.credit_limit') AS credit_limit_source
FROM canonical_company
WHERE golden_id = 'acme-corp-001';
```

**Result:**
```
golden_id      | full_name  | annual_revenue | industry   | full_name_source | annual_revenue_source | industry_source | credit_limit_source
acme-corp-001  | Acme Corp  | 5000000        | Technology | netsuite         | salesforce            | salesforce      | netsuite
```

**In the Serving Store (MySQL RDS):**

```sql
-- Same query, using MySQL JSON functions
SELECT
    golden_id,
    full_name,
    annual_revenue,
    JSON_EXTRACT(field_provenance, '$.full_name') AS full_name_source,
    JSON_EXTRACT(field_provenance, '$.annual_revenue') AS annual_revenue_source,
    JSON_EXTRACT(field_provenance, '$.credit_limit') AS credit_limit_source
FROM canonical_company
WHERE golden_id = 'acme-corp-001';
```

**Audit: Which source won more fields?**

```sql
-- Count fields won by each source
SELECT
    golden_id,
    full_name,
    -- Salesforce wins
    size(filter(map_values(field_provenance), x -> x = 'salesforce')) AS salesforce_field_count,
    -- NetSuite wins
    size(filter(map_values(field_provenance), x -> x = 'netsuite')) AS netsuite_field_count
FROM canonical_company
WHERE golden_id = 'acme-corp-001';
```

**Why this design:**
- ✅ **No recomputation** — survivorship logic runs once, not per query
- ✅ **Instant answers** — source attribution is queryable directly from Athena or Serving Store
- ✅ **Audit-ready** — full traceability without drilling into S3 governance files
- ✅ **BI-friendly** — dashboards can color-code fields by source, flag unusual winners

---

## Understanding Golden Record Structure

Every golden record (entity-resolved canonical record) includes far more than just the 14 `output_fields` from the survivorship policy. Here's the complete schema:

**Business fields (14 declared in survivorship policy):**
- full_name, email_address, phone_number, annual_revenue, employee_count, credit_limit, outstanding_balance, currency_code, billing_country, billing_state, industry, is_active, created_date, last_modified_date

**System fields (automatically added for audit & matching traceability):**
1. **`golden_id`** — Deterministic ID (hash of contributing records). Stable across re-runs, meaning the same matched set always produces the same ID.
2. **`contributing_source_records`** — Array of source record IDs that matched and merged into this golden record (e.g., `["sf-account-001", "ns-customer-042"]`).
3. **`survivorship_version`** — Policy version that was applied (e.g., `"v1"`). Enables audit trails when policies evolve.
4. **`match_run_id`** — Run ID of the entity resolution execution. Links the golden record back to match decisions and audit logs.
5. **`field_provenance`** — JSON map showing which source won each field: `{"full_name": "netsuite", "annual_revenue": "salesforce", ...}` (see [Querying Source Attribution](#querying-source-attribution-field-provenance) above for query examples).

**Total: 19 columns per golden record** in the Parquet file.

**Partition structure** (part of S3 path, not individual columns):
- `golden_date=YYYY-MM-DD` — date the golden record was published
- `run_id={run_id}` — Step Functions execution ID

**Query example in Athena:**
```sql
-- See the full record with system fields
SELECT
    golden_id,
    full_name,
    annual_revenue,
    contributing_source_records,
    survivorship_version,
    match_run_id
FROM canonical_company
WHERE golden_id = 'acme-001';
```

---

## Key Design Principles

| Principle | What it means in practice |
|---|---|
| **Credentials never in code** | All passwords and API keys live only in AWS Secrets Manager; Lambda fetches them at runtime |
| **Raw data is immutable** | The raw S3 layer is write-once — no transformation ever modifies the original copy |
| **Memory-safe streaming** | Records are never loaded all at once; they flow through in pages to support millions of records |
| **Schema changes never break silently** | Drift evaluator detects and classifies every structural change before it reaches downstream systems |
| **Every run is fully auditable** | A `PipelineStageContract` record is written to DynamoDB at every stage of every run |
| **Replays are always safe** | Watermarks only advance on success; failed or replayed runs never produce duplicate records |
| **Infrastructure is code** | 100% Terraform — no manual AWS console clicks; every resource is versioned in git |
| **Least-privilege access** | Each Lambda role has IAM permissions only for the exact AWS resources it needs |

---

## Module Map (Where to Find What)

```
connector_runtime/          ← extraction: adapters, config, watermark, run lifecycle
  adapters/
    salesforce/             ← Salesforce connector (SOQL + raw writer)
    netsuite/               ← NetSuite connector (REST + raw writer)
    mysql_rds/              ← MySQL connector (SQL + raw writer)
  configuration_repository/ ← reads entity config from DynamoDB
  run_lifecycle/            ← run_id generation, audit log, DLQ routing
  registry.py               ← maps source_id strings to adapter classes

watermark_management/       ← delta sync bookmarks (DynamoDB-backed)

schema_management/          ← schema snapshots + drift detection
  snapshot_repository/      ← store/load schema JSON in S3
  drift_evaluation/         ← compare snapshots, classify changes

transformation/             ← clean + enrich raw data
  field_mapping/            ← rename/cast/concat rules (S3-backed JSON)
  quality_evaluation/       ← null checks, range checks, regex
  curated_layer_writer.py   ← write canonical Parquet to S3
  transformation_pipeline.py← orchestrates all transformation steps

entity_resolution/          ← golden record matching + merging
  matching_engine/          ← deterministic + probabilistic field matching
  resolution_config/        ← ResolutionConfigRegistry — S3-backed config loader
  survivorship_policy.py    ← per-field winner rules + output schema projection
  canonical_record_publisher/  ← write canonical (mastered) records to analytics S3

governance/                 ← data catalog, lineage, classification
  data_catalog_registration.py ← register datasets in Glue
  lineage_record.py         ← write lineage events to S3
  data_classification_policy.py ← PII field classification + masking
  retention_policy_enforcer.py  ← enforce S3 lifecycle rules

observability/              ← logging + metrics
  structured_logger.py      ← JSON structured logs (scrubs PII/secrets)
  metrics_emitter.py        ← CloudWatch custom metrics

orchestration/              ← scheduling + workflow
  event_bridge/             ← EventBridge schedule definitions
  step_functions/           ← state machine + retry policy

contracts/                  ← shared data contracts (Pydantic models)
  entity_configuration_contract.py ← entity config shape
  pipeline_stage_contract.py       ← run audit log shape
  observability_contract.py        ← log event shape

infrastructure/             ← all AWS infrastructure (Terraform)
  environments/dev|staging|prod/
  modules/kms|iam|storage|secrets|lambda_pipeline|orchestration|...

scripts/
  seed_entity_config.py     ← write entity configs to DynamoDB
  trigger_extraction.py     ← manually trigger or schedule runs
```

---

## Technology Stack Quick Reference

A complete list of every technology this platform uses, with a one-line description of its role.

### AWS Services

| Service | What it does in this platform |
|---|---|
| **Amazon EventBridge Scheduler** | Fires the "time to run" signal for each entity on a cron schedule |
| **AWS Step Functions** | Manages the pipeline as a 5-stage workflow; handles retries and failure routing |
| **AWS Lambda** | Runs all pipeline code (extraction, transformation, entity resolution, analytics publish, serving load) |
| **AWS ECS Fargate** | Runs large-volume extractions (> 5 M records/day) without Lambda timeout limits |
| **Amazon S3** | Stores all data: raw, curated, analytics, schema snapshots, configs, reports, lineage |
| **S3 Object Lock (GOVERNANCE)** | Makes raw data immutable — files cannot be deleted or overwritten for 7 years |
| **S3 Intelligent-Tiering** | Automatically moves old analytics data to cheaper storage tier |
| **Amazon DynamoDB** | Stores entity configs, watermarks, run audit logs, and source onboarding records |
| **AWS Secrets Manager** | Stores source API credentials securely; rotates them automatically |
| **AWS Glue Data Catalog** | Maintains a registry of all curated and analytics tables (like a library catalogue) |
| **Amazon Athena** | Lets you run SQL queries directly against S3 Parquet files (no database server needed) |
| **Amazon RDS MySQL 8** | Operational serving database for apps and dashboards requiring low-latency reads |
| **Amazon SQS** | Dead-Letter Queue — holds failed pipeline runs for replay |
| **Amazon CloudWatch** | Collects logs, custom metrics, and fires alarms when something goes wrong |
| **AWS X-Ray** | Traces every request through the system for debugging |
| **Amazon SNS** | Sends alert emails/PagerDuty notifications when alarms fire |
| **AWS KMS** | Manages encryption keys for all data at rest |
| **AWS IAM** | Controls who (which service role) can do what — no wildcard permissions |
| **Amazon VPC** | Private network — all platform services run isolated from the public internet |

### Source System APIs

| Source | API Used | Notes |
|---|---|---|
| **Salesforce CRM** | Bulk API 2.0 | Async, high-volume CSV streaming; handles millions of records |
| **NetSuite ERP** | SuiteQL REST API | SQL-like query language over REST |
| **MySQL RDS** | SQL via `pymysql` | Read-only; introspects schema via `INFORMATION_SCHEMA` |

### Python Libraries

| Library | Purpose |
|---|---|
| **Pydantic v2** | Validates all config and data models; catches bad data before it enters the pipeline |
| **structlog** | Writes structured JSON log events with automatic PII scrubbing |
| **boto3** | Python SDK for all AWS services |
| **pyarrow** | Reads and writes Apache Parquet files |
| **pymysql** | Connects to MySQL RDS |
| **requests** | Makes HTTP calls to Salesforce and NetSuite APIs |

### Data Format

| Format | Used for | Why |
|---|---|---|
| **Apache Parquet** | All data lake files (raw, curated, analytics) | Columnar format; 5–10× smaller than JSON; fast analytical queries |
| **JSON** | Config files, snapshots, reports, lineage records | Human-readable; version-controlled in Git |

### Infrastructure and CI/CD

| Tool | Purpose |
|---|---|
| **Terraform** ≥ 1.8 | Provisions all AWS infrastructure; 3 environments (dev/staging/prod) |
| **GitHub Actions** | 7-stage CI/CD pipeline: lint → typecheck → test → SAST → CVE → IaC → Terraform validate |
| **Ruff** | Python code linter |
| **mypy** | Python static type checker (strict mode) |
| **bandit** | Python security scanner (OWASP Top 10) |
| **pip-audit** | Checks Python dependencies for known CVEs |
| **checkov** | Scans Terraform code for security misconfigurations |
| **pytest + moto** | Unit/integration tests with AWS mocking (no real AWS needed for local tests) |

---

## Related Documentation

| Document | What it covers |
|---|---|
| [docs/DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) | Step-by-step instructions to deploy the platform to AWS |
| [docs/PLATFORM_FLOW.md](PLATFORM_FLOW.md) | Deep technical flow with all 15 internal pipeline stages |
| [docs/EXECUTIVE_OVERVIEW.md](EXECUTIVE_OVERVIEW.md) | Business outcomes, schedules, compliance — for leadership |
| [README.md](../README.md) | Developer setup, commands reference |
