# Glossary & Key Terminology

**For:** All stakeholders  
**Purpose:** Define technical and business terms used throughout documentation  
**Last updated:** 2026-06-29

---

## Core Concepts

### Data Lake
A centralized repository that stores raw, structured data from multiple sources in its native format, enabling analytics, reporting, and machine learning at scale.

**In our context:** S3 buckets (raw, curated, analytics layers) + metadata repository (DynamoDB) + query engine (Athena).

---

### ETL / ELT

**ETL** = Extract → Transform → Load (traditional approach; transform before storage)  
**ELT** = Extract → Load → Transform (modern approach; load raw first, transform later)

**Our platform uses ELT:**
- Extract raw data → Load to S3 (immutable)
- Later → Transform (field mapping, quality checks) → Load to curated layer

**Benefit:** If transformation logic changes, replay from raw without re-extracting from source.

---

### Extraction
The process of retrieving data from a source system (Salesforce, NetSuite, MySQL) and copying it to the data lake's raw layer.

**Types:**
- **Full extract:** All records from source (e.g., first-time load, or reference data)
- **Incremental extract:** Only records changed since last extraction (faster, using watermark)

---

### Transformation
Process of converting raw source data into standardized, business-ready format.

**Steps:**
1. Field mapping (rename `Account_Name__c` → `account_name`)
2. Type casting (convert strings to numbers/dates)
3. PII masking (redact sensitive fields)
4. Quality validation (reject bad records)

---

### Watermark
A timestamp bookmark that records exactly where an incremental extraction last stopped.

**Example:**
- Last successful extraction: 2026-06-15 02:00 UTC
- Watermark stored: "2026-06-15T02:00:00Z"
- Next extraction: pull records modified after that timestamp

**Benefit:** Avoids re-extracting entire dataset on each run (delta sync).

---

### Schema
The structure of data — list of field names, types, sizes, and nullability.

**Example (Salesforce Account):**
```
{
  "fields": [
    {"name": "Id", "type": "string", "length": 18},
    {"name": "Name", "type": "string", "length": 255, "nullable": false},
    {"name": "Revenue", "type": "double", "nullable": true}
  ]
}
```

---

### Schema Drift
A change in source system schema compared to the previous extraction.

**Types:**
- **No drift:** Schema unchanged (expected)
- **Non-breaking drift:** New optional field added (safe to ignore)
- **Potentially breaking drift:** Field length reduced (may cause data loss)
- **Breaking drift:** Field removed or made mandatory (stops pipeline; requires review)

---

### Golden Record
A single, authoritative, consolidated view of an entity (customer, company, product) across all source systems.

**Example:** "John Smith" appears as:
- `Contact` in Salesforce
- `CustomerName` in NetSuite
- `user_name` in MySQL Orders

