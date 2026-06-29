# Enterprise Data Lake Platform — Executive Overview

**Version:** 2.0  
**Date:** 2026-06-29  
**Classification:** Internal — Leadership Review  

> **Current Status:** Dev environment fully operational as of 2026-06-29. 34 companies, 49 persons, and 35,971 contracts are live and queryable via Athena. Staging and Production deployments are next.  
> For a concise leadership-ready summary, see [LEADERSHIP_BRIEF.md](LEADERSHIP_BRIEF.md).
**Audience:** Engineering leadership, product leadership, data governance, security, and finance stakeholders

---

## Table of Contents

1. [What We Built and Why](#1-what-we-built-and-why)
2. [Business Outcomes](#2-business-outcomes)
3. [How the Platform Works — Functional Walkthrough](#3-how-the-platform-works--functional-walkthrough)
4. [Data Flow: From Source to Business Insight](#4-data-flow-from-source-to-business-insight)
5. [Connected Data Sources](#5-connected-data-sources)
6. [Delta (Incremental) Sync — How We Stay Current](#6-delta-incremental-sync--how-we-stay-current)
7. [Extraction Schedules (Cron Job Reference)](#7-extraction-schedules-cron-job-reference)
8. [Least Privilege Access Model — Who Can Read What](#8-least-privilege-access-model--who-can-read-what)
9. [Data Layers Explained](#9-data-layers-explained)
10. [Data Quality and Governance](#10-data-quality-and-governance)
11. [Security Architecture Summary](#11-security-architecture-summary)
12. [Technology Stack and Tools](#12-technology-stack-and-tools)
13. [Operational Resilience](#13-operational-resilience)
14. [Scalability and Cost Profile](#14-scalability-and-cost-profile)
15. [Adding New Data Sources — Zero Code Changes](#15-adding-new-data-sources--zero-code-changes)
16. [Compliance and Audit Readiness](#16-compliance-and-audit-readiness)
17. [Key Metrics and SLOs](#17-key-metrics-and-slos)
18. [Roadmap](#18-roadmap)

---

## 1. What We Built and Why

### The Problem

Our organisation's data lived in silos:
- Customer records in **Salesforce** (CRM)
- Financial and order data in **NetSuite** (ERP)
- Transactional data in **MySQL RDS** (internal databases)

Each team had its own extract scripts — inconsistent, brittle, impossible to audit, and carrying serious security risks (credentials in scripts, no access control, no lineage).

Analytics teams waited days for data. Compliance teams had no record of who accessed what or when. The same customer could appear as three different entities across three systems with no way to resolve them.

### What We Built

An **Enterprise Data Lake Platform** — a production-grade, security-first, metadata-driven pipeline that:

- Continuously extracts data from all source systems on defined schedules
- Stores it in three governed layers (Raw, Curated, Analytics)
- Resolves the same entity across sources into a single "golden record"
- Enforces data classification, masking, and retention automatically
- Provides a full audit trail from source record to business insight
- Requires **zero code changes** to add a new data source or a new entity

---

## 2. Business Outcomes

| Outcome | Before | After |
|---|---|---|
| Time to data availability | 24–72 hours (manual) | 1–4 hours (automated, scheduled) |
| Customer entity resolution | 3 disconnected systems | Single golden record per customer |
| PII in analytics datasets | Uncontrolled | Masked/tokenised at pipeline level |
| Audit trail | None | Full lineage from source to serving |
| New source onboarding | 2–4 weeks (code change + deployment) | 2–3 days (configuration only) |
| Data quality visibility | No monitoring | Quality reports per entity per run |
| Credential security | Scripts and .env files | AWS Secrets Manager with auto-rotation |
| Compliance readiness | Manual documentation | Automated lineage + retention enforcement |

---

## 3. How the Platform Works — Functional Walkthrough

The platform runs as a **scheduled, fully automated pipeline**. The flow is:

```
SCHEDULE
    │
    ▼  (e.g. every day at 02:00 UTC for Salesforce Accounts)
EXTRACT — connect to source, discover what changed since last run, pull records
    │
    ▼
STORE RAW — write every record exactly as it came from the source (immutable)
    │
    ▼
TRANSFORM — map source fields to standard names, check data quality, mask PII
    │
    ▼
CURATE — publish a clean, trusted, business-ready version of the data
    │
    ▼
RESOLVE — match the same customer/supplier/product across Salesforce + NetSuite + MySQL
    │
    ▼
SERVE — load into the analytics database for BI tools, dashboards, and ML models
```

Each step produces a machine-readable audit record. No step is skipped. No data is lost. If anything fails, the platform queues a replay automatically.

---

## 4. Data Flow: From Source to Business Insight

```
┌─────────────────────────────────────────────────────────────────┐
│                      SOURCE SYSTEMS                             │
│   Salesforce (CRM) │ NetSuite (ERP) │ MySQL RDS (Transactional) │
└────────────┬────────┴───────┬────────┴──────────┬───────────────┘
             │                │                   │
             └───────────────►▼◄──────────────────┘
                     CONNECTOR RUNTIME
                  (discovers fields, builds query,
                   streams records in batches of 50k)
                              │
                   ┌──────────▼──────────┐
                   │     RAW LAYER       │
                   │  S3 — Parquet files │  ← Immutable. Every source record,
                   │  per entity per day │    exactly as received. 7-year retention.
                   └──────────┬──────────┘
                              │
                   ┌──────────▼──────────┐
                   │  TRANSFORMATION     │
                   │  Field mapping      │  ← Rename fields to standard names.
                   │  Quality checks     │    Reject/warn on bad data.
                   │  PII masking        │    Mask or tokenise sensitive fields.
                   └──────────┬──────────┘
                              │
                   ┌──────────▼──────────┐
                   │   CURATED LAYER     │
                   │  S3 — clean data    │  ← Trusted, standardised, PII-safe.
                   │  Glue catalog       │    Queryable via Athena immediately.
                   └──────────┬──────────┘
                              │
                   ┌──────────▼──────────┐
                   │  ENTITY RESOLUTION  │
                   │  Match + dedupe     │  ← "John Smith" in Salesforce and
                   │  across all sources │    "J. Smith" in NetSuite → same person.
                   └──────────┬──────────┘
                              │
                   ┌──────────▼──────────┐
                   │  ANALYTICS LAYER    │
                   │  S3 — partitioned   │  ← Optimised for BI and ML.
                   │  Golden records     │    Year/month/day partitions.
                   └──────────┬──────────┘
                              │
                   ┌──────────▼──────────┐
                   │   SERVING STORE     │
                   │  RDS / Redshift /   │  ← Powers low-latency APIs,
                   │  DynamoDB           │    apps, and microservices.
                   └─────────────────────┘
```

---

## 5. Connected Data Sources

| Source System | Type | Current Entities | Extraction Method |
|---|---|---|---|
| **Salesforce** | CRM | Account, Contact, Opportunity, Lead, Case (configurable — no hardcoded list) | Salesforce Bulk API 2.0 (high-volume, async) |
| **NetSuite** | ERP | Customer, Vendor, Invoice, Purchase Order, GL Journal (configurable) | SuiteQL REST API |
| **MySQL RDS** | Transactional DB | Orders, Products, Inventory, Users (configurable) | JDBC / SQLAlchemy read-only connection |

**Planned future sources** (configuration-only addition — no code change):  
Dynamics 365, HubSpot, SAP, PostgreSQL, REST APIs, CSV/Excel/SFTP

---

## 6. Delta (Incremental) Sync — How We Stay Current

### The Problem with Full Loads

Extracting every record every time a pipeline runs is wasteful and slow. A Salesforce org with 5 million accounts would take hours if extracted in full daily.

### How Delta Sync Works

The platform uses a **watermark** — a timestamp bookmark that records exactly where each pipeline last left off.

**Example (Salesforce Accounts):**

```
Run #1 (2026-06-14):
  Extract all Accounts where SystemModstamp >= 1970-01-01 AND < 2026-06-14
  → Writes watermark: "last successful: 2026-06-14T00:00:00Z"

Run #2 (2026-06-15):
  Extract Accounts where SystemModstamp >= 2026-06-14T00:00:00Z AND < 2026-06-15T00:00:00Z
  → Only records changed in the last 24 hours are extracted
  → Writes watermark: "last successful: 2026-06-15T00:00:00Z"
```

### Late-Arriving Data Protection

Sources sometimes update records with a slight delay. The platform supports a configurable **overlap window** (e.g., subtract 2 hours from the watermark lower bound) to catch late-arriving updates without re-processing the entire dataset.

### Watermark Safety Guarantees

- The watermark **never advances** if the extraction failed.
- If two extraction runs finish concurrently, only one wins (optimistic locking) — no gap, no duplicate.
- Replay is supported: any historical window can be re-extracted without corrupting the watermark.

### Full Load vs Incremental — Per Entity

| Entity | Load type | Reason |
|---|---|---|
| Salesforce Account | Incremental | High volume; changes frequently |
| Salesforce Opportunity | Incremental | Large dataset; use `CloseDate` watermark |
| NetSuite Invoice | Incremental | Append-heavy; use `dateCreated` watermark |
| Reference data (country codes, currency) | Full | Small; rarely changes; simpler |

Load type is set per entity in the configuration record — no code change to switch.

---

## 7. Extraction Schedules (Cron Job Reference)

Schedules are managed by **AWS EventBridge Scheduler**. Each entity has exactly one schedule. Schedules can be updated at any time without deployment.

### Production Schedule Reference

| Source | Entity | Schedule | UTC Time | Frequency | Notes |
|---|---|---|---|---|---|
| Salesforce | Account | `cron(0 2 * * ? *)` | 02:00 daily | Daily | After close of business in US/Pacific (previous day) |
| Salesforce | Contact | `cron(0 2 * * ? *)` | 02:00 daily | Daily | Co-scheduled with Account |
| Salesforce | Opportunity | `cron(0 3 * * ? *)` | 03:00 daily | Daily | Staggered to avoid concurrent Salesforce API load |
| Salesforce | Lead | `cron(0 3 * * ? *)` | 03:00 daily | Daily | |
| Salesforce | Case | `cron(0 4 * * ? *)` | 04:00 daily | Daily | |
| NetSuite | Customer | `cron(0 1 * * ? *)` | 01:00 daily | Daily | Before Salesforce load begins |
| NetSuite | Invoice | `cron(0 1 30 * ? *)` | 01:30 daily | Daily | |
| NetSuite | Vendor | `cron(0 5 * * ? *)` | 05:00 daily | Daily | Low priority; runs after primary sources |
| MySQL RDS | Orders | `cron(0/4 * * * ? *)` | Every 4 hours | 6× daily | High-frequency; near-real-time operational data |
| MySQL RDS | Products | `cron(0 6 * * ? *)` | 06:00 daily | Daily | Reference data; low change rate |
| MySQL RDS | Inventory | `cron(0/2 * * * ? *)` | Every 2 hours | 12× daily | Inventory is time-sensitive |

### How to Update a Schedule

No deployment required. The schedule can be updated via the platform API or CLI:

```bash
# Example: change Salesforce Account to run every 4 hours
python scripts/trigger_extraction.py --update-schedule \
  --source-id salesforce \
  --entity-id salesforce-account \
  --schedule "cron(0 */4 * * ? *)"
```

Changes take effect on the next trigger window.

### Schedule Naming Convention

Schedule names follow `{source_id}--{entity_id}` (e.g. `salesforce--salesforce-account`). Double-hyphen separates source and entity to avoid ambiguity.

---

## 8. Least Privilege Access Model — Who Can Read What

The platform enforces a **zero-trust, need-to-know** access model. Each pipeline stage runs under a dedicated IAM service role with the minimum permissions required — no shared credentials, no wildcard permissions.

### IAM Role Map

| Role | Can Read | Can Write | Notes |
|---|---|---|---|
| `extraction-service-role` | Raw S3 (write-only via put_object); Secrets Manager (source credentials, GetSecretValue only); DynamoDB config table (GetItem only); DynamoDB watermark table (GetItem, PutItem on own entities) | Raw S3 | Cannot read curated, analytics, or governance buckets |
| `transformation-service-role` | Raw S3 (read-only on own entity prefix); Mapping bucket; Quality policy bucket | Curated S3; Glue catalog | Cannot write to raw layer; cannot read Secrets Manager |
| `entity-resolution-role` | Curated S3 (read-only) | Analytics S3; Glue catalog | Scoped to resolution output prefix only |
| `analytics-serve-role` | Analytics S3 (read-only); Curated S3 (read-only) | Serving database | Read-only on data lakes |
| `governance-role` | All S3 buckets (metadata path only); DynamoDB audit tables | Governance S3; DynamoDB onboarding table; S3 Object Lock API | Only role that can place/lift legal holds |
| `ci-cd-deploy-role` | Terraform state bucket | IAM role updates (boundary-constrained); Lambda/ECS task deployments | Cannot access data buckets or Secrets Manager values |
| **BI / Analytics consumers** | Analytics S3 (read; prefix-scoped to approved datasets) | None | Individual user IAM roles or assumed role via Athena |
| **ML engineers** | Analytics S3 (read; feature store prefix) | Feature store S3 | No access to raw or curated layers |

### S3 Bucket Access Matrix

| Bucket | Extraction | Transformation | Entity Resolution | Analytics Serve | Governance | BI/Analytics |
|---|---|---|---|---|---|---|
| Raw (`{env}-edl-raw`) | **Write** | Read | ✗ | ✗ | Read (audit) | ✗ |
| Curated (`{env}-edl-curated`) | ✗ | **Write** | Read | Read | Read (audit) | ✗ |
| Analytics (`{env}-edl-analytics`) | ✗ | ✗ | **Write** | Read | Read (audit) | **Read (prefix-scoped)** |
| Schema Snapshots | Write | Read | ✗ | ✗ | Read | ✗ |
| Governance | ✗ | Write (lineage) | Write (lineage) | ✗ | **Write** | ✗ |
| Mapping / Quality | ✗ | Read | ✗ | ✗ | **Write** | ✗ |

### Source Credential Access

Each source has its own Secrets Manager secret:
- Path: `{environment}/{source_id}/credentials`
- Only the `extraction-service-role` for that source has `GetSecretValue` permission.
- Rotation is scheduled (every 90 days for Salesforce OAuth tokens, every 365 days for MySQL read-only passwords).
- Credentials are retrieved at runtime, held in memory for the duration of one extraction run, and never logged.

---

## 9. Data Layers Explained

### Raw Layer — The Source of Truth Archive

**Purpose:** Store every record exactly as it came from the source system. Never modified. Never deleted (within retention window).

- Format: Apache Parquet (columnar, compressed — typically 5–10× smaller than JSON)
- Partitioned by source → entity → extraction date
- Immutable: old files are never overwritten; new files are added alongside
- Retention: 7 years (S3 Object Lock, GOVERNANCE mode)
- Access: extraction service role (write); transformation role (read)
- **No PII masking** — raw data is as-received. Access is strictly controlled.

**Why keep raw?** If a transformation rule was wrong, or a field mapping was incorrect, you can replay from raw without going back to the source system. This is the single most important recovery capability in the platform.

### Curated Layer — The Trusted Business Layer

**Purpose:** Clean, standardised, domain-aligned data ready for business use.

- Source fields mapped to canonical names (e.g. `Account_Name__c` → `account_name`)
- PII fields masked or tokenised per classification policy
- Quality-checked records only (blocking violations prevent publication)
- Registered in AWS Glue Data Catalog — immediately queryable via Athena
- Retention: 3 years
- Access: transformation role (write); analytics roles (read)

**What "curated" means:** A business analyst querying `curated.customer` sees a clean, consistent schema regardless of which source system the data originated from.

### Analytics Layer — The Consumption Layer

**Purpose:** Optimised for BI tools, dashboards, and ML models.

- Year/month/day partitioned for efficient time-range queries
- Contains **curated domain datasets** and **canonical entity records** (entity-resolved golden records)
- Two canonical entity types currently defined:
  - **`company`** — merges Salesforce Account + NetSuite Customer into a single trusted company profile
  - **`person`** — normalises Salesforce Contact into a canonical person record
- Every golden record includes **5 system fields** beyond the 14 business `output_fields`: `golden_id`, `contributing_source_records`, `survivorship_version`, `match_run_id`, and `field_provenance`. See [PLATFORM_FLOW: System Fields in Golden Records](../PLATFORM_FLOW.md#system-fields-in-golden-records) for details.
- Registered in Glue catalog; Athena workgroup configured per team
- Retention: 1 year active; archival to Glacier after
- Access: read-only for approved BI users and ML engineers (prefix-scoped)

### Serving Store — The Application Layer

**Purpose:** Powers operational applications and APIs that require low-latency reads and cannot query S3 directly.

- Loaded from the analytics layer via upsert
- Target: RDS (PostgreSQL), Redshift, or DynamoDB (depending on deployment profile)
- Supports both full-replace and incremental-merge load modes
- **Not the same as Athena reporting** — Athena is the query engine that runs SQL over the Analytics layer (S3 Parquet) for BI tools, dashboards, and ad-hoc analysis. Serving Store is the operational store for predictable sub-second API reads at high concurrency.

---

## 10. Data Quality and Governance

### Quality Checks Per Entity

Every entity has a quality policy that runs before curated publication:

| Check type | Example | Effect when violated |
|---|---|---|
| Null check | `customer_id` must not be null | **BLOCKING** — curated write skipped |
| Pattern check | Email must match `^[^@]+@[^@]+\.[^@]+$` | BLOCKING or WARNING |
| Range check | `order_amount` must be between 0 and 10,000,000 | WARNING only |
| Enum check | `status` must be one of `[active, inactive, pending]` | BLOCKING |

A quality report is written to S3 for every run. When a BLOCKING violation occurs, a CloudWatch alarm fires and the on-call team is notified. The curated write is skipped for that run — the previous curated dataset remains unchanged.

### Schema Drift Detection

Every time data is extracted, the field schema is compared against the previous snapshot. Changes are classified:

| Change type | Example | Action |
|---|---|---|
| **No drift** | Nothing changed | Pipeline proceeds normally |
| **Non-breaking** | New optional field added | Pipeline proceeds; downstream teams notified |
| **Potentially breaking** | Field length reduced | Pipeline proceeds; manual review recommended |
| **Breaking** | Field removed or type changed | Raw data stored; **transformation blocked until reviewed** |

Breaking drift triggers a CloudWatch alarm and requires a governance review before transformation resumes. This prevents corrupt data from reaching the curated layer.

### Source Onboarding Governance

A new data source cannot be extracted until it passes **six mandatory gates**:

```
SOURCE_REGISTRATION  →  CREDENTIAL_REGISTRATION  →  ENTITY_MAPPING
        →  EXTRACTION_PROFILE  →  SECURITY_GOVERNANCE  →  ACCEPTANCE_VALIDATION
```

Each gate is recorded and immutably logged. A gate cannot be skipped without a written waiver (minimum 20-character justification, stored in the audit trail).

### Declarative Entity Resolution Configuration

Who counts as the "same company" across Salesforce and NetSuite, and which source wins when field values conflict, are declared as **versioned JSON config files** — not embedded in code. The files live in `config/entity_resolution/` (version-controlled in Git) and are published to S3 before each environment is activated.

This means:
- Adding a new entity type or changing a match threshold is a config file change, reviewed and approved like any data governance decision, with no Lambda deployment required
- Every historical version of the rules is retained; any pipeline run can be replayed with the exact rules that were active at the time
- The `output_fields` list in each survivorship policy is the authoritative schema contract for canonical analytics tables — only declared fields appear in Parquet output

---

## 11. Security Architecture Summary

The platform is built security-first, with controls embedded at every layer:

### Credential Management
- Source credentials stored exclusively in **AWS Secrets Manager**
- Separate secret per source system, per environment
- Automatic rotation scheduled per source type
- Credentials held in memory only for the duration of a single extraction run — never written to logs, files, or environment variables

### Encryption
- All data **at rest** encrypted with AWS KMS (SSE-KMS)
- All data **in transit** encrypted (TLS 1.2+ mandatory)
- All inter-service communication over AWS private endpoints (no public internet traversal)

### Network Isolation
- Platform runs in a private VPC with no internet gateway
- All AWS service access (S3, DynamoDB, Secrets Manager, CloudWatch) via VPC endpoints
- Source connectivity via AWS PrivateLink or VPN (no public credential exposure)

### PII and Sensitive Data
- Fields classified as PII are automatically masked before any write to the curated or analytics layer
- Masking strategies: redact, partial mask, tokenise (HMAC-SHA256 keyed), hash, full mask
- PII field names are never included in log output, quality reports, or drift alerts
- Classification policy is a configuration artefact — updated without code changes

### Audit Trail
- Every pipeline stage emits an immutable audit record to DynamoDB
- Every data write produces a lineage record in the governance S3 bucket
- CloudWatch logs capture all structured events; X-Ray traces all service calls
- S3 access logging enabled on all data buckets

### Least Privilege
- No IAM role has `Resource: "*"` or `Action: "*"` permissions
- All roles are scoped to specific tables, bucket prefixes, and actions
- CI/CD deployment role cannot access data buckets
- BI consumers can only read prefix-scoped analytics data

---

## 12. Technology Stack and Tools

The platform is built exclusively on proven, production-grade technologies. Every component is version-pinned, security-scanned, and infrastructure-as-code managed.

### Cloud Platform

| Layer | Technology | Notes |
|---|---|---|
| Cloud provider | **AWS (Amazon Web Services)** | All services; `us-east-1` default (configurable per environment) |
| Infrastructure as Code | **Terraform** ≥ 1.8, < 2.0 | AWS Provider ~> 5.0; all infrastructure version-controlled |

### Compute and Orchestration

| Component | Technology | Notes |
|---|---|---|
| Pipeline orchestration | **AWS Step Functions** | Standard Workflow (staging/prod); Express Workflow (dev) |
| Event scheduling | **Amazon EventBridge Scheduler** | Cron per entity; managed at runtime without deployment |
| Extraction runtime (small/medium) | **AWS Lambda** (Python 3.14) | Up to 15-minute timeout; streaming memory model |
| Extraction runtime (large volume) | **AWS ECS Fargate** | For datasets > 5 M records/day; no timeout limit |

### Storage

| Layer | Technology | Notes |
|---|---|---|
| Raw data | **Amazon S3** + S3 Object Lock (GOVERNANCE) | Immutable; 7-year retention; SSE-KMS encrypted |
| Curated data | **Amazon S3** | Parquet (Snappy); append-only per `run_id` partition |
| Analytics data | **Amazon S3** + S3 Intelligent-Tiering | Auto-moves to infrequent access after 90 days |
| Schema snapshots | **Amazon S3** | Immutable JSON per run; SHA-256 fingerprinted |
| Field mapping / entity resolution config | **Amazon S3** | Versioned JSON; latest-pointer pattern |
| Watermark state | **Amazon DynamoDB** | Point-in-time recovery; optimistic concurrency |
| Entity configuration | **Amazon DynamoDB** | `{env}-entity-extraction-config`; KMS encrypted |
| Run audit log | **Amazon DynamoDB** | TTL-enabled; GSI on source/entity/time |
| Serving store | **Amazon RDS MySQL 8** | Private VPC; read-only for analytics consumers |

### Data Format

| Format | Used for |
|---|---|
| **Apache Parquet** (Snappy compression) | All data layer files (raw, curated, analytics) — typically 5–10× smaller than JSON |
| **JSON** | Config files, schema snapshots, drift reports, quality reports, lineage records |

### Source System Connectors

| Source | API / Protocol | Notes |
|---|---|---|
| **Salesforce CRM** | Bulk API 2.0 (async, high-volume); Describe API for metadata | Handles millions of records without API timeouts |
| **NetSuite ERP** | SuiteQL REST API; metadata endpoint | OAuth 1.0a; parameterised SuiteQL queries |
| **MySQL RDS** | SQL via `pymysql`; `INFORMATION_SCHEMA` introspection | Read-only credentials; parameterised queries only |

### Data Catalog and Query Engine

| Component | Technology | Notes |
|---|---|---|
| Metadata catalog | **AWS Glue Data Catalog** | Registered after every curated write |
| Ad-hoc SQL queries | **Amazon Athena** | Serverless; $5/TB scanned; partitioned datasets minimise scan cost |
| BI tool connectivity | Athena ODBC/JDBC or RDS direct | Supports Tableau, Power BI, Looker, Metabase |

### Security and Secrets

| Concern | Technology | Notes |
|---|---|---|
| Credential storage | **AWS Secrets Manager** | One secret per source per environment; scheduled rotation |
| Encryption at rest | **AWS KMS** (customer-managed CMK, SSE-KMS) | Applied to all S3 buckets, DynamoDB tables, and SQS queues |
| Encryption in transit | **TLS 1.2+** | Mandatory; enforced via S3 bucket policy (`aws:SecureTransport`) |
| Network isolation | **Amazon VPC** (private subnets) | No internet gateway; all AWS traffic via VPC Endpoints |
| PII tokenisation | **HMAC-SHA256** (keyed) | Deterministic pseudonym preserving join-key usability |
| Field fingerprinting | **SHA-256** | Schema snapshot identity and drift detection |
| Access control | **AWS IAM** (least-privilege roles) | No wildcard `Action:*` or `Resource:*` anywhere |

### Monitoring and Observability

| Component | Technology | Notes |
|---|---|---|
| Structured logging | **structlog** ≥ 24.4 | JSON output; PII-scrubbing processor; forwarded to CloudWatch Logs |
| Custom metrics | **Amazon CloudWatch** | Namespace: `EnterpriseDatalake`; 6 canonical metrics |
| Alarms | **Amazon CloudWatch Alarms** | 4 platform alarms: extraction failures, breaking drift, watermark lag, activity absent |
| Distributed tracing | **AWS X-Ray** | All Lambda and service-to-service calls instrumented |
| Alerting | **Amazon SNS** | SNS topic → email / PagerDuty for on-call |
| Dead-Letter Queue | **Amazon SQS** (KMS-encrypted) | 14-day retention; replay via `RunReplayController` |

### Python Stack and Code Quality

| Tool / Library | Version | Purpose |
|---|---|---|
| **Python** | 3.14.x (pyenv) | Runtime language for all Lambda/ECS task code |
| **Pydantic** | ≥ 2.7 | Data model validation; frozen models; strict `extra='forbid'` |
| **boto3** | Latest | AWS SDK — all AWS service calls |
| **pyarrow** | Latest | Apache Parquet read/write |
| **pymysql** | Latest | MySQL RDS connector |
| **structlog** | ≥ 24.4 | Structured JSON logging with PII scrubbing |
| **Ruff** | ≥ 0.5 | Linter (rules: E, W, F, I, N, S, B, C90, UP, ANN, T20, RUF) |
| **mypy** | ≥ 1.10 | Static type checker (strict mode) |
| **bandit** | ≥ 1.7 | Python SAST scanner (OWASP Top 10) |
| **pip-audit** | ≥ 2.7 | Dependency CVE scanning |
| **checkov** | Latest | Terraform IaC security scanner |
| **pytest** | Latest | Tests; ≥ 80% coverage gate enforced in CI |
| **moto** | ≥ 5.0 | AWS service mocking for unit tests |

### CI/CD

| Tool | Notes |
|---|---|
| **GitHub Actions** | 7-stage CI gate: lint → typecheck → test → SAST → CVE scan → IaC scan → Terraform validate |
| SHA-pinned action references | All `uses:` entries pinned to commit SHA digest (no mutable `@v4` tags) |
| **pre-commit hooks** | Ruff, detect-private-key, no-commit-to-branch, bandit, Terraform fmt/validate/checkov, detect-secrets |

---

## 13. Operational Resilience

### Automated Failure Recovery

| Failure scenario | Automatic response | Manual action required? |
|---|---|---|
| Source API temporarily unavailable | Retry with exponential backoff (3 attempts) | Only if circuit breaker opens |
| Partial extraction run | Watermark not advanced; next run picks up full window | No |
| DLQ message received | Replay available via `RunReplayController` | Yes — operations team triggers replay |
| Breaking schema drift | Raw stored; transformation blocked; alarm fires | Yes — governance review |
| Quality BLOCKING violation | Previous curated unchanged; report written; alarm fires | Yes — data team review |
| Watermark concurrency conflict | Run completes with `partial=True`; next run self-heals | No |

### Circuit Breaker

Each source–entity pair has an independent circuit breaker:
- Tracks consecutive failures
- Opens after threshold (default: 5 consecutive failures)
- Open circuit fails fast — does not waste time or API quota hammering a failing source
- Auto-resets on successful run; or manually reset after remediation

### Dead-Letter Queue

All terminal pipeline failures produce a structured DLQ message containing:
- `run_id`, `source_id`, `entity_id`, `failed_stage`
- `error_message` (scrubbed of all credentials and PII)
- `enqueued_at` timestamp

Operations teams consume DLQ messages via CloudWatch alarms and replay using the built-in replay controller.

### Idempotency

All pipeline writes are **idempotent**: re-running any stage with the same inputs produces the same outputs. S3 `put_object` replaces existing files; DynamoDB writes use conditional expressions. Replay is safe.

---

## 14. Scalability and Cost Profile

### Extraction Scaling

| Source volume | Handling |
|---|---|
| < 100k records / entity / day | Single Lambda execution (max 15 min) |
| 100k – 5M records / entity / day | Salesforce Bulk API 2.0 (async job); streamed in 50k-record chunks |
| > 5M records / entity / day | ECS Fargate task (no Lambda timeout limit) |

Streaming architecture: only one 50,000-record chunk is held in memory at any time. RAM usage is constant regardless of dataset size.

### Storage Costs (Approximate)

Parquet compression typically achieves 5–10× reduction vs raw JSON. Example: 10 million customer records (Salesforce Accounts) at ~2 KB each:

| Format | Size | Monthly S3 cost (Standard) |
|---|---|---|
| Raw JSON | ~20 GB / day | ~$0.46 / day |
| Parquet (Snappy) | ~2.5 GB / day | ~$0.06 / day |

The analytics layer moves to **S3 Intelligent-Tiering** after 30 days (auto moves to infrequent access after 90 days of no access).

### Athena Query Cost

Athena charges $5 per TB scanned. Year/month/day partitioning on the analytics layer means a typical dashboard query scans only the relevant partition — typically 1–10 GB rather than the full dataset.

---

## 15. Adding New Data Sources — Zero Code Changes

Adding a new data source requires only **configuration** — no code deployment:

### What is needed

1. **Register credentials** in Secrets Manager at path `{env}/{source_id}/credentials`
2. **Add entity configuration records** to DynamoDB (can be done via script or UI)
3. **Create extraction schedule** in EventBridge Scheduler
4. **Pass the 6-gate onboarding checklist** (security + governance review)

### What is NOT needed

- No new Python code
- No changes to orchestration, transformation, or governance modules
- No code review or deployment cycle for the source itself
- No new infrastructure (all compute, storage, and networking is shared)

### Timeline for a new source (estimated)

| Activity | Owner | Duration |
|---|---|---|
| Credential registration + Secrets Manager setup | Platform team | 0.5 day |
| Entity configuration records | Data team | 0.5 day |
| Dry-run in dev environment | Data team | 1 day |
| Security governance review | Security team | 1 day |
| Canary run in staging | Data team | 0.5 day |
| Production activation | Platform team | 0.5 day |
| **Total** | | **~4 days** |

Compare: previous approach of writing a new ETL script = 2–4 weeks.

---

## 16. Compliance and Audit Readiness

### Regulatory Controls Implemented

| Requirement | Implementation |
|---|---|
| Data retention enforcement | S3 Object Lock (GOVERNANCE mode); 7 years raw, 3 years curated |
| Right to erasure (GDPR Article 17) | Legal hold lift + S3 Object Lock governance bypass (governance role only); lineage records updated |
| Data lineage documentation | Automated lineage records from source to serving, per run |
| Access audit trail | CloudWatch Logs + S3 access logs; DynamoDB audit table per pipeline stage |
| PII masking at rest | Applied at transformation stage; never in raw layer without access control |
| Breach notification readiness | Classification policy enables immediate scoping of affected entities |
| Third-party data access | IAM prefix-scoped read roles per team; no shared credentials |

### Evidence Available for Audit

| Audit question | Evidence location |
|---|---|
| "Who extracted this data on this date?" | DynamoDB run audit log (table: `{env}-run-audit-log`) |
| "What fields were extracted?" | Schema snapshot (S3: `schemas/{source_id}/{entity_id}/{date}.json`) |
| "Was PII masked before analytics access?" | Transformation lineage record; classification policy version logged |
| "What changed in the source schema?" | Drift report (S3: alongside schema snapshot) |
| "Was this source security-reviewed before extraction started?" | Source onboarding registry (DynamoDB: `{env}-source-onboarding`) |
| "Who has access to raw PII data?" | IAM role policy (Terraform — version-controlled; extraction-service-role only) |

---

## 17. Key Metrics and SLOs

### Service Level Objectives

| SLO | Target | Alert threshold |
|---|---|---|
| Extraction completion rate | ≥ 99.5% of scheduled runs complete | < 98% over 7 days |
| Data freshness (time to curated) | ≤ 4 hours from extraction start | > 6 hours |
| Quality pass rate | ≥ 95% of entities publish without blocking violations | < 90% |
| Schema drift (breaking) | 0 unreviewed breaking drift events in production | Any unreviewed event > 24 hours |
| Watermark lag | ≤ 26 hours (daily entities); ≤ 3 hours (hourly entities) | Exceed threshold for 2 consecutive runs |
| DLQ depth | 0 unprocessed messages | Any message older than 4 hours |

### CloudWatch Dashboard Metrics

| Metric | Description |
|---|---|
| `RecordsExtracted` | Count of raw records written per run per entity |
| `RecordsFailed` | Records rejected (quality or mapping failure) |
| `WatermarkLagSeconds` | How far behind the watermark is vs current time |
| `SchemaDriftCount` | Drift events detected per entity |
| `RetryCount` | Retry attempts per run (high value = source instability) |

---

## 18. Roadmap

### Near-term (next quarter)

| Item | Description |
|---|---|
| Dynamics 365 connector | Configuration-only addition; adapter code ~3 days |
| HubSpot connector | Marketing activity data for unified customer view |
| Real-time CDC pipeline | Debezium + Kafka for sub-minute latency on MySQL changes |
| Self-service entity configuration UI | Web UI for data owners to manage entity configs without scripts |
| Data quality dashboard | Business-facing quality report dashboard in CloudWatch or Grafana |

### Medium-term (next two quarters)

| Item | Description |
|---|---|
| ML feature store integration | Publish analytics features directly to SageMaker Feature Store |
| Cross-environment data sharing | Analytics layer shared across prod environments via AWS Lake Formation |
| Column-level access control | Lake Formation column-masking for Athena queries (alternative to IAM prefix scope) |
| Automated schema migration | When non-breaking drift detected, auto-update Glue catalog schema |

### Long-term

| Item | Description |
|---|---|
| Multi-cloud support | Azure Data Lake and GCP BigQuery as alternative serving stores |
| AI-assisted entity resolution | LLM-based fuzzy matching for unstructured entity data |
| Data mesh transition | Domain-aligned ownership of curated datasets with platform providing infrastructure |

---

*For technical implementation details, see [docs/PLATFORM_FLOW.md](PLATFORM_FLOW.md).*  
*For infrastructure configuration, see [infrastructure/environments/](../infrastructure/environments/).*  
*For source onboarding, see [governance/source_onboarding_registry.py](../governance/source_onboarding_registry.py).*
