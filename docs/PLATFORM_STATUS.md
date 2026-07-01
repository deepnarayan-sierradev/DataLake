# Platform Status — Enterprise Data Lake

**Last updated:** 2026-07-01  
**Prepared by:** Platform Engineering

---

## Current Status: Dev ✅ | Staging 🔲 | Production 🔲

| Environment | Status | Notes |
|---|---|---|
| **Dev** | ✅ Live and operational | All 4 Lambda stages deployed and tested end-to-end |
| **Staging** | 🔲 Not started | Requires DynamoDB pre-creation + Terraform apply |
| **Production** | 🔲 Not started | Requires staging sign-off first |

---

## Live Data (Dev — as of 2026-06-29)

| Entity | Records | Location |
|---|---|---|
| Companies (Salesforce Accounts) | 34 | `dev_edl_analytics.company` |
| Persons (Salesforce Contacts) | 49 | `dev_edl_analytics.person` |
| Contracts (MySQL RDS) | 35,971 | `dev_edl_analytics.contract` |

**Query in Athena (AWS Console → Athena → database: `dev_edl_analytics`):**

```sql
SELECT * FROM dev_edl_analytics.company    WHERE analytics_date='2026-06-29';
SELECT * FROM dev_edl_analytics.person     WHERE analytics_date='2026-06-29';
SELECT COUNT(*) FROM dev_edl_analytics.contract   WHERE analytics_date='2026-06-29';
SELECT COUNT(*) FROM dev_edl_analytics.supplier   WHERE analytics_date='2026-06-29';
SELECT COUNT(*) FROM dev_edl_analytics.ar_invoice  WHERE analytics_date='2026-06-29';
SELECT COUNT(*) FROM dev_edl_analytics.ap_bill     WHERE analytics_date='2026-06-29';
```

---

## Connected Data Sources

| Source | Status | Entities |
|---|---|---|
| **Salesforce CRM** | ✅ Connected | `salesforce-account` (companies), `salesforce-contact` (persons) |
| **MySQL RDS** | ✅ Connected | `mysql-rds-contracts` (contracts) |
| **Sage Intacct** | ✅ Connected | `sage-intacct-customer` (companies), `sage-intacct-vendor` (suppliers), `sage-intacct-arinvoice` (AR invoices), `sage-intacct-apbill` (AP bills) |
| **Sage X3** | ✅ Connected | `sage-x3-customer` (companies), `sage-x3-supplier` (suppliers) |
| **NetSuite ERP** | 🔲 Pending | Not yet onboarded |

---

## AWS Resources — Dev Environment

**AWS Account:** `087972550871` | **Region:** `us-east-1`

### S3 Buckets

| Bucket | Pipeline stage | Written by | Read by | Purpose |
|---|---|---|---|---|
| `dev-edl-raw-layer` | Stage A — Extraction | Extraction Lambda | Transformation Lambda | Immutable raw Parquet files written once per extraction run. One Hive-partitioned prefix per entity per date. Never overwritten; watermark prevents re-extraction of unchanged data. |
| `dev-edl-curated-layer` | Stage B — Transformation | Transformation Lambda | Entity Resolution Lambda, Athena | Field-mapped, quality-checked Parquet (canonical column names). Also stores field-mapping JSON config files under `field-mappings/{source_id}/{entity_id}/`. |
| `dev-edl-analytics-layer` | Stages C & D — Entity Resolution + Analytics | Entity Resolution Lambda, Analytics Publisher Lambda | Athena, downstream BI tools | Golden (de-duplicated) records from entity resolution and consumption-optimised Parquet for analytics. Registered in Glue Catalog for Athena queries. |
| `dev-edl-schema-snapshots` | Stage A — Extraction (post-extract) | Extraction Lambda | Drift Evaluation (same Lambda) | JSON schema fingerprints captured after every extraction. The drift evaluator compares the new snapshot against `latest.json` to detect breaking changes (added/removed/type-changed columns). Path: `{source_id}/{entity_id}/{schema_hash}/YYYY-MM-DD.json` + `drift-report-YYYY-MM-DD.json`. |
| `dev-edl-s3-access-logs` | All stages (passive) | AWS S3 service (automatic) | Security & compliance audits | Receives S3 server access logs from every other data lake bucket. Never written to directly by pipeline code. Used for access auditing, cost attribution, and compliance. Retention: 30 days (dev). |
| `dev-edl-terraform-state` | Infrastructure (deploy time only) | Terraform CLI, `make lambda-upload` | Terraform CLI, Lambda service at deploy | Dual-purpose: (1) Terraform remote state file (`environments/dev/terraform.tfstate`) with DynamoDB lock for single-writer safety. (2) Lambda artifact store — `lambda/extraction-pipeline.zip` uploaded here by `make lambda-upload` and pulled by Lambda on every `terraform apply`. Not accessed at pipeline runtime. |

