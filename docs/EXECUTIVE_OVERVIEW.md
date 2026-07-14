# Enterprise Data Lake Platform — Executive Overview

**Version:** 2.2  
**Date:** 2026-07-14  
**Classification:** Internal — Leadership Review  
**Audience:** Engineering leadership, product leadership, data governance, security, and finance stakeholders

> **Current Status:** Only the Dev AWS environment is deployed. Of five planned source
> connectors, two — Salesforce and MySQL RDS — have real credentials and have run end-to-end:
> **34 Salesforce accounts** and **36,023 MySQL RDS contract rows** are extracted, transformed,
> resolved, and queryable via Athena today. The other three connectors (Sage Intacct, Sage X3,
> NetSuite) are code-complete but have no credentials populated, so nothing has run for them yet.
> Staging and Production are not deployed — no AWS account exists for either. See
> [PLATFORM_STATUS.md](PLATFORM_STATUS.md) for the full current inventory.

---

## TL;DR

- **What this is:** one governed, metadata-driven pipeline that replaces per-team manual data
  pulls from Salesforce, NetSuite, MySQL RDS, and Sage with automated extraction, PII masking,
  entity resolution, and a full audit trail.
- **Current status:** Dev is the only deployed environment. Salesforce and MySQL RDS are fully
  connected with real data flowing end-to-end; Sage Intacct, Sage X3, and NetSuite are
  code-complete but still have empty Secrets Manager credential shells. Staging and production
  have no AWS account yet.
- **Headline business outcome (projected):** roughly 103% Year 1 ROI and about $6,300/month in
  eliminated manual-labor cost once all five sources are live and a pilot tenant is running —
  this is a financial model to sanity-check the business case, not yet a measured result. See
  [COST_ANALYSIS_AND_ROI.md](COST_ANALYSIS_AND_ROI.md) for the full breakdown.
- **What's not ready yet:** the serving store (a relational database export that would let BI
  tools like Power BI or Tableau query the platform directly) is fully built but not deployed to
  any environment — and even once deployed, there is currently no secure network path for an
  external BI tool to reach it. That is separate, not-yet-designed infrastructure work, not just
  a deployment step.