Platform matches them → creates one "golden record" with:
- Unified ID (`golden_id`)
- Merged fields (address from Salesforce, phone from NetSuite)
- Survivorship rules (which source's data wins for each field)

---

### Entity Resolution
The process of identifying and matching the same real-world entity (person, company, product) across multiple source systems.

**Methods:**
- **Deterministic:** Exact match (e.g., SSN matches exactly → same person)
- **Probabilistic:** Score-based match (e.g., name similarity > 95% + address match → likely same person)

---

### PII (Personally Identifiable Information)
Data that can identify an individual (name, email, phone, SSN, credit card, address).

**Our approach:**
- Raw layer: Stores PII (secured; only extraction team + compliance access)
- Curated + Analytics layers: PII masked/redacted (safe for broader analyst access)

---

### Masking Strategy
How PII is obfuscated to prevent identification while preserving some utility.

| Field | Strategy | Example |
|---|---|---|
| Email | MASK_EMAIL | `john.smith@company.com` → `j****@company.com` |
| Phone | REDACT | `555-123-4567` → `XXXX-XXXX-XXXX` |
| SSN | TOKENIZE | `123-45-6789` → `tok_a3f9c1d2e5f` (irreversible) |
| Customer ID | HASH | `12345` → `9e3b0c44` (one-way hash) |
| Full address | REDACT | `123 Main St, NYC, NY 10001` → `REDACTED` |

---

### Lineage
A complete record of data's journey from source to final destination, showing every transformation and processing step.

**Captured automatically:**
- Source system → Raw layer (extraction lineage)
- Raw layer → Curated layer (transformation lineage)
- Curated → Analytics / Golden records (resolution lineage)

**Benefit:** Audit trail; ability to explain why an analytics number differs from source.

---

## AWS Services Used

### S3 (Simple Storage Service)
Cloud object storage (files, not databases).

**Our usage:**
- Raw layer bucket: `{env}-raw-layer` (immutable, 7-year retention)
- Curated layer bucket: `{env}-curated-layer` (transformation outputs)
- Analytics layer bucket: `{env}-analytics-layer` (BI-ready data)
- Schema snapshots bucket: `{env}-schema-snapshots` (field schemas per run)

---

### DynamoDB
Fully managed NoSQL database (key-value store).

**Our usage:**
- **Configuration table:** Entity extraction configs (what to extract, watermark field, etc.)
- **Watermark table:** Last successful extraction timestamp per entity
- **Audit log table:** Every pipeline stage records an entry (immutable log)
- **Onboarding registry:** 6-gate source onboarding approval status

---

### Lambda
Serverless compute (run code without managing servers).

**Our usage:**
- Extraction Lambda: Pulls data from Salesforce/NetSuite/MySQL, writes raw Parquet
- Transformation Lambda: Applies field mapping, PII masking, quality checks
- Entity resolution Lambda: Matches records across sources, creates golden records
- Serving store Lambda: Loads analytics data into RDS/DynamoDB

**Limits:**
- Max execution time: 15 minutes
- Max memory: 10 GB
- For very large extractions (> 10M records/day): Use ECS Fargate instead

---

### Step Functions
Serverless workflow orchestration (chains Lambda calls together).

**Our usage:** Defines the pipeline flow:
```
Extraction Lambda 
  → (if not blocking drift)
    → Transformation Lambda 
      → (if not quality blocking) 
        → Entity Resolution Lambda 
          → Analytics Publish Lambda 
            → Serving Store Load Lambda
```

Handles retries, branching logic, and failure routing automatically.

---

### EventBridge Scheduler
Cron job scheduling service (triggers tasks on schedule).

**Our usage:** Triggers extraction pipeline on schedule (e.g., 02:00 UTC daily for Salesforce Account).

---

### Secrets Manager
Centralized secret storage with encryption and rotation.

**Our usage:** Stores source system credentials:
- `prod/sources/salesforce/credentials` (OAuth tokens)
- `prod/sources/netsuite/credentials` (API keys)
- `prod/sources/mysql-rds/credentials` (DB password)
- `prod/sources/sage/intacct/credentials` (Intacct OAuth 2.0 client credentials)
- `prod/sources/sage/x3/credentials` (X3 OAuth 2.0 client credentials + folder name)

Credentials auto-rotated every 90 days; never logged or exposed in code.

---

### Athena
Serverless SQL query engine over S3 data.

**Our usage:** BI tools query analytics layer via Athena:
```sql
SELECT account_name, revenue FROM analytics.salesforce_account WHERE region = 'EMEA'
```

Charges: $5 per TB scanned (year/month/day partitioning reduces scan volume).

---

### Glue Catalog
AWS metadata repository (like a data dictionary).

**Our usage:**
- Registers all curated and analytics tables
- Provides schema discovery (columns, types, partitions)
- Integrates with Athena (Athena knows table schema automatically)

---

### CloudWatch
AWS monitoring & observability service.

**Our usage:**
- **Logs:** Structured JSON logs from Lambda (searchable, alertable)
- **Metrics:** Custom metrics (RecordsExtracted, QualityFailureRate, WatermarkLag)
- **Alarms:** Triggers SNS notifications when threshold exceeded (e.g., extraction failure)
- **Dashboard:** Unified view of pipeline health

---

### KMS (Key Management Service)
Hardware-backed encryption key management.

**Our usage:**
- One customer-managed CMK per environment
- All S3 buckets encrypted with this key
- Only authorized IAM roles can decrypt data

---

## Data Lake Layers Explained

### Raw Layer
**Purpose:** Immutable archive of source data, exactly as received.

- **Storage:** S3 Parquet files
- **Schema:** Source fields preserved (no renaming)
- **Retention:** 7 years
- **Mutability:** Write-once (Object Lock GOVERNANCE mode)
- **Access:** Extraction team write; transformation team read; governance team audit
- **PII:** Present (unmasked; access-controlled)
- **Partition:** source → entity → extraction_date → run_id

**Why?** If transformation logic is wrong, replay from raw instead of re-extracting from source (which may be slow or API-rate-limited).

---

### Curated Layer
**Purpose:** Per-source standardized, quality-checked data ready for analysis.

- **Storage:** S3 Parquet files
- **Schema:** Canonical field names (standardized across all sources)
- **Retention:** 3 years
- **Mutability:** Append-only (new run_id partition per extraction)
- **Access:** Transformation team write; analytics team read (prefix-scoped)
- **PII:** Masked/redacted
- **Quality:** Only records passing quality checks
- **Partition:** source → entity → extraction_date → run_id

**Data is here:** Ready for BI queries, but organized per source. (E.g., separate `curated.salesforce_customer` from `curated.netsuite_customer`).

---

### Analytics Layer
**Purpose:** Consumption-optimized datasets for BI, dashboards, ML models.

- **Storage:** S3 Parquet files
- **Schema:** Optimized for analytics (denormalized, pre-aggregated where useful)
- **Retention:** 1 year active; archival to Glacier after
- **Mutability:** Append-only per run_id
- **Access:** BI analysts, ML engineers (no access to raw or curated)
- **PII:** Fully masked
- **Partitions:** 
  - **`curated/`** prefix: domain datasets (one per entity type, all sources merged)
  - **`canonical/`** prefix: golden records (entity-resolved; one company, one person, etc.)

**Data here:** What analysts query for dashboards.

**Example:** Query one `canonical.company` table that merges Salesforce Accounts + NetSuite Customers (no need to join).

---

### Serving Store (Optional)
**Purpose:** Operational database for APIs and apps requiring sub-second queries.

- **Storage:** RDS (PostgreSQL), Redshift, or DynamoDB
- **Data source:** Analytics layer (read-only)
- **Load type:** REPLACE INTO (idempotent upsert)
- **Use cases:** Low-latency API responses, mobile app backends, real-time dashboards

**Not used for:** Report generation (use Athena instead; cheaper, doesn't require database).

---

## Governance & Compliance

### Data Classification
Categorizing data fields by sensitivity level.

| Level | Example fields | Masking required | Access control |
|---|---|---|---|
| **PUBLIC** | country_code, industry_type | No | Anyone |
| **INTERNAL** | employee_count, annual_revenue | No | Employees only |
| **SENSITIVE** | phone_number, email_address | Yes (MASK_EMAIL) | Approved teams only |
| **PII** | SSN, credit card, full address | Yes (REDACT/TOKENIZE) | Extraction team + compliance only |

---

### Data Retention
How long data is kept before deletion.

| Layer | Retention | Policy |
|---|---|---|
| Raw | 7 years | S3 Object Lock GOVERNANCE (compliance requirement) |
| Curated | 3 years | Standard S3 lifecycle rule |
| Analytics | 1 year | Active; then Glacier archive (cost optimization) |
| Serving Store | Per table | Usually 90–180 days (operational data) |

---

### Source Onboarding
Six-gate approval process before a new data source can be extracted.

| Gate | Approver | Checks |
|---|---|---|
| SOURCE_REGISTRATION | Platform team | Credentials stored, SLA agreement signed |
| CREDENTIAL_REGISTRATION | Security team | Secrets Manager entry created, rotation scheduled |
| ENTITY_MAPPING | Data team | DynamoDB configs created, field list complete |
| EXTRACTION_PROFILE | Platform team | Dry-run succeeded, schema snapshot captured |
| SECURITY_GOVERNANCE | CISO | Access model approved, classification policy confirmed |
| ACCEPTANCE_VALIDATION | Data team | Canary run passed, record counts match, quality checks ✓ |

No extraction can begin without all 6 gates passed.

---

### Audit Trail
Immutable record of every extraction, transformation, and data access.

**Captured:**
- Who ran what pipeline stage, when, with what inputs
- How many records processed, how many failed
- What schema changes detected
- Who accessed what data, when
- Every field mapping, PII masking decision

**Stored:**
- DynamoDB audit-log table (real-time queries)
- S3 lineage bucket (long-term archive, Glacier)
- CloudWatch Logs (searchable for 30 days, then archive)

---

## Common Abbreviations

| Abbr. | Meaning |
|---|---|
| **SLA** | Service Level Agreement (uptime commitment) |
| **SLO** | Service Level Objective (99.5% run completion rate) |
| **DLQ** | Dead-Letter Queue (failed message queue) |
| **IAM** | Identity & Access Management (AWS user/role/permission system) |
| **KMS** | Key Management Service (encryption key management) |
| **CMK** | Customer-Managed Key (encryption key you control) |
| **WORM** | Write-Once-Read-Many (immutable object storage) |
| **CSV** | Comma-Separated Values (text data format) |
| **JSON** | JavaScript Object Notation (text data format) |
| **Parquet** | Columnar data format (efficient, compressed) |
| **Athena** | AWS serverless SQL query engine |
| **Glue** | AWS data integration & catalog service |
| **GDPR** | General Data Protection Regulation (EU privacy law) |
| **CCPA** | California Consumer Privacy Act (US privacy law) |
| **SOC 2** | System and Organization Controls (audit standard) |
| **HIPAA** | Health Insurance Portability & Accountability Act (US healthcare privacy law) |
| **OWASP** | Open Web Application Security Project (security standards) |
| **PII** | Personally Identifiable Information (sensitive personal data) |
| **SIEM** | Security Information & Event Management (security monitoring) |

---

## Technology and Tools Glossary

Quick-reference definitions for every tool and service used in the platform.

| Term | Full Name | Role in Platform |
|---|---|---|
| **EventBridge** | Amazon EventBridge Scheduler | Fires cron-based pipeline triggers per entity |
| **Step Functions** | AWS Step Functions | Orchestrates the 5-stage pipeline; handles retries and branching |
| **Lambda** | AWS Lambda | Serverless Python compute for all pipeline stages |
| **Fargate** | AWS ECS Fargate | Container compute for large-volume extractions (> 5 M records/day) |
| **S3** | Amazon Simple Storage Service | Stores all data layers: raw, curated, analytics, snapshots, configs |
| **Object Lock** | S3 Object Lock (GOVERNANCE mode) | Makes raw data immutable; enforces 7-year retention |
| **Intelligent-Tiering** | S3 Intelligent-Tiering | Auto-moves analytics data to cheaper storage after 90 days of inactivity |
| **DynamoDB** | Amazon DynamoDB | NoSQL database for config, watermark state, audit log, onboarding records |
| **Secrets Manager** | AWS Secrets Manager | Secure credential store; auto-rotation; never in code or logs |
| **Glue Catalog** | AWS Glue Data Catalog | Metadata registry for curated and analytics tables |
| **Athena** | Amazon Athena | Serverless SQL query engine over S3 Parquet files |
| **RDS** | Amazon Relational Database Service (MySQL 8) | Serving store for operational apps and low-latency reads |
| **SQS** | Amazon Simple Queue Service | Dead-Letter Queue for failed pipeline runs; KMS-encrypted |
| **CloudWatch** | Amazon CloudWatch | Logs, custom metrics, alarms, dashboards |
| **X-Ray** | AWS X-Ray | Distributed tracing across all Lambda/service calls |
| **SNS** | Amazon Simple Notification Service | Alert fanout to email / PagerDuty |
| **KMS** | AWS Key Management Service | Customer-managed CMK; SSE-KMS encryption for all data at rest |
| **IAM** | AWS Identity and Access Management | Least-privilege service roles; no wildcard permissions |
| **VPC** | Amazon Virtual Private Cloud | Private network; no internet gateway; VPC Endpoints for AWS services |
| **Terraform** | HashiCorp Terraform ≥ 1.8 | Infrastructure as Code; provisions all AWS resources |
| **Python** | Python 3.14.x | Runtime language for all platform code |
| **Pydantic** | Pydantic v2 | Data model validation library; frozen, strict models |
| **structlog** | structlog ≥ 24.4 | Structured JSON logging with PII-scrubbing processor |
| **boto3** | AWS SDK for Python | Python library for all AWS service calls |
| **pyarrow** | Apache Arrow Python | Parquet file read/write |
| **pymysql** | PyMySQL | Python MySQL connector for RDS |
| **Ruff** | Ruff ≥ 0.5 | Python linter (enforces code style + security rules) |
| **mypy** | mypy ≥ 1.10 | Python static type checker (strict mode) |
| **bandit** | bandit ≥ 1.7 | Python SAST scanner (OWASP Top 10) |
| **pip-audit** | pip-audit ≥ 2.7 | Dependency CVE scanner |
| **checkov** | Checkov | Terraform IaC security scanner |
| **moto** | moto ≥ 5.0 | AWS service mocking library for unit tests |
| **Bulk API 2.0** | Salesforce Bulk API 2.0 | High-throughput async Salesforce data extraction API |
| **SuiteQL** | NetSuite SuiteQL REST API | SQL-like query API for NetSuite ERP |
| **Intacct REST API** | Sage Intacct REST API | JSON-POST query API with OAuth 2.0 client credentials; `ia::meta.next` cursor pagination |
| **OData v4** | Open Data Protocol version 4 | REST-based query standard used by Sage X3; supports `$select`, `$filter`, `$orderby`, `$top`; `@odata.nextLink` cursor pagination |
| **Sage ERP** | Sage Group ERP products | Family of enterprise accounting/ERP products; platform supports Intacct (cloud accounting) and X3 (enterprise ERP) |
| **HMAC-SHA256** | Hash-based Message Authentication Code | PII tokenisation algorithm (keyed; deterministic pseudonym) |
| **SHA-256** | Secure Hash Algorithm 256-bit | Field schema fingerprinting; irreversible hash masking |
| **Jaro-Winkler** | Jaro-Winkler string similarity | Name fuzzy-matching algorithm in entity resolution |
| **Jaccard** | Jaccard token-set similarity | Company-name word-overlap scoring in entity resolution |
| **Parquet** | Apache Parquet | Columnar binary file format (Snappy-compressed for curated/analytics; large_utf8 for raw) |
| **SOQL** | Salesforce Object Query Language | SQL-like query language for Salesforce data |

---

## Pronunciation Guide

- **Parquet:** "par-KAY" (not "PAR-quet")
- **DynamoDB:** "dy-NA-mo-DEE-bee"
- **Athena:** "uh-THEE-nuh"
- **Salesforce:** "SALES-force" (not "sales-FORCE")
- **NetSuite:** "NET-sweet"
- **KMS:** "KAY-em-ess" (not "kim-us")
- **IAM:** "eye-AM" (not "ee-AM")

---

**Last updated:** 2026-06-29  
**Owner:** Data Platform Team