> **Legacy bucket — `dev-schema-snapshots`**: Created manually on 2026-06-26 before Terraform standardised the `dev-edl-*` naming convention. Contains early test snapshots only. The active bucket is `dev-edl-schema-snapshots`. This bucket is safe to archive and delete.

### S3 Key Patterns

| Layer | Pattern |
|---|---|
| Raw | `s3://dev-edl-raw-layer/raw/{source_id}/{entity_id}/extraction_date=YYYY-MM-DD/part-NNNNN.parquet` |
| Curated | `s3://dev-edl-curated-layer/curated/{source_id}/{entity_id}/run_id={run_id}/data.parquet` |
| Golden records | `s3://dev-edl-analytics-layer/canonical/{entity_type}/golden_date={date}/run_id={run_id}/golden.parquet` |
| Analytics | `s3://dev-edl-analytics-layer/analytics/{entity_type}/analytics_date=YYYY-MM-DD/data.parquet` |

### DynamoDB Tables

> These tables are **not** Terraform-managed — they were pre-created manually and must be created by hand in any new environment before running `terraform apply`.

| Table | Purpose | Hash key |
|---|---|---|
| `dev-entity-extraction-config` | Entity extraction configuration (source, watermark field, load type, etc.) | `entity_id` (String) |
| `dev-watermark-repository` | Per-entity watermark timestamps for incremental loads | — |
| `dev-run-audit-log` | Immutable audit record of every pipeline run | — |

### Lambda Functions

| Function | Handler | Purpose |
|---|---|---|
| `dev-extraction-pipeline` | `connector_runtime.extraction_pipeline_handler.lambda_handler` | Extract from source → raw layer |
| `dev-transformation-pipeline` | `transformation.transformation_pipeline_handler.lambda_handler` | Raw → curated layer |
| `dev-entity-resolution-pipeline` | `entity_resolution.entity_resolution_pipeline_handler.lambda_handler` | Curated → golden records |
| `dev-analytics-publisher` | `analytics_publisher.analytics_publisher_handler.lambda_handler` | Golden records → analytics layer |

All four Lambdas are deployed from the **same zip**: `s3://dev-edl-terraform-state/lambda/extraction-pipeline.zip`

### Step Functions

| State Machine | Purpose |
|---|---|
| `dev-data-pipeline` | Full end-to-end pipeline (extraction → analytics) |
| `dev-extraction-pipeline` | Extraction stage only (used for manual triggers) |

### Glue Catalog

| Database | Tables |
|---|---|
| `dev_edl_analytics` | `company`, `person`, `contract`, `supplier`, `ar_invoice`, `ap_bill` |

### Secrets Manager

| Secret | Contents |
|---|---|
| `dev/sources/salesforce/credentials` | `instance_url`, `client_id`, `client_secret` |
| `dev/sources/mysql-rds/credentials` | `host`, `port`, `username`, `password`, `database` |
| `dev/sources/sage/intacct/credentials` | `token_url`, `client_id`, `client_secret`, `base_url`, `company_id` |
| `dev/sources/sage/x3/credentials` | `token_url`, `client_id`, `client_secret`, `base_url`, `folder` |

---

## Terraform State

| Item | Value |
|---|---|
| Backend | S3 remote state |
| State bucket | `dev-edl-terraform-state` |
| State key | `environments/dev/terraform.tfstate` |
| Lock table | `dev-edl-terraform-state-lock` |

---

## Next Steps

### Activate Sage Intacct and Sage X3 Schedules

Entity configs for all 6 Sage entities are already seeded. To enable live extraction:

- Create Secrets Manager secrets: `dev/sources/sage/intacct/credentials` and `dev/sources/sage/x3/credentials`
- Set `schedule_enabled=True` for Sage entities in DynamoDB via `seed_entity_config.py`
- Trigger a dry-run: `python scripts/run_sage_connector_local.py --entity-id sage-intacct-customer --dry-run`

### Deploy Staging Environment

Pre-requisites:
- Staging AWS credentials configured
- Create 3 DynamoDB tables manually: `staging-entity-extraction-config`, `staging-watermark-repository`, `staging-run-audit-log`
- Upload Lambda zip: `ARTIFACTS_BUCKET=staging-edl-terraform-state make lambda-upload`

```bash
cd infrastructure/environments/staging
terraform init
terraform apply -target=module.iam
terraform apply -target=module.lambda_pipeline -target=module.transformation_lambda
terraform apply -target=module.orchestration
```

### Deploy Production Environment

Same pattern as staging — use `prod-edl-terraform-state` as `ARTIFACTS_BUCKET`. Lambda log retention is already configured at 365 days in HCL.

### Onboard NetSuite

- Add connector credentials to Secrets Manager: `dev/sources/netsuite/credentials`
- Seed entity config for NetSuite entities
- No code changes required — configuration-only onboarding