- **Where to look next:** [KNOWN_GAPS_AND_ROADMAP.md](KNOWN_GAPS_AND_ROADMAP.md) tracks every
  open gap in plain language; [PLATFORM_STATUS.md](PLATFORM_STATUS.md) is the canonical
  "what's deployed right now" reference.

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
9. [Data Layers — Summary](#9-data-layers--summary)
10. [Data Quality and Governance](#10-data-quality-and-governance)
11. [Security Architecture Summary](#11-security-architecture-summary)
12. [Technology Stack — Summary](#12-technology-stack--summary)
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
- ERP financial records in **Sage Intacct** (cloud accounting) and **Sage X3** (enterprise ERP)

Each team had its own extract scripts — inconsistent, brittle, impossible to audit, and carrying serious security risks (credentials in scripts, no access control, no lineage).

Analytics teams waited days for data. Compliance teams had no record of who accessed what or when. The same customer could appear as three different entities across three systems with no way to resolve them.

### What We Built

An **Enterprise Data Lake Platform** — a security-first, metadata-driven pipeline, currently
running in a single Dev environment, that:

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
| Credential security | Scripts and .env files | AWS Secrets Manager with daily expiry-check alerting (auto-rotation planned) |
| Compliance readiness | Manual documentation | Automated lineage + retention enforcement |

These outcomes describe the pipeline's designed and implemented behavior, demonstrated so far in
the Dev environment for the two connected sources (Salesforce, MySQL RDS). They have not yet been
demonstrated at production scale, across all five source connectors, or with a second tenant.

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
RESOLVE — match the same customer/supplier/product across Salesforce + NetSuite + MySQL + Sage
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
│   Salesforce (CRM) │ NetSuite (ERP) │ MySQL RDS (Transactional) │ Sage (Intacct + X3) │
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
                   │  MySQL / Postgres / │  ← Would power low-latency APIs
                   │  SQL Server         │    and apps. Code-complete, not
                   │  (not yet deployed) │    yet deployed in any environment.
                   └─────────────────────┘
```

---

## 5. Connected Data Sources

Of five source connectors, two have real credentials and are running; three are code-complete
but not yet connected to a real account.

| Source System | Type | Status | Configured Entities | Extraction Method |
|---|---|---|---|---|
| **Salesforce** | CRM | ✅ Connected, live | Account (companies — **live, 34 records**), Contact (persons — live, count not re-confirmed), Opportunity and Contract (configured, not yet seeded) | Salesforce Bulk API 2.0 (high-volume, async) |
| **MySQL RDS** | Transactional DB | ✅ Connected, live | Contracts (**live, 36,023 rows**), Contract Terms (configured, not yet seeded) | JDBC / SQLAlchemy read-only connection |
| **Sage Intacct** | Cloud Accounting ERP | 🟡 Code-complete, no credentials yet | Customer, Vendor, AR Invoice, AP Bill (configured, not run) | Intacct REST API (OAuth 2.0; JSON-POST; `ia::meta.next` cursor pagination) |
| **Sage X3** | Enterprise ERP | 🟡 Code-complete, no credentials yet | Customer, Supplier (configured, not run) | OData v4 REST API (OAuth 2.0; `@odata.nextLink` pagination) |
| **NetSuite** | ERP | 🟡 Code-complete, no credentials yet | Customer (configured, not run) | SuiteQL REST API |

Bringing a code-complete source online requires only populating its Secrets Manager credential
and seeding its entity configuration — no new code — but that work has not been done for
Sage Intacct, Sage X3, or NetSuite yet.

**Planned future sources** (configuration-only addition — no code change expected):  
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

> **Illustrative, not current state:** the table below shows the scheduling pattern the platform
> supports and how a fully onboarded set of entities would be scheduled. No entity schedule is
> enabled in Dev today — the only EventBridge schedule actually running is the daily credential
> expiry check. Today's live entities (Salesforce Account/Contact, MySQL RDS Contracts) have been
> run via manual trigger, not yet on an enabled cron schedule.

### Illustrative Schedule Reference

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
- Path: `{environment}/sources/{source_id}/credentials` (or `{environment}/sources/sage/{product}/credentials` for Sage)
- Only the `extraction-service-role` for that source has `GetSecretValue` permission.
- A daily automated check compares each secret's age against a threshold and alerts the platform team via SNS if rotation is overdue; automatic rotation itself is a planned follow-up and is not yet implemented.
- Credentials are retrieved at runtime, held in memory for the duration of one extraction run, and never logged.

---

## 9. Data Layers — Summary

The platform stores data in four progressively more refined layers — **Raw** (immutable,
as-received archive), **Curated** (cleaned, standardised, PII-masked, queryable via Athena),
**Analytics** (entity-resolved "golden records" optimised for BI and ML), and an optional
**Serving Store** (a relational database export for applications that need low-latency reads
rather than S3-backed SQL).

Why layer it this way, in business terms: keeping an untouched raw copy means a bad
transformation rule can always be replayed without going back to the source system; keeping
curated and analytics separate means a business analyst gets one clean, consistent schema per
entity regardless of which source system the data originated from.

Only two of the seven-plus configured Analytics-layer entity types have live data flowing into
them today — `company` (Salesforce Account only so far) and `person` (Salesforce Contact) —
since only Salesforce and MySQL RDS are connected. The rest are configured and waiting on their
source connector or on seeding.

The **Serving Store** is code-complete (`serving_store/` module: four engine adapters, Lambda
handler, Terraform) but not yet deployed to any environment — nothing has been `terraform
apply`'d, so its Step Functions stage still runs as a no-op everywhere, including Dev. Even once
deployed, there is no VPN, PrivateLink, or bastion host anywhere in the network layer today, so
an external BI tool (Power BI, Tableau) would have no way to reach it — that network path is
still a design decision away, tracked in
[KNOWN_GAPS_AND_ROADMAP.md](KNOWN_GAPS_AND_ROADMAP.md).

For the full definition of each layer (retention periods, IAM roles, partitioning, formats) and
the complete "AWS service → role in platform" reference, see
[GLOSSARY_AND_TERMINOLOGY.md](GLOSSARY_AND_TERMINOLOGY.md#data-lake-layers-explained).

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
- Daily automated expiry-check Lambda alerts the platform team via SNS when a secret's age exceeds a threshold; automatic rotation is a planned follow-up, not yet implemented
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

### Multi-Tenant Isolation — Partial, Not Yet Production-Grade
- Each tenant's data is prefixed by `tenant_code` in S3 (raw, curated, analytics, schema
  snapshots) and in the DynamoDB watermark and entity-type-registry tables — this isolation is
  enforced by the application's writer/reader code today
- Two gaps remain: the entity-extraction-config table is isolated only by an application-level
  guard, not a tenant-scoped key, and no S3 bucket-policy or IAM condition yet enforces the
  tenant prefix as a hard boundary
- No second tenant has been onboarded yet, so this isolation model has not been exercised with
  real multi-tenant traffic
- Do not represent tenant isolation as complete or IAM-enforced yet — see
  [KNOWN_GAPS_AND_ROADMAP.md](KNOWN_GAPS_AND_ROADMAP.md) for the full, current list of isolation
  gaps

---

## 12. Technology Stack — Summary

The platform is built exclusively on proven, production-grade AWS services — Lambda and Step
Functions for compute and orchestration, S3 for all three data layers, DynamoDB for
configuration/watermark/audit state, Glue and Athena for cataloguing and querying, and
Secrets Manager/KMS/IAM for credentials, encryption, and access control. Every component is
version-pinned, security-scanned (SAST, dependency CVE scanning, IaC scanning), and managed
entirely as Terraform infrastructure-as-code, with an 8-stage CI gate on every change.

For the full "AWS service → role in platform" reference table, plus the Python library, CI/CD,
and observability stack, see
[GLOSSARY_AND_TERMINOLOGY.md](GLOSSARY_AND_TERMINOLOGY.md#technology-and-tools-glossary).

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

Storage costs step down further over time via S3 lifecycle rules (raw data moves to
Infrequent Access at 90 days and Glacier at 365 days; curated data moves to Infrequent Access at
180 days) — this is plain S3 lifecycle policy, not S3 Intelligent-Tiering, which is not
configured anywhere in this platform today.

### Athena Query Cost

Athena charges $5 per TB scanned. Year/month/day partitioning on the analytics layer means a typical dashboard query scans only the relevant partition — typically 1–10 GB rather than the full dataset.

### Full Cost Model and ROI

The figures above illustrate the platform's cost *shape* (compression ratio, partition-scoped
query cost). For the complete, itemised AWS monthly cost estimate, staffing cost comparison, and
ROI projection — all reconciled against the actual Terraform-defined resources — see
[COST_ANALYSIS_AND_ROI.md](COST_ANALYSIS_AND_ROI.md). Headline projection: roughly
**$770–$790/month** in AWS infrastructure once all five sources are connected and a pilot tenant
is running, against an estimated **$8,000/month** manual-labor baseline — a financial model, not
yet a measured result.

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
| "Who extracted this data on this date?" | DynamoDB run audit log (table: `{env}-edl-run-audit-log`) |
| "What fields were extracted?" | Schema snapshot (S3: `schemas/{source_id}/{entity_id}/{date}.json`) |
| "Was PII masked before analytics access?" | Transformation lineage record; classification policy version logged |
| "What changed in the source schema?" | Drift report (S3: alongside schema snapshot) |
| "Was this source security-reviewed before extraction started?" | Source onboarding registry (DynamoDB: `{env}-source-onboarding`) |
| "Who has access to raw PII data?" | IAM role policy (Terraform — version-controlled; extraction-service-role only) |

---

## 17. Key Metrics and SLOs

These are the design targets the alarms and dashboards are built around. They have not yet been
measured over a sustained period of real, scheduled production traffic — today's evidence is a
small number of manually-verified runs against two connected sources in Dev.

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

### Immediate next steps (the actual gating items before a second tenant or a staging deployment)

| Item | Description |
|---|---|
| Populate remaining source credentials | Sage Intacct, Sage X3, and NetSuite connectors are code-complete but have no real credentials yet — needed before any of the three can run |
| Verify control-plane API end-to-end | The Cognito-authenticated REST API (tenant provisioning, entity registration, pipeline trigger) is deployed in Dev, but a live login/JWT round-trip against the deployed API Gateway + Cognito pool has not yet been exercised |
| Pilot tenant onboarding | Onboard one real second tenant and run it in parallel with the default tenant for roughly a week with no cross-tenant incidents — not yet started |
| Load test at target scale | Synthetic load test across the target entity count and tenant count — not yet started |
| Close remaining tenant-isolation gaps | `entity-extraction-config` tenant-key scoping and the S3 bucket-policy tenant-prefix condition should land before pilot tenant traffic is trusted — see [KNOWN_GAPS_AND_ROADMAP.md](KNOWN_GAPS_AND_ROADMAP.md) for the full list |
| Design and build a serving-store network path | The serving store itself is code-complete but not deployed, and even once deployed there is no VPN/PrivateLink/bastion for an external BI tool to reach it — needs a design decision (e.g. AWS Client VPN with per-tenant certificates) before it is usable by Power BI/Tableau |
| Staging deployment | Requires its own AWS account/credentials; `terraform validate` is already clean for `staging`, so this is provisioning work, not code work |
| Production deployment | Gated on staging sign-off per this repo's promotion policy; no AWS account exists for production yet |

### Near-term (next quarter, after the items above)

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

*For technical implementation details, see [docs/PIPELINE_FLOW.md](PIPELINE_FLOW.md).*  
*For infrastructure configuration, see [infrastructure/environments/](../infrastructure/environments/).*  
*For source onboarding, see [governance/source_onboarding_registry.py](../governance/source_onboarding_registry.py).*
